# AgnoBuilder backend — Python 3.12 + uv-managed dependencies.
#
# Layering strategy:
#   1. Install uv + system deps (cached unless uv version changes).
#   2. Copy ONLY pyproject.toml + uv.lock first, run `uv sync` so the
#      dependency layer caches independently of source changes.
#   3. Copy src/ + shared/ last — code edits don't invalidate the dep
#      cache. The `shared/` dir carries the cross-stack
#      `nodes.manifest.json` + `connection_rules.json` consumed by the
#      runtime's `_load_manifest_path()` resolver.
#
# Build context: project root (see `docker-compose.yml::backend.build`)
# — the Dockerfile lives under `backend/` but uses `backend/` prefix
# in COPY paths so compose's `.` context still resolves correctly.

FROM python:3.12-slim AS base

# uv (single-binary Python package manager) — installed at the version
# pinned by the team. `uv sync --frozen` later requires uv.lock to be
# present and consistent.
COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /uvx /usr/local/bin/

# curl is needed for the healthcheck (and `uv` shells out to fetch
# Python interpreters on first sync when not pinned to a specific
# interpreter build). `ca-certificates` keeps HTTPS working for the
# initial Python download.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─────────────────────────────────────────────────────────────────
# Layer 2: dependency install. Re-runs only when pyproject.toml or
# uv.lock changes — the bulk of the image size lives here.
# ─────────────────────────────────────────────────────────────────
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

# ─────────────────────────────────────────────────────────────────
# Layer 3: source. Touched most often in dev — sits on top of the
# dep layer so unrelated code edits skip re-downloading.
# `shared/` sits next to the venv root so the runtime's
# `Path(__file__).parent.parent.parent.parent.parent / "shared"`
# resolution (see `node_types.py::_SHARED_DIR`) finds it at
# `/app/shared/` → `/shared/nodes.manifest.json` matches the
# resolver's expectation once it walks up the path tree.
# ─────────────────────────────────────────────────────────────────
COPY backend/src ./src
# `backend/templates/` carries the Jinja headers (`workflow.py.jinja`)
# that `app.core.compile.serialize._env()` resolves via
# `Path(__file__).resolve().parents[4] / "templates"` → `/app/templates`.
# Without this COPY the runtime raises `TemplateNotFound` whenever a
# user tries to export a workflow as Python (see GitHub issue log
# "export_workflow 500 — workflow.py.jinja not found"). `../shared`
# is sibling to `backend/templates/` in the repo layout, but the
# source-emit code only reads templates so we COPY it explicitly
# rather than relying on a wildcard.
COPY backend/templates ./templates
COPY shared ../shared

# `uvicorn` lives in the venv uv created at /app/.venv. Putting it on
# PATH lets `CMD ["uvicorn", ...]` resolve without `uv run`.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8880

# `--app-dir src` matches `start.sh` — uvicorn imports `app.main:app`
# by looking for an `app/` package under src/. `--proxy-headers` makes
# the loopback healthcheck work behind compose's port-mapping.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8880", \
     "--app-dir", "src", "--proxy-headers"]