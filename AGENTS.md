# AGENTS.md

This repo is the **infrastructure** for an MCP-served second brain (see README). The
brain's data lives in `BRAIN_DIR`, not here. The wiki method agents follow is the
`INSTRUCTIONS` string in `server.py`; the bookkeeping that enforces it is in `brain.py`.

- Code: `brain.py` (files, git, FTS5 search) and `server.py` (MCP tools, auth, entrypoint).
- Check: `uv run pytest -q`. For deployment changes, also `podman build .`.
- Image: `.github/workflows/image.yml` publishes `ghcr.io/jhoogstraat/second-brain` (amd64 + arm64) on every push to `main`.
- `deploy/` is one example deployment: a rootless Podman pod via Quadlet units.
  `UserNS=` can't be set on a container that joins a pod.
- FastMCP is 4.x. Check behaviour against its source in `.venv/` before relying on it.

## Git remote

Push and pull over HTTPS: `origin` is `https://github.com/jhoogstraat/second-brain.git`. Auth comes from the GitHub CLI (`gh auth git-credential`), which is already logged in. This machine has no SSH key for GitHub, so keep the remote on HTTPS.
