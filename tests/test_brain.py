import asyncio
import os

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain.brain import Brain, BrainError
from second_brain.server import build_auth, build_server, load_secret_files


def test_end_to_end(tmp_path):
    brain = Brain(tmp_path / "brain")
    brain.init()
    brain.init()  # idempotent

    async def run():
        async with Client(build_server(brain)) as c:
            call = lambda tool, **kw: c.call_tool(tool, kw)
            assert "[[overview]]" in (await call("read", name="index")).data

            src = (await call("save_source", title="Anki Essay", content="Spaced repetition beats cramming.")).data
            assert src["source"].endswith("-anki-essay")
            page = (await call("write_page", title="Spaced Repetition", kind="Concept",
                               summary="Reviewing at growing intervals.",
                               body="# Spaced Repetition\n\nBeats cramming, see [[Active Recall]] and [[Überblick]].",
                               sources=[src["source"]])).data
            assert page["path"] == "wiki/concept/spaced-repetition.md"
            assert page["links_to_missing_pages"] == ["active-recall", "ueberblick"]

            text = (await call("read", name="Spaced Repetition")).data
            assert text.count("# Spaced Repetition") == 1  # duplicate H1 dropped
            assert "[[active-recall|Active Recall]]" in text and 'kind: "concept"' in text
            await call("write_page", title="Active Recall", kind="concept", summary="Testing yourself.",
                       body="Pairs with [[spaced-repetition]].")
            assert "Linked from: [[active-recall]]" in (await call("read", name="spaced-repetition")).data

            index = (await call("read", name="index")).data
            assert "## Concept" in index and "- [[spaced-repetition]] — Reviewing at growing intervals." in index
            await call("write_page", title="Conventions", kind="meta", summary="Rules.", body="Write in German.")
            assert "## Conventions of this brain\n\nWrite in German." in (await call("read", name="index")).data

            hits = (await call("search", query="cramming")).data
            assert {h["name"] for h in hits} == {src["source"], "spaced-repetition"}

            report = (await call("lint")).data
            assert report["broken_links"] == {"spaced-repetition": ["ueberblick"]}
            assert report["uncited_sources"] == [] and report["orphans"] == []

            for bad in (dict(title="x", kind="note", summary="s", body="b", sources=["nope"]),
                        dict(title="Index", kind="note", summary="s", body="b"),
                        dict(title="x", kind="note", summary="s", body="---\ntitle: x\n---")):
                with pytest.raises(ToolError):
                    await call("write_page", **bad)
            for bad in ("../escape.md", ".git/config", "wiki/.hidden.md"):
                with pytest.raises(ToolError, match="outside"):
                    await call("read", name=bad)
            with pytest.raises(ToolError, match="immutable"):
                await call("delete_page", name=src["source"])

            assert (await call("delete_page", name="Active Recall")).data["now_broken_links_in"] == ["spaced-repetition"]
            assert "Ingest this" in (await c.get_prompt("ingest", {"source": "x"})).messages[0].content.text
            assert "# Index" in (await c.read_resource("brain://index.md"))[0].text
            assert "cramming" in (await c.read_resource(f"brain://raw/{src['source']}.md"))[0].text

    asyncio.run(run())
    log = brain.read("log")
    assert "] source | Anki Essay" in log and "] delete | Active Recall" in log
    assert brain.git("status", "--porcelain").stdout == ""  # everything committed

    backup = brain.reset()
    assert (backup / "raw").is_dir() and brain.pages().keys() == {"overview"}


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


def test_secret_files(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("s3cret\n")
    monkeypatch.delenv("BRAIN_TOKEN", raising=False)
    monkeypatch.setenv("BRAIN_TOKEN_FILE", str(tmp_path / "token"))
    load_secret_files()
    assert os.environ["BRAIN_TOKEN"] == "s3cret"


def test_page_edge_cases(tmp_path):
    brain = Brain(tmp_path / "brain")
    brain.init()
    w = brain.write_page

    w("C++", "concept", "A language.", "x")
    with pytest.raises(BrainError, match="overwrite"):
        w("C#", "concept", "Another language.", "y")  # both slugify to "c"
    w("Mercury", "entity", "The planet.", "x")
    with pytest.raises(BrainError, match="already exists as kind 'entity'"):
        w("Mercury", "element", "The metal.", "y")

    assert w("東京", "entity", "Capital of Japan.", "x")["page"] == "東京"
    src = brain.save_source("Москва", "text")["source"]
    assert src.endswith("-москва")
    w("Moskau", "entity", "City.", "See [[Москва]].", sources=[src])

    # Text copied from `read` (with its backlinks footer) doesn't store the footer.
    w("A", "concept", "a", "links [[B]]")
    w("B", "concept", "b", "plain")
    copied = brain.read("A").split("\n# A\n", 1)[1]
    assert "Linked from" in copied
    w("A", "concept", "a", copied)
    assert "Linked from" not in (brain.root / "wiki/concept/a.md").read_text()

    page = w("Go", "concept", "A language.",
             "# Golang tips\n\n---\n\n`[[Not A Link]]` ![[Photo.png]] [[photo.png]] [[Index]] [[Big Idea]]\n"
             "```\n[[Also Not]]\n```")
    text = (brain.root / page["path"]).read_text()
    assert "# Golang tips" in text and "`[[Not A Link]]`" in text and "[[Also Not]]" in text
    assert "![[Photo.png]]" in text and "[[big-idea|Big Idea]]" in text
    assert page["links_to_missing_pages"] == ["big-idea"]
    with pytest.raises(BrainError, match="frontmatter"):
        w("Bad", "note", "s", "---\ntitle: x\n---\nbody")

    (brain.root / "wiki/concept/hand.md").write_text("---\r\ntitle: Hand\r\nsources: [a, b]\r\n---\r\nbody\r\n")
    from second_brain.brain import parse
    assert parse((brain.root / "wiki/concept/hand.md").read_text())[0]["sources"] == ["a", "b"]
