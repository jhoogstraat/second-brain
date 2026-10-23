"""MCP server exposing the brain over Streamable HTTP at /mcp."""

import os
from pathlib import Path
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider, MultiAuth
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .brain import Brain, BrainError

INSTRUCTIONS = """\
This server is the user's second brain: a git-versioned LLM wiki of markdown files
(raw/ immutable sources, wiki/ pages you maintain, index.md catalog, log.md history).
Before your first change in a conversation, read AGENTS.md — it is the schema and
defines the ingest, query and lint workflows. For questions, start from index.md
and search, and cite the pages you used."""


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
    def read(path: str) -> str:
        """Read a file from the brain, e.g. "AGENTS.md", "index.md" or "wiki/concepts/x.md"."""
        return guard(brain.read, path)

    @mcp.tool(name="list", annotations={"readOnlyHint": True})
    def list_files(directory: str = "") -> list[str]:
        """List all files under a directory of the brain ("" = everything)."""
        return guard(brain.files, directory)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search(query: str, limit: int = 10) -> list[dict]:
        """Full-text (BM25) search across all markdown in the brain. Returns paths and snippets, best first."""
        return guard(brain.search, query, limit)

    @mcp.tool(annotations={"destructiveHint": False})
    def write(path: str, content: str, message: str = "") -> str:
        """Create or overwrite a markdown file (whole content) and commit it.
        New sources go to raw/ (existing raw/ files are immutable); wiki pages to wiki/.
        `message` is the git commit message."""
        return guard(brain.write, path, content, message)

    @mcp.tool(annotations={"destructiveHint": True})
    def delete(path: str, message: str = "") -> str:
        """Delete a wiki file (not raw/) and commit. Recoverable from git history."""
        return guard(brain.delete, path, message)

    @mcp.tool
    def log(operation: str, title: str, details: str = "") -> str:
        """Append an entry to log.md, e.g. operation="ingest", title="Article Title",
        details="pages touched: ...". Call once at the end of every ingest/query/lint."""
        return guard(brain.log, operation, title, details)

    @mcp.prompt
    def ingest(source: str) -> str:
        """Ingest a new source (URL, pasted text, or file) into the brain."""
        return f"Read AGENTS.md, then run the Ingest workflow for this source:\n\n{source}"

    @mcp.prompt
    def query(question: str) -> str:
        """Answer a question from the brain, filing good answers back."""
        return f"Read AGENTS.md, then run the Query workflow for:\n\n{question}"

    @mcp.prompt
    def lint() -> str:
        """Health-check the wiki: contradictions, orphans, gaps, broken links."""
        return "Read AGENTS.md, then run the Lint workflow over the whole wiki."

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def main() -> None:
    brain = Brain(Path(os.environ.get("BRAIN_DIR", "/data/brain")))
    brain.init()
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
