"""The brain on disk: a git repo of markdown files (raw/, wiki/, index.md, log.md)."""

import logging
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).parent / "template"
# ponytail: one global write lock; plenty for a single user's handful of harnesses.
_lock = threading.Lock()


class BrainError(ValueError):
    pass


class Brain:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        name = os.environ.get("BRAIN_GIT_NAME", "Second Brain")
        email = os.environ.get("BRAIN_GIT_EMAIL", "brain@localhost")
        return subprocess.run(
            ["git", "-c", f"user.name={name}", "-c", f"user.email={email}", "-C", str(self.root), *args],
            check=check, capture_output=True, text=True,
        )

    def init(self) -> None:
        """Seed an empty data dir with the template and make it a git repo."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "AGENTS.md").exists():
            shutil.copytree(TEMPLATE, self.root, dirs_exist_ok=True)
            (self.root / "raw").mkdir(exist_ok=True)
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")
        self.commit(".", "Initialise second brain")

    def path(self, rel: str) -> Path:
        p = (self.root / rel.lstrip("/")).resolve()
        # Dot-paths (.git, .gitignore, ...) would let a client break or hijack git.
        if not p.is_relative_to(self.root) or any(part.startswith(".") for part in p.relative_to(self.root).parts):
            raise BrainError(f"path outside the brain: {rel}")
        return p

    def is_raw(self, p: Path) -> bool:
        parts = p.relative_to(self.root).parts
        return bool(parts) and parts[0].casefold() == "raw"  # casefold: macOS is case-insensitive

    def walk(self, base: Path, pattern: str = "*"):
        """Regular files under base, skipping dot-dirs and symlinks (which could point outside)."""
        for p in base.rglob(pattern):
            if p.is_file() and not p.is_symlink() and not any(
                part.startswith(".") for part in p.relative_to(self.root).parts
            ):
                yield p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def commit(self, rel: str, message: str) -> None:
        self.git("add", "-A", "--", rel)
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self.git("commit", "-q", "-m", message)
        if os.environ.get("BRAIN_GIT_PUSH") == "1":
            # ponytail: synchronous push per write; batch/background it if writes feel slow.
            r = self.git("push", "-q", check=False)
            if r.returncode:
                log.warning("git push failed: %s", r.stderr.strip())

    def read(self, rel: str) -> str:
        p = self.path(rel)
        if not p.is_file():
            raise BrainError(f"no such file: {rel}")
        return p.read_text(errors="replace")

    def files(self, rel: str = "") -> list[str]:
        base = self.path(rel)
        return sorted(self.rel(p) for p in self.walk(base))

    def write(self, rel: str, content: str, message: str) -> str:
        p = self.path(rel)
        with _lock:
            if self.is_raw(p) and p.exists():
                raise BrainError("raw/ sources are immutable; save a new file instead")
            created = not p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self.commit(self.rel(p), message or f"{'Create' if created else 'Update'} {self.rel(p)}")
        return f"{'created' if created else 'updated'} {self.rel(p)}"

    def delete(self, rel: str, message: str) -> str:
        p = self.path(rel)
        if self.is_raw(p):
            raise BrainError("raw/ sources are immutable")
        if not p.is_file():
            raise BrainError(f"no such file: {rel}")
        with _lock:
            p.unlink()
            self.commit(self.rel(p), message or f"Delete {self.rel(p)}")
        return f"deleted {self.rel(p)}"

    def log(self, operation: str, title: str, details: str = "") -> str:
        operation, title = " ".join(operation.split()), " ".join(title.split())  # one-line heading
        entry = f"\n## [{date.today().isoformat()}] {operation} | {title}\n{details.strip()}\n"
        with _lock:
            with open(self.path("log.md"), "a") as f:
                f.write(entry)
            self.commit("log.md", f"log: {operation} | {title}")
        return entry.strip()

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """BM25 full-text search over every markdown file."""
        terms = " OR ".join('"' + t.replace('"', '""') + '"' for t in query.split())
        if not terms:
            return []
        # ponytail: rebuilds an in-memory FTS5 index per query (fine to ~thousands of pages);
        # persist it or switch to qmd/hybrid vectors when it gets slow.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(path UNINDEXED, body, tokenize='porter unicode61')")
        db.executemany(
            "INSERT INTO docs VALUES (?, ?)",
            ((self.rel(p), p.read_text(errors="replace")) for p in self.walk(self.root, "*.md")),
        )
        rows = db.execute(
            "SELECT path, snippet(docs, 1, '**', '**', '…', 24) FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (terms, limit),
        )
        return [{"path": path, "snippet": snippet} for path, snippet in rows]
