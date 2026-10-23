# Second Brain — schema

This is a personal knowledge base run as an **LLM wiki** (after Andrej Karpathy's
[llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).
The human curates sources and asks questions; you, the LLM, write and maintain the
wiki. Knowledge compounds: every source and every good answer leaves the wiki
better than you found it. This file is the schema — you and the human co-evolve it;
edit it when a convention changes.

You reach this repository only through the MCP tools (`read`, `list`, `search`,
`write`, `delete`, `log`). Every write is a git commit, so mistakes are recoverable.

## Layout

- `raw/` — **immutable** source documents (articles, notes, transcripts, papers),
  one markdown file per source, named `raw/YYYY-MM-DD-short-slug.md`. You may add new
  files; you never edit or delete existing ones. They are the source of truth.
- `wiki/` — markdown pages you own entirely:
  - `wiki/sources/` — one summary page per raw source (same slug).
  - `wiki/entities/` — people, organisations, products, places.
  - `wiki/concepts/` — ideas, methods, topics.
  - `wiki/notes/` — syntheses, comparisons, answers filed back from queries.
  - `wiki/overview.md` — the big picture: what this brain knows, the main threads.
- `index.md` — catalog of every wiki page: link + one-line summary, grouped by
  category. Read it first on every query; update it on every change.
- `log.md` — append-only history, written via the `log` tool only.

## Page conventions

- File names: lowercase kebab-case, e.g. `wiki/concepts/spaced-repetition.md`.
- Start every wiki page with frontmatter:
  ```yaml
  ---
  title: Spaced Repetition
  type: concept        # source | entity | concept | note | overview
  tags: [learning, memory]
  sources: [raw/2026-04-02-anki-essay.md]
  updated: 2026-04-02
  ---
  ```
- Link pages with Obsidian wikilinks: `[[spaced-repetition]]` (file name, no folder,
  no extension). Link generously — the graph is the value.
- Cite claims back to sources: `([[2026-04-02-anki-essay]])` after the sentence.
- Keep pages focused: one entity or concept per page; split pages that sprawl.
- When a newer source contradicts an existing claim, keep both, mark the conflict
  under a `## Contradictions` heading, and say which source is newer.

## Workflows

### Ingest — a new source arrives

1. Save it verbatim to `raw/YYYY-MM-DD-slug.md` (frontmatter: `title`, `url`,
   `author`, `captured`), unless it is already there.
2. Read it fully. Tell the human the key takeaways in a few bullets and ask what to
   emphasise, unless they asked you to proceed autonomously.
3. Write `wiki/sources/<slug>.md`: summary, key claims, quotes worth keeping, links.
4. Update or create every entity and concept page the source touches (often 5–15
   pages), adding cited claims and cross-links. Update `wiki/overview.md` if the
   big picture moved.
5. Update `index.md` with every page you created or changed.
6. `log` operation `ingest` with the source title and the pages touched.

### Query — the human asks a question

1. Read `index.md`, then `search` for the key terms; read the relevant pages (and
   raw sources when the wiki is thin).
2. Answer with citations to wiki pages and sources.
3. If the answer is a reusable synthesis, file it as `wiki/notes/<slug>.md`, link it
   from related pages and `index.md`, and `log` operation `query`.

### Lint — periodic health check

Look for and fix, then `log` operation `lint` with what changed:
- contradictions between pages; stale claims superseded by newer sources;
- orphan pages (no inbound links) and pages missing from `index.md`;
- concepts mentioned repeatedly that lack their own page;
- missing cross-links and broken `[[links]]`.
Finish by suggesting open questions and sources worth adding.
