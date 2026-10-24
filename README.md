<img src="assets/icon.svg" width="64" height="64" alt="">

# second_brain

The infrastructure for a personal **second brain** in the style of Andrej Karpathy's
[LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). It is
one HTTP server, so run it wherever you like. Once it has a public HTTPS URL, any MCP
harness can reach it: Claude, ChatGPT, Codex, Claude Code, and others.

This repo is only the system. The brain itself is a separate git repo of markdown
files in `BRAIN_DIR` (`/data/brain` in the image).

```
Claude / ChatGPT / Codex ──HTTPS──▶ second-brain MCP (:8000) ──▶ BRAIN_DIR: brain git repo
```

## How it works

In Karpathy's method the LLM, not you, writes and maintains a wiki compiled from your
sources. Knowledge then compounds instead of being re-derived on every RAG query.

The server keeps the agent's job down to judgment: what to write, what to link, what
conflicts. It does all the bookkeeping itself:

| Agent decides | Server does |
|---|---|
| A page's title, kind, one-line summary and body | Path (`wiki/<kind>/<slug>.md`), frontmatter, created/updated dates |
| `[[Title]]` links | Normalises them to file names, reports links to missing pages, adds backlinks when a page is read |
| Which sources a page draws on | Validates them; sources in `raw/` are immutable |
| — | Regenerates `index.md`, appends to `log.md`, makes a git commit for every change |
| Fixing what `lint` reports | `lint` finds broken links, orphans, pages without sources, uncited sources |

The method itself sits in the server instructions every client receives on connect, so
there is no schema file to read first. Rules specific to your brain (language, how to
handle recipes, ...) go on a `Conventions` page, which the index shows at the top.

On disk the brain is `raw/` (sources), `wiki/<kind>/` (pages), `index.md` and `log.md`.
An Obsidian vault opens it as is.

**MCP surface:**
- **Tools:**
  - `read`: a page by title, a source, `index` or `log`.
  - `search`
  - `save_source`
  - `write_page`
  - `delete_page`
  - `lint`
- **Resources:** `brain://index.md`, plus `brain://{path}` for any file.
- **Prompts:** `ingest`, `query`, `lint`. Each one repeats the method, for clients that drop server instructions.

**Search** is BM25 using SQLite FTS5. Karpathy found that an index file plus brief
summaries is enough up to hundreds of pages, and that you don't need embedding-based RAG
at that scale. If the wiki outgrows this, move to hybrid vector search (e.g.
[qmd](https://github.com/tobi/qmd)).

**Auth** is OAuth 2.1, which is the only scheme claude.ai and ChatGPT accept:
- FastMCP's GitHub OAuth proxy provides DCR, CIMD and PKCE.
- It only accepts the GitHub logins (or numeric user IDs, which survive renames) listed in `GITHUB_ALLOWED_USERS`.
- CLIs can use a static bearer token (`BRAIN_TOKEN`) instead.
- Don't put a proxy that requires its own browser login (e.g. Cloudflare Access) in front of it; the clients' OAuth discovery can't get past it.

## Run it

The server needs:
- **Config** from environment variables: see [`deploy/env.example`](deploy/env.example). `HOST` and `PORT` default to `0.0.0.0:8000`.
- **Persistent storage** for `/data` (the brain repo and OAuth state), or set `BRAIN_DIR` and `FASTMCP_HOME` elsewhere.
- **A public HTTPS URL** in front of port 8000, set as `PUBLIC_URL`.

The image `ghcr.io/jhoogstraat/second-brain` is published for amd64 and arm64, tagged
`latest` and `sha-<commit>`. You can also run it without a container via
`uv run second-brain`.

### Example: Podman and Cloudflare Tunnel

[`deploy/`](deploy) runs it as a rootless Podman pod with a Cloudflare tunnel, so the
host needs no open ports. You need a Linux host with Podman 5 (e.g. Debian Trixie), a
domain on Cloudflare, and a GitHub account.

1. **Install Podman and git.** Clone this repo onto the host (Debian shown):
   ```sh
   sudo apt install -y podman git
   git clone https://github.com/jhoogstraat/second-brain.git && cd second-brain
   ```
2. **Create the tunnel.**
   - In Cloudflare Zero Trust, go to *Networks → Tunnels → Create a tunnel*, choose *Cloudflared*, and copy the token.
   - Add a public hostname, e.g. `brain.example.com`, with service **`http://localhost:8000`**.
   - Keep WAF challenges, Bot Fight Mode and Access off for this hostname, or OAuth discovery from Claude and ChatGPT will fail.
3. **Create the GitHub OAuth App** at github.com → Settings → Developer settings → OAuth Apps:
   - Homepage: `https://brain.example.com`
   - Callback: `https://brain.example.com/auth/callback`
4. **Configure and start.** The script pulls the image (built by GitHub Actions on every push to `main`). Its first run creates two files in `~/.config/second-brain/`:
   `env` for the app's secrets ([template](deploy/env.example)) and `tunnel.env` for the
   tunnel token alone, so the cloudflared image never sees the app's secrets. Fill both
   in, then run the script again:
   ```sh
   ./deploy/install.sh   # creates env + tunnel.env
   nano ~/.config/second-brain/env ~/.config/second-brain/tunnel.env
   ./deploy/install.sh   # pulls the image, installs the Quadlet units, starts the pod
   ```
   Verify with `curl https://brain.example.com/health`, which should print `ok`.

Everything runs as rootless systemd user services, so it starts on boot and restarts on
failure. Check logs with `journalctl --user -u second-brain -f`. `podman-auto-update`
pulls new images and restarts every day. To apply an update now, run `podman auto-update`.
After changing anything in `deploy/`, `git pull` and run `./deploy/install.sh` again.

## Connect clients

The MCP URL is `https://brain.example.com/mcp`. OAuth clients open a GitHub login the
first time they connect.

| Client | How to connect |
|---|---|
| **Claude** (web, desktop, mobile) | Settings → Connectors → *Add custom connector* → paste the URL. |
| **ChatGPT** | Settings → Apps & Connectors → Advanced → enable Developer mode → *Create* → paste the URL, auth *OAuth*. |
| **Claude Code** | `claude mcp add --transport http brain https://brain.example.com/mcp`, then `/mcp` to log in. To use the token instead, add `--header "Authorization: Bearer $BRAIN_TOKEN"`. |
| **Codex** | `codex mcp add brain --url https://brain.example.com/mcp`, then `codex mcp login brain`. Or set `bearer_token_env_var = "BRAIN_TOKEN"` under `[mcp_servers.brain]` in `~/.codex/config.toml`. |

Then just talk to it, for example *"Ingest this article: <url>"*, *"What do I know
about X?"* or *"Lint the brain."* The MCP prompts `ingest`, `query` and `lint` do the
same thing.

## Start over

`second-brain reset` moves the brain to a timestamped backup next to it (e.g.
`/data/brain.20261024-101500.bak`) and seeds a fresh one. With the Podman example:

```sh
podman exec second-brain second-brain reset
```

Delete the backup folder from the volume once you're sure you don't need it.

The fresh brain is a new git repo with no remote, so if you push to a mirror
(`BRAIN_GIT_PUSH=1`), add the remote again as described below.

## Browse it in Obsidian (optional)

Karpathy uses Obsidian as the IDE for reading the wiki. To get a copy, mirror the brain
to a private git repo. On GitHub, use a fine-grained token that only has
*Contents: read/write* on that repo. Add the remote to the brain repo, push once, then
set `BRAIN_GIT_PUSH=1` and restart the server. With the Podman example:

```sh
podman exec second-brain git -C /data/brain remote add origin https://x-access-token:<TOKEN>@github.com/you/brain-data.git
podman exec second-brain git -C /data/brain push -u origin main
echo BRAIN_GIT_PUSH=1 >> ~/.config/second-brain/env && systemctl --user restart second-brain
```

Every change is now pushed. Clone the mirror into Obsidian to read it. The server
doesn't pull changes, so make edits through an LLM client, not in Obsidian.

## Develop

```sh
uv run pytest -q                                   # tests
BRAIN_DIR=/tmp/brain BRAIN_NO_AUTH=1 uv run second-brain   # local server on :8000/mcp
```
