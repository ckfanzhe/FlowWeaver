# FlowWeaver

[![GitHub stars](https://img.shields.io/github/stars/ckfanzhe/FlowWeaver?style=social)](https://github.com/ckfanzhe/FlowWeaver/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)


FlowWeaver is an open-source visual builder for [agno](https://github.com/agno-agi/agno)-style agent workflows. Drag-and-drop nodes onto a canvas, wire data flow + tool attachments, run multi-turn workflows in a chat panel, and export the whole thing as a standalone Python file that depends only on agno. Inspired by community projects like **Agnobuilder**, FlowWeaver focuses on a single-engine runtime, declarative node-type definitions, and a friendly chat-driven creation surface.

<p align="center">
  <img src="./docs/images/image.png" alt="FlowWeaver canvas — visual workflow builder with chat panel" width="900" />
</p>

## Features

- **Visual orchestration** — React Flow canvas with 6 node kinds (`agent`, `ask`, `branch`, `flow`, `loop`, `tool`) covering executable, control-flow, compound, and tool-source shapes.
- **Tool attachments** — wire HTTP / MCP / function / preset tools to agents via the canvas; the runtime injects them at compile time.
- **Multi-turn chat runtime** — persistent session store with cross-restart durability, HITL (human-in-the-loop) pause/resume, configurable history window, and a chat-builder API that turns natural-language prompts into workflow edits.
- **Declarative node types** — every node is one JSON manifest entry + one strategy class. Adding a node is ~3 files.
- **Python export** — every workflow compiles to a standalone `.py` file that uses only `agno` + stdlib. The export pipeline is byte-stable across runs.
- **Single-engine runtime** — one agno workflow per run. No parallel state machines, no mirror channels, no dual-runtime reconciliation.

## Status

First open-source release. The runtime, IR, serializer, event adapter, chat builder, and the 6-node-type catalog are stable and exported to PyPI-compatible Python. v1.5 includes persistence across process restart.

## Quick start

The platform is Postgres-only (SQLite was dropped in v1.5). For a self-contained Postgres + backend + frontend stack, prefer the Docker Compose path below. For local dev:

```bash
# 1. Bring up Postgres (one of):
docker compose up -d postgres           # compose-managed Postgres
# OR a local Postgres on 127.0.0.1:5432 with user=agnobuilder db=agnobuilder

# 2. Backend (port 8880)
cd backend
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8880 --app-dir src --reload

# 3. Frontend (port 5173) — in another terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The first-time flow has the user identify via email (no password — FlowWeaver is for internal-network deployment).

## Docker Compose (one-shot deployment)

For a fully containerised stack (Postgres + FastAPI + Vite preview frontend), copy the example env file and run compose:

```bash
cp .env.example .env          # fill in any provider keys if needed
docker compose up --build      # builds 3 images + starts the stack
```

**`.env` is optional.** All compose variables have built-in defaults (`${POSTGRES_USER:-agnobuilder}` etc.), so `docker compose up` works without a `.env` file — you'll just see two harmless warnings at startup:

```text
WARN[...] The "POSTGRES_USER" variable is not set. Defaulting to a blank string.
env file .../.env not found
```

Silencing them: `cp .env.example .env` and (optionally) edit `POSTGRES_PASSWORD` to something other than the demo default. The warnings disappear once the file exists.

**Permission denied at the docker socket?** If `docker compose up` errors with `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`, your user isn't in the `docker` group. See the [Troubleshooting](#troubleshooting-docker-permission-denied) section below.

### Internal-network / LAN deployment

The compose stack is configured for **internal-network deployment** by default — see [[agnobuilder-internal-only]] for the security model. Out of the box:

- **Backend** binds `0.0.0.0:8880` (already LAN-reachable).
- **Frontend (vite preview)** binds `0.0.0.0:4173`.
- **`VITE_API_BASE`** is left empty in `docker-compose.yml` — the JS bundle auto-detects via `window.location.hostname` so the same image works on any host the user types in their browser (`http://192.168.1.10:4173`, `http://mybox.local:4173`, etc.) without rebuilding.
- **`AGNOBUILDER_CORS_ORIGINS=*`** enables wildcard CORS for the trusted internal network (the platform's `identify` flow uses a header token, not a cookie, so disabling `allow_credentials` alongside the wildcard doesn't break auth).

Workflow for a multi-machine LAN deploy:

```bash
# 1. One machine runs the stack
docker compose up -d --build
# Find the host's LAN IP (e.g. `192.168.1.10`)

# 2. From any other machine on the same LAN, open the browser to:
#    http://192.168.1.10:4173
#    The frontend auto-detects the hostname and calls the backend at
#    http://192.168.1.10:8880 — same build, no extra config.

# 3. If the LAN has a DNS name (e.g. `mybox.local`), browsers on
#    other machines can use that instead of the IP:
#    http://mybox.local:4173  →  API at http://mybox.local:8880
```

For production-public deploys, tighten `AGNOBUILDER_CORS_ORIGINS` to an explicit list:

```bash
AGNOBUILDER_CORS_ORIGINS=https://app.example.com
```

The `docker-compose.yml` `frontend.build.args.VITE_API_BASE` field can be set to a fixed URL when running behind a reverse proxy that serves frontend + backend on different hostnames (e.g. `https://api.example.com` for the API while the frontend lives at `https://app.example.com`). Leave empty for the LAN default.

### Host port mapping

`docker-compose.yml` exposes backend on `8880` and frontend on `4173`. If those collide with other services on the host, change the left side of the `:8880` and `:4173` port mappings (the right side is the in-container port — don't change it unless you also change the frontend `preview.port` + the auto-detect's `8880` constant in `api/client.ts`).

Once the healthchecks turn green:

| Service | URL | Notes |
| --- | --- | --- |
| Frontend | http://localhost:4173 | Vite preview serving the static bundle |
| Backend | http://localhost:8880 | FastAPI; `GET /health` → `{"status":"healthy"}` |
| Postgres | (internal only) | Data persists in the `pgdata` named volume |

`docker compose down` stops and removes the containers (data stays in `pgdata`). `docker compose down -v` also wipes the database.

The compose stack uses **Postgres** — same as the local `start.sh` path (Postgres-only since v1.5; SQLite was dropped). `start.sh` reads `AGNOBUILDER_DATABASE_URL` from `.env` and expects a Postgres reachable at that URL; compose supplies the URL via the `postgres` service in `docker-compose.yml`. LLM credentials are still configured via the in-app Settings → LLM Models panel after first start (the `.env` file is not read for credentials by the runtime).

`./start.sh` and `docker compose up` are independent paths — pick one. Use `start.sh` for hot-reload local development, `docker compose up` for a self-contained stack (demo deploys, CI, internal-network rollout).

## Configuring an LLM provider

The runtime reads LLM credentials from the **`LlmPreset` table in the database**, not from `.env` files. Add your provider after the first backend start:

1. Open the app, click the **user menu** (top right) → **Settings**.
2. Open the **LLM Models** tab → **Add preset**.
3. Pick a provider (OpenAI / Anthropic / Google / xAI / OpenAI-compatible), paste your API key, set the model id (e.g. `claude-sonnet-4-5`, `gpt-4o`, `Qwen3-8B`), and Save.
4. Mark the preset as **default** so the runtime uses it for all agents.

The `.env.example` file at the repo root is **only useful for the `real_llm_preset` test fixture** (runs the integration test suite against a real provider). It is NOT read by the runtime API. Do not paste production keys into `.env` expecting them to take effect — the runtime ignores that file for credentials.

For a local vLLM setup, run vLLM on port 8000, then add a preset in the UI:

```text
Provider: OpenAI-compatible
Base URL: http://localhost:8000/v1
API key:  EMPTY                # vLLM ignores this by default
Model:   Qwen3-8B
```

## How it fits together

```
┌──────────────┐     IR + manifest      ┌──────────────────┐
│   Canvas     │  ◀──────────────────▶  │  Python export   │
│ (React Flow) │   drag / drop / wire   │ (Python source)  │
└──────┬───────┘                        └──────────────────┘
       │ SSE
       ▼
┌──────────────┐  agno 2.x workflow    ┌──────────────────┐
│  Runtime API │  ──────────────────▶  │ Postgres + sessions │
│   (FastAPI)  │   event-stream SSE     │  (SQLAlchemy)    │
└──────────────┘                        └──────────────────┘
       ▲
       │ chat-builder API
       │
┌──────────────┐
│  Chat panel  │  natural-language edits → staged diffs → apply
└──────────────┘
```

A single workflow round-trip:

1. The canvas produces an IR (intermediate representation) of nodes + edges.
2. The IR compiles into an `agno.workflow.Workflow` (the only runtime engine).
3. `Wf.run(...)` streams events back to the SSE consumer.
4. The chat panel renders each event as a typed bubble (text / tool_call / tool_result / confirmation / completed / error).
5. `Wf.continue_run(...)` resumes after a HITL gate.

## Adding a node type

The dispatch plumbing (pipeline, tool factories, serializer) is registry-driven and doesn't need to be touched.

```bash
# 1. Edit shared/nodes.manifest.json — add your entry (or `extends: "http"` for a preset).
# 2. (Only if the type is new) Add the strategy class + form component.
# 3. Regenerate the TS union so typecheck stays green:
python scripts/generate_node_types.py
```

See [`shared/nodes.manifest.json`](shared/nodes.manifest.json) for the canonical manifest schema.

## Project layout

```
backend/    # FastAPI + SQLAlchemy + Python orchestration engine
frontend/   # React 18 + React Flow v12 + Zustand + Tailwind
shared/     # Cross-cutting JSON manifests (node types, connection rules)
scripts/    # Repo-level dev / CI utilities
```

## Architecture notes

- **Single-engine runtime** — every run goes through one agno workflow. No parallel state machines, no mirror channels.
- **Declarative node types** — every node is one JSON manifest entry + one strategy class. Adding a node is ~3 files.
- **Two-tier session store** — in-memory hot cache + Postgres cold store. The session survives process restart; cross-restart pause-state surfaces a clean 409 with a re-trigger hint.
- **Chat builder** — natural-language edits flow through a staged diff + strict validation pipeline. Apply is atomic; rollback is automatic on validation failure.

## Troubleshooting

### `docker compose up`: `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`

The current shell user is not in the `docker` group, so the Docker CLI can't reach the daemon socket. Fix once per machine:

```bash
# 1. Add yourself to the docker group (requires sudo).
sudo usermod -aG docker "$USER"

# 2a. Either log out + log back in (permanent — applies to new shells), or
# 2b. open a brand-new login shell (e.g. `newgrp docker` in the current one).

# 3. Verify — should print the docker version, NOT error.
docker ps
```

Linux distro notes:

- **Ubuntu / Debian**: the docker daemon install typically creates the `docker` group automatically; `usermod -aG docker $USER` is enough.
- **Fedora / RHEL / CentOS Stream**: same as Ubuntu; `docker` group is created by the Docker CE RPM.
- **Arch / Manjaro**: same; package `docker` ships the group.
- **WSL2 (Windows host with Docker Desktop)**: Docker Desktop auto-configures the WSL distro's docker group; if `docker ps` still errors, restart Docker Desktop and reopen the WSL terminal.
- **Raspberry Pi OS / Debian ARM**: same; ensure `sudo` is installed.

If `docker ps` still errors post-reboot, check group membership with `id "$USER"` — `docker` should be listed. If not, repeat step 1; if it is and still fails, your Docker daemon socket may live at a non-default path — set `DOCKER_HOST` per the Docker docs.

## License

[MIT](./LICENSE) — see [`LICENSE`](LICENSE) for the full text.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, code style, and the conventional-commits format this repo uses.

## Languages

- [English](./README.md)
- [中文](./docs/README.zh-CN.md)