"""The brain on disk: a git repo of markdown (raw/ sources, wiki/ pages, index.md, log.md).

The server does all bookkeeping — paths, frontmatter, links, index, log, commits — so an
agent only decides what to write.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)
# ponytail: one global write lock; plenty for a single user's handful of harnesses.
_lock = threading.Lock()
# [[target#heading|label]]; not ![[embeds]]. Code spans/blocks are skipped via CODE.
LINK = re.compile(r"(?<!!)\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
FOOTER = "\n---\nLinked from:"
UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue", "ß": "ss"})
ROOT_FILES = {"index": "index.md", "log": "log.md"}
LOG_HEADER = '# Log\n\nAppend-only, written by the server. Latest: `grep "^## \\[" log.md | tail -5`.\n'
CONVENTIONS = "conventions"


class BrainError(ValueError):
    pass


def slugify(text: str) -> str:
    """Lowercase, accents dropped (é -> e, ä -> ae), other scripts kept (東京, москва)."""
    text = unicodedata.normalize("NFKD", text.translate(UMLAUTS))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "-", unicodedata.normalize("NFC", text).lower()).strip("-")


def one_line(text: str) -> str:
    return " ".join(text.split())


def frontmatter(meta: dict) -> str:
    # JSON values are valid YAML, so Obsidian/Dataview read it and parsing stays trivial.
    return "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items()) + "---\n"


def parse(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        head, sep, body = text[4:].partition("\n---\n")
        if sep:
            meta = {}
            for line in head.splitlines():
                k, _, v = line.partition(":")
                try:
                    meta[k.strip()] = json.loads(v)
                except ValueError:  # hand-edited YAML, e.g. in Obsidian
                    v = v.strip()
                    meta[k.strip()] = ([x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                                       if v.startswith("[") and v.endswith("]") else v)
            return meta, body
    return {}, text


def is_page_link(target: str) -> bool:
    suffix = Path(target.strip()).suffix.lower()
    return not suffix or suffix == ".md" or not suffix[1:].isalpha()  # skip [[photo.png]] attachments


def links(text: str) -> set[str]:
    text = CODE.sub("", text)
    return {slugify(Path(t).stem if t.strip().endswith(".md") else t)
            for t in (m.group(1) for m in LINK.finditer(text)) if is_page_link(t)}


def normalize_links(text: str) -> str:
    """[[Spaced Repetition]] -> [[spaced-repetition|Spaced Repetition]] so links match file names."""
    def fix(m):
        target, heading, label = m.group(1).strip(), m.group(2) or "", m.group(3)
        slug = slugify(target)
        if slug == target or not slug or not is_page_link(target):
            return m.group(0)
        return f"[[{slug}{heading}{label or '|' + target}]]"
    parts = CODE.split(text)
    codes = CODE.findall(text)
    return "".join(LINK.sub(fix, p) + (codes[i] if i < len(codes) else "") for i, p in enumerate(parts))


class Brain:
    def __init__(self, root: Path):
        self.root = root.resolve()

    # --- git ---------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        name = os.environ.get("BRAIN_GIT_NAME", "Second Brain")
        email = os.environ.get("BRAIN_GIT_EMAIL", "brain@localhost")
        return subprocess.run(
            ["git", "-c", f"user.name={name}", "-c", f"user.email={email}", "-C", str(self.root), *args],
            check=check, capture_output=True, text=True,
        )

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self.git("commit", "-q", "-m", message)
        if os.environ.get("BRAIN_GIT_PUSH") == "1":
            # ponytail: synchronous push per write; batch/background it if writes feel slow.
            r = self.git("push", "-q", check=False)
            if r.returncode:
                log.warning("git push failed: %s", r.stderr.strip())

    # --- lifecycle ---------------------------------------------------------

    def init(self) -> None:
        """Create a fresh brain if the data dir is empty; otherwise leave it alone."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")
        if not (self.root / "log.md").exists():
            (self.root / "raw").mkdir(exist_ok=True)
            (self.root / "log.md").write_text(LOG_HEADER)
            self.write_page(
                "Overview", "overview", "What this brain knows and the main threads running through it.",
                "Nothing here yet. After the first sources, summarise what this brain knows.",
            )

    def reset(self) -> Path | None:
        """Move the current brain aside as a backup (returned) and start a fresh one."""
        backup = self.root.with_name(f"{self.root.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
        with _lock:  # ponytail: only guards this process; stop the server first if it is busy writing
            moved = self.root.exists()
            if moved:
                shutil.move(self.root, backup)
        self.init()
        return backup if moved else None

    # --- lookup ------------------------------------------------------------

    def path(self, rel: str) -> Path:
        p = (self.root / rel.lstrip("/")).resolve()
        # Dot-paths (.git, .gitignore, ...) would let a client break or hijack git.
        if not p.is_relative_to(self.root) or any(part.startswith(".") for part in p.relative_to(self.root).parts):
            raise BrainError(f"path outside the brain: {rel}")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def walk(self, base: Path, pattern: str = "*.md"):
        """Regular files under base, skipping dot-dirs and symlinks (which could point outside)."""
        if not base.is_dir():
            return
        for p in base.rglob(pattern):
            if p.is_file() and not p.is_symlink() and not any(
                part.startswith(".") for part in p.relative_to(self.root).parts
            ):
                yield p

    def pages(self) -> dict[str, Path]:
        return {p.stem: p for p in self.walk(self.root / "wiki")}

    def sources(self) -> dict[str, Path]:
        return {p.stem: p for p in self.walk(self.root / "raw")}

    def resolve(self, name: str) -> Path:
        """A page title/slug, a source slug, "index"/"log", or a path."""
        if "/" in name or name.endswith(".md"):
            p = self.path(name)
        else:
            slug = slugify(name)
            p = self.pages().get(slug) or self.sources().get(slug) or (
                self.root / ROOT_FILES[slug] if slug in ROOT_FILES else None)
        if not p or not p.is_file():
            raise BrainError(f"not found: {name} (try search)")
        return p

    def backlinks(self, slug: str) -> list[str]:
        return sorted(s for s, p in self.pages().items() if s != slug and slug in links(p.read_text()))

    # --- reading -----------------------------------------------------------

    def read(self, name: str) -> str:
        p = self.resolve(name)
        text = p.read_text(errors="replace")
        if p.parent.parent == self.root / "wiki":
            refs = self.backlinks(p.stem)
            text += f"\n---\nLinked from: {', '.join(f'[[{s}]]' for s in refs) if refs else 'nothing yet'}\n"
        return text

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """BM25 full-text search over all pages and sources."""
        terms = " OR ".join('"' + t.replace('"', '""') + '"' for t in query.split())
        if not terms:
            return []
        # ponytail: rebuilds an in-memory FTS5 index per query (fine to ~thousands of pages);
        # persist it or switch to qmd/hybrid vectors when it gets slow.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(name UNINDEXED, title, summary UNINDEXED, body, "
                   "tokenize='porter unicode61')")
        for p in [*self.walk(self.root / "wiki"), *self.walk(self.root / "raw")]:
            meta, body = parse(p.read_text(errors="replace"))
            db.execute("INSERT INTO docs VALUES (?, ?, ?, ?)",
                       (p.stem, meta.get("title", p.stem), meta.get("summary", ""), body))
        rows = db.execute(
            "SELECT name, title, summary, snippet(docs, 3, '**', '**', '…', 24) FROM docs "
            "WHERE docs MATCH ? ORDER BY rank LIMIT ?", (terms, limit))
        return [{"name": n, "title": t, "summary": s, "snippet": sn} for n, t, s, sn in rows]

    def lint(self) -> dict:
        """Mechanical health check. Contradictions and stale claims are left to the agent."""
        pages, sources = self.pages(), self.sources()
        known = set(pages) | set(sources) | set(ROOT_FILES)
        broken, inbound, uncited, no_sources = {}, set(), set(sources), []
        for slug, p in pages.items():
            meta, body = parse(p.read_text())
            out = links(body)
            if missing := sorted(out - known):
                broken[slug] = missing
            inbound |= out - {slug}
            uncited -= set(meta.get("sources", [])) | out
            if not meta.get("sources") and meta.get("kind") not in ("overview", "meta", "note"):
                no_sources.append(slug)
        return {
            "broken_links": broken,
            "orphans": sorted(set(pages) - inbound - {"overview", CONVENTIONS}),
            "pages_without_sources": sorted(no_sources),
            "uncited_sources": sorted(uncited),
        }

    # --- writing -----------------------------------------------------------

    def save_source(self, title: str, content: str, url: str = "") -> dict:
        if not one_line(title) or not content.strip():
            raise BrainError("title and content are required")
        with _lock:
            base = f"{date.today().isoformat()}-{slugify(title)}"
            slug, n = base, 2
            while (self.root / "raw" / f"{slug}.md").exists():
                slug, n = f"{base}-{n}", n + 1
            meta = {"title": one_line(title), "url": url, "captured": date.today().isoformat()}
            p = self.root / "raw" / f"{slug}.md"
            p.parent.mkdir(exist_ok=True)
            p.write_text(frontmatter(meta) + "\n" + content.strip() + "\n")
            self._log("source", meta["title"])
            self.commit(f"source: {meta['title']}")
        return {"source": slug, "related_pages": [r["name"] for r in self.search(title, 8) if r["name"] != slug]}

    def write_page(self, title: str, kind: str, summary: str, body: str, sources: list[str] | None = None) -> dict:
        title, summary, kind = one_line(title), one_line(summary), slugify(kind)
        slug = slugify(title)
        if not slug or not kind or not summary:
            raise BrainError("title, kind and summary are required")
        if slug in ROOT_FILES:
            raise BrainError(f"'{title}' is reserved; choose another title")
        body = body.replace("\r\n", "\n").strip()
        if FOOTER in "\n" + body:  # text copied from `read` carries the generated backlinks footer
            body = ("\n" + body).rpartition(FOOTER)[0].strip()
        if re.match(r"---\n(?:[\w-]+:.*\n)+---(\n|$)", body):
            raise BrainError("body must not contain frontmatter; pass title/kind/summary/sources instead")
        first, _, rest = body.partition("\n")
        if first.strip().lower() == f"# {title.lower()}":
            body = rest.strip()
        with _lock:
            all_sources = self.sources()
            if slug in all_sources:
                raise BrainError(f"'{title}' collides with source {slug}; choose another title")
            cited = []
            for s in sources or []:
                s = slugify(Path(s).stem)
                if s not in all_sources:
                    raise BrainError(f"unknown source: {s} (save it with save_source first)")
                cited.append(s)
            existing = self.pages().get(slug)
            old = parse(existing.read_text())[0] if existing else {}
            if existing and str(old.get("title", slug)).lower() != title.lower():
                raise BrainError(f"'{title}' would overwrite the page '{old.get('title')}' (same file name "
                                 f"{slug}); pick a more distinct title, or use '{old.get('title')}' to update it")
            p = self.root / "wiki" / kind / f"{slug}.md"
            if existing and existing != p:
                raise BrainError(f"'{title}' already exists as kind '{old.get('kind', existing.parent.name)}'; "
                                 "use that kind to update it, or delete_page it first to re-file it")
            created = old.get("created")
            meta = {"title": title, "kind": kind, "summary": summary, "sources": cited,
                    "created": created or date.today().isoformat(), "updated": date.today().isoformat()}
            body = normalize_links(body)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(frontmatter(meta) + f"\n# {title}\n\n{body}\n")
            self._reindex()
            action = "update" if existing else "create"
            self._log(action, f"{title} ({kind})")
            self.commit(f"{action}: {title}")
            known = set(self.pages()) | set(all_sources) | set(ROOT_FILES)
        result = {"page": slug, "path": self.rel(p), "action": action}
        if missing := sorted(links(body) - known):
            result["links_to_missing_pages"] = missing
        return result

    def delete_page(self, name: str) -> dict:
        slug = slugify(Path(name).stem)
        with _lock:
            p = self.pages().get(slug)
            if not p:
                raise BrainError(f"no such page: {name} (sources are immutable)")
            title = parse(p.read_text())[0].get("title", slug)
            p.unlink()
            self._reindex()
            self._log("delete", title)
            self.commit(f"delete: {title}")
        return {"deleted": slug, "now_broken_links_in": self.backlinks(slug)}

    def _log(self, operation: str, title: str) -> None:
        with open(self.root / "log.md", "a") as f:
            f.write(f"\n## [{date.today().isoformat()}] {operation} | {one_line(title)}\n")

    def _reindex(self) -> None:
        by_kind: dict[str, list[str]] = {}
        conventions = ""
        for slug, p in sorted(self.pages().items()):
            meta, body = parse(p.read_text())
            kind = meta.get("kind", p.parent.name)
            by_kind.setdefault(kind, []).append(f"- [[{slug}]] — {meta.get('summary', '')}")
            if slug == CONVENTIONS:
                conventions = body.split("\n", 2)[-1].strip() if body.lstrip().startswith("# ") else body.strip()
        out = ["# Index", "", "Catalog of every page, generated by the server on each change.", ""]
        if conventions:
            out += ["## Conventions of this brain", "", conventions, ""]
        for kind in sorted(by_kind, key=lambda k: (k != "overview", k)):
            out += [f"## {kind.replace('-', ' ').capitalize()}", "", *by_kind[kind], ""]
        (self.root / "index.md").write_text("\n".join(out))
