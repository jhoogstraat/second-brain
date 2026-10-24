"""MCP server exposing the brain over Streamable HTTP at /mcp."""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.auth import AuthProvider, MultiAuth
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .brain import Brain, BrainError

INSTRUCTIONS = """\
This is the user's second brain: a wiki that you, the LLM, write and maintain (after
Karpathy's "LLM wiki"). Knowledge compounds: every source and every good answer should
leave the wiki richer. You decide what to write; the server handles paths, frontmatter,
links, the index, the log and git commits.

- Questions: `search`, then `read` the pages it finds (`read("index")` lists every page).
  Answer with [[page]] citations. If the answer is a reusable synthesis, save it with
  `write_page(kind="note")`.
- New material (article, notes, transcript, a file's text): `save_source` it verbatim, then
  `write_page` a kind="source" summary citing it, then create or update every entity and
  concept page it touches — usually several. `read` a page before updating it:
  `write_page` replaces the whole body.
- Pages: one topic per page, a one-line summary, and generous [[Title]] links to related
  pages (missing targets are reported back — create them or drop the link). Kinds are free
  folders: entity, concept, note, source, or domain kinds like recipe. List the raw
  sources a page draws on in `sources`. When sources disagree, keep both claims and say
  which is newer. Ask the user what to emphasise unless they said to go ahead.
- Maintenance: `lint` reports broken links, orphans and uncited sources; fix them and look
  for contradictions while you are there.
- Rules the user sets for this brain go on the page "Conventions" (kind="meta"); the index
  shows them at the top, so follow them."""


class AllowlistGitHub(GitHubProvider):
    """GitHub OAuth that only honours tokens for allowed GitHub logins or numeric user IDs.
    Numeric IDs survive account renames; logins are friendlier."""

    def __init__(self, allowed: set[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed = allowed

    async def load_access_token(self, token: str):
        t = await super().load_access_token(token)
        claims = (t.claims or {}) if t else {}
        ids = {str(claims.get("login", "")).lower(), str(claims.get("sub", ""))} - {""}
        return t if ids & self.allowed else None


SECRETS = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "JWT_SIGNING_KEY", "BRAIN_TOKEN")


def load_secret_files() -> None:
    """Read NAME from the file in NAME_FILE, so secrets can be mounted instead of passed as env."""
    for name in SECRETS:
        if path := os.environ.get(f"{name}_FILE"):
            os.environ[name] = Path(path).read_text().strip()


def build_auth() -> AuthProvider | None:
    env = os.environ
    verifiers = []
    if env.get("BRAIN_TOKEN"):
        verifiers.append(StaticTokenVerifier({env["BRAIN_TOKEN"]: {"client_id": "static-token", "scopes": ["read:user"]}}))
    if env.get("GITHUB_CLIENT_ID"):
        allowed = {u.strip().lower() for u in env.get("GITHUB_ALLOWED_USERS", "").split(",") if u.strip()}
        if not allowed or not env.get("JWT_SIGNING_KEY") or not env.get("PUBLIC_URL"):
            raise SystemExit("GitHub auth needs GITHUB_ALLOWED_USERS, JWT_SIGNING_KEY and PUBLIC_URL")
        github = AllowlistGitHub(
            allowed,
            client_id=env["GITHUB_CLIENT_ID"],
            client_secret=env["GITHUB_CLIENT_SECRET"],
            base_url=env["PUBLIC_URL"],
            jwt_signing_key=env["JWT_SIGNING_KEY"],
            required_scopes=["read:user"],  # least privilege: GitHub's default "user" can edit the profile
            cache_ttl_seconds=300,
        )
        return MultiAuth(server=github, verifiers=verifiers) if verifiers else github
    if verifiers:
        return verifiers[0]
    if env.get("BRAIN_NO_AUTH") == "1":
        return None
    raise SystemExit("No auth configured: set GITHUB_CLIENT_ID/... and/or BRAIN_TOKEN (or BRAIN_NO_AUTH=1 for local dev)")


def build_server(brain: Brain, auth: AuthProvider | None = None) -> FastMCP:
    mcp = FastMCP("second-brain", instructions=INSTRUCTIONS, auth=auth)

    def guard(fn, *args):
        try:
            return fn(*args)
        except BrainError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations={"readOnlyHint": True})
    def read(name: str) -> str:
        """Read a page by title or name (e.g. "Spaced Repetition"), a source, "index"
        (catalog of every page) or "log". Pages end with the pages linking to them."""
        return guard(brain.read, name)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search(query: str, limit: int = 10) -> list[dict]:
        """Full-text search over pages and sources. Returns name, title, summary, snippet; best first."""
        return guard(brain.search, query, limit)

    @mcp.tool(annotations={"destructiveHint": False})
    def save_source(title: str, content: str, url: str = "") -> dict:
        """Store new material verbatim as an immutable source. Returns its name (cite it in
        write_page's `sources`) and existing pages that look related."""
        return guard(brain.save_source, title, content, url)

    @mcp.tool(annotations={"destructiveHint": False})
    def write_page(title: str, kind: str, summary: str, body: str, sources: list[str] | None = None) -> dict:
        """Create or replace a wiki page. `kind`: entity, concept, note, source, overview, meta, or
        any domain kind. `summary`: one line for the index. `body`: markdown with [[Title]] links,
        no frontmatter. `sources`: names of sources the page draws on."""
        return guard(brain.write_page, title, kind, summary, body, sources)

    @mcp.tool(annotations={"destructiveHint": True})
    def delete_page(name: str) -> dict:
        """Delete a wiki page (sources are immutable). Recoverable from git history."""
        return guard(brain.delete_page, name)

    @mcp.tool(annotations={"readOnlyHint": True})
    def lint() -> dict:
        """Health check: broken links, orphan pages, pages without sources, uncited sources."""
        return brain.lint()

    @mcp.resource("brain://index.md", name="index", mime_type="text/markdown")
    def index() -> str:
        """Catalog of every page with its one-line summary."""
        return brain.read("index")

    @mcp.resource("brain://{path*}", name="file", mime_type="text/markdown")
    def file(path: str) -> str:
        """Any file in the brain, e.g. brain://wiki/concept/x.md."""
        try:
            return brain.read(path)
        except BrainError as e:
            raise ResourceError(str(e)) from e

    # Prompts repeat the method, for clients that drop server instructions.
    @mcp.prompt
    def ingest(source: str) -> str:
        """Add new material to the brain."""
        return f"{INSTRUCTIONS}\n\n---\n\nIngest this into the brain:\n\n{source}"

    @mcp.prompt
    def query(question: str) -> str:
        """Answer a question from the brain."""
        return f"{INSTRUCTIONS}\n\n---\n\nAnswer from the brain: {question}"

    @mcp.prompt(name="lint")
    def lint_brain() -> str:
        """Health-check and tidy the wiki."""
        return f"{INSTRUCTIONS}\n\n---\n\nRun `lint`, fix what it finds, and look for contradictions."

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def main() -> None:
    brain = Brain(Path(os.environ.get("BRAIN_DIR", "/data/brain")))
    if sys.argv[1:] == ["reset"]:
        backup = brain.reset()
        print(f"Fresh brain at {brain.root}" + (f"; old one kept at {backup}" if backup else ""))
        return
    brain.init()
    load_secret_files()
    mcp = build_server(brain, build_auth())
    hosts = ["localhost", "127.0.0.1"]
    if os.environ.get("PUBLIC_URL"):
        hosts.append(urlparse(os.environ["PUBLIC_URL"]).hostname)
    mcp.run(
        transport="http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        allowed_hosts=hosts,
        show_banner=False,
    )
