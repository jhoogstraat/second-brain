<img src="assets/icon.svg" width="64" height="64" alt="">

# second_brain

The infrastructure for a personal **second brain** in the style of Andrej Karpathy's
[LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). It runs
on a Raspberry Pi, and any MCP harness can reach it over the internet: Claude, ChatGPT,
Codex, Claude Code, and others.

This repo is only the system. The brain itself is a separate git repo of markdown
files that lives in a Podman volume on the Pi.

```
Claude / ChatGPT / Codex ──HTTPS──▶ Cloudflare ──tunnel──▶ Pi: [ cloudflared │ second-brain MCP ] ──▶ volume: brain git repo
```

## How it works

In Karpathy's method the LLM, not you, writes and maintains a wiki compiled from your
sources. Knowledge then compounds instead of being re-derived on every RAG query. The
brain has three layers:

| Path | What it is |
|---|---|
| `raw/` | Immutable source documents. The LLM can add new ones but never edits existing ones. |
| `wiki/` | Pages the LLM writes: source summaries, entities, concepts, notes, `overview.md`. |
| `AGENTS.md` | The schema. It holds the conventions and the **ingest / query / lint** workflows, and you and the LLM evolve it together. |
| `index.md`, `log.md` | The catalog of every page, and the append-only history. |

On first start the server seeds this layout from [`src/second_brain/template`](src/second_brain/template).

**MCP surface:**
- **Tools:** `read`, `list`, `search`, `write`, `delete`, `log`.
- **Prompts:** `ingest`, `query`, `lint`.
- **Instructions:** tell every client to read `AGENTS.md` before its first change in a conversation.

Every change becomes a git commit, so any edit can be reverted.

**Search** is BM25 using SQLite FTS5. Karpathy found that an index file plus brief
summaries is enough up to hundreds of pages, and that you don't need embedding-based RAG
at that scale. If the wiki outgrows this, move to hybrid vector search (e.g.
[qmd](https://github.com/tobi/qmd)).

**Auth** is OAuth 2.1, which is the only scheme claude.ai and ChatGPT accept:
- FastMCP's GitHub OAuth proxy provides DCR, CIMD and PKCE.
- It only accepts the GitHub logins (or numeric user IDs, which survive renames) listed in `GITHUB_ALLOWED_USERS`.
- CLIs can use a static bearer token (`BRAIN_TOKEN`) instead.
- Don't put Cloudflare Access in front of it. Remote clients like claude.ai can't complete its browser login.

## Deploy on the Pi

You need a 64-bit Raspberry Pi OS (Trixie, which ships Podman 5), a domain on
Cloudflare, and a GitHub account.

1. **Install Podman and git.** Clone this repo onto the Pi:
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
4. **Log in to the registry.** GitHub Actions builds the image
   (`ghcr.io/jhoogstraat/second-brain`, arm64 + amd64) on every push to `main`. The repo
   is private, so the image is too. Create a classic token with `read:packages`, then:
   ```sh
   podman login ghcr.io -u jhoogstraat
   ```
5. **Configure and start.** The first run creates two files in `~/.config/second-brain/`:
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

## Browse it in Obsidian (optional)

Karpathy uses Obsidian as the IDE for reading the wiki. To get a copy, mirror the brain
to a private GitHub repo. Use a fine-grained token that only has *Contents: read/write*
on that repo.

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
