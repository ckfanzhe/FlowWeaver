# AgnoBuilder frontend — Vite static build + Vite preview server.
#
# Two stages:
#   1. `build`  — npm ci (deps) + tsc -b (typecheck) + vite build (bundle).
#                 VITE_API_BASE is a *build-time* variable so we pass it
#                 via `ARG` here; `docker-compose.yml::frontend.args`
#                 supplies the value when building.
#   2. `runtime` — node:22-alpine with the bundled `dist/` and Vite CLI.
#                  `npx vite preview` serves `dist/` over plain HTTP —
#                  no nginx needed for the demo path. Production users
#                  can swap this stage for an `nginx:alpine` image.
#
# Build context: `./frontend` (see `docker-compose.yml::frontend.build`).

# ─────────────────────────────────────────────────────────────────
# Stage 1: build
# ─────────────────────────────────────────────────────────────────
FROM node:22.23.2-slim AS build

WORKDIR /app

# Build-time arg — Vite reads `import.meta.env.VITE_API_BASE` at build
# time and bakes the value into the JS bundle. Changing it later
# requires a rebuild (this is Vite's standard behaviour).
ARG VITE_API_BASE=http://localhost:8880
ENV VITE_API_BASE=$VITE_API_BASE

# Cache npm install layer separately from source. `npm ci` enforces
# the lockfile; `--no-audit --no-fund` silences noisy output.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Source last so unrelated edits don't bust the npm cache layer.
COPY . .

# `tsc -b && vite build` is what `npm run build` does (per package.json);
# calling it directly skips npm's PATH lookup so the error message
# points at the actual failing tool.
RUN npm run build

# ─────────────────────────────────────────────────────────────────
# Stage 2: runtime — serve `dist/` via `vite preview`
# ─────────────────────────────────────────────────────────────────
FROM node:22.23.2-slim AS runtime

WORKDIR /app

# Same lockfile → same node_modules tree at runtime.
#
# Note: we deliberately install the FULL dependency set (no `--omit-dev`)
# because `vite preview` is shipped as a devDependency in `package.json`
# (matches the standard Vite scaffold). Using `--omit=dev` here would
# strip the `vite` CLI from `node_modules/.bin/`, and `CMD ["npx", ...]`
# would then fetch `vite` on the fly (a fresh `vite@8.x` per container
# start — noisy warning, slow cold start, non-deterministic). The few
# extra MB of devDeps are the simpler trade than pinning + reinstalling
# vite explicitly. Switch to an nginx-based static stage when the image
# size starts to matter.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Static bundle + node_modules (vite CLI lives in node_modules/.bin/).
COPY --from=build /app/dist ./dist

# `vite preview` defaults to :4173. `--host 0.0.0.0` makes it listen
# on all, which is what compose's port mapping needs. `--strictPort`
# exits non-zero on bind failure (so compose healthcheck can detect
# startup problems).
ENV PORT=4173
EXPOSE 4173

CMD ["npx", "vite", "preview", \
     "--host", "0.0.0.0", "--port", "4173", "--strictPort"]