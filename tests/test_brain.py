import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain.brain import Brain
from second_brain.server import build_auth, build_server


def test_end_to_end(tmp_path):
    brain = Brain(tmp_path / "brain")
    brain.init()
    brain.init()  # idempotent

    async def run():
        async with Client(build_server(brain)) as c:
            call = lambda name, **kw: c.call_tool(name, kw)
            assert "LLM wiki" in (await call("read", path="AGENTS.md")).data
            await call("write", path="raw/2026-01-01-anki.md", content="Spaced repetition beats cramming.")
            await call("write", path="wiki/concepts/spaced-repetition.md", content="See [[2026-01-01-anki]].")
            await call("log", operation="ingest", title="Anki essay")

            hits = (await call("search", query="repetition cramming")).data
            assert hits[0]["path"] == "raw/2026-01-01-anki.md"
            assert "wiki/concepts/spaced-repetition.md" in (await call("list", directory="wiki")).data

            with pytest.raises(ToolError, match="immutable"):
                await call("write", path="raw/2026-01-01-anki.md", content="tampered")
            with pytest.raises(ToolError, match="immutable"):
                await call("delete", path="raw/2026-01-01-anki.md")
            with pytest.raises(ToolError, match="immutable"):
                await call("delete", path="RAW/2026-01-01-anki.md")
            for bad in ("../escape.md", ".git/config", ".gitignore", "wiki/.hidden.md"):
                with pytest.raises(ToolError, match="outside"):
                    await call("read", path=bad)

            await call("delete", path="wiki/concepts/spaced-repetition.md")
            assert (await c.get_prompt("ingest", {"source": "x"})).messages

    asyncio.run(run())
    assert "] ingest | Anki essay" in brain.read("log.md")
    commits = brain.git("log", "--oneline").stdout.splitlines()
    assert len(commits) == 5  # init, 2 writes, log, delete


def test_auth_required(monkeypatch):
    for k in ("BRAIN_TOKEN", "GITHUB_CLIENT_ID", "BRAIN_NO_AUTH"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        build_auth()
    monkeypatch.setenv("GITHUB_CLIENT_ID", "x")
    with pytest.raises(SystemExit, match="ALLOWED_USERS"):
        build_auth()


def test_github_allowlist(monkeypatch):
    from fastmcp.server.auth import AccessToken
    from fastmcp.server.auth.providers.github import GitHubProvider

    from second_brain.server import AllowlistGitHub

    async def fake_load(self, token):
        return AccessToken(token=token, client_id="c", scopes=["read:user"], claims={"login": token, "sub": "42"})

    monkeypatch.setattr(GitHubProvider, "load_access_token", fake_load)
    p = AllowlistGitHub({"jhoogstraat"}, client_id="x", client_secret="x",
                        base_url="https://brain.example.com", jwt_signing_key="k" * 32)
    assert asyncio.run(p.load_access_token("JHoogstraat")) is not None
    assert asyncio.run(p.load_access_token("mallory")) is None
    by_id = AllowlistGitHub({"42"}, client_id="x", client_secret="x",
                            base_url="https://brain.example.com", jwt_signing_key="k" * 32)
    assert asyncio.run(by_id.load_access_token("mallory")) is not None  # numeric ID matches
