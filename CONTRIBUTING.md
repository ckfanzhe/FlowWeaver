# Contributing to FlowWeaver

Thanks for your interest in contributing. This document covers the
day-to-day mechanics of submitting changes.

## Project layout

```
backend/    # FastAPI + SQLAlchemy + Python orchestration engine
frontend/   # React 18 + React Flow v12 + Zustand + Tailwind
shared/     # Cross-cutting JSON manifests (node types, connection rules)
scripts/    # Repo-level dev / CI utilities
```

## Local development

### Backend

```bash
cd backend
uv sync                  # or: pip install -e .
uv run uvicorn app.main:app --reload --port 8880
```

The backend boots an in-memory SQLite (`backend/data/dev.db` is
auto-created via `Base.metadata.create_all` on first startup). For a
real LLM, set the relevant env vars from `.env.example`.

### Frontend

```bash
cd frontend
npm install
npm run dev               # vite dev server on port 5173
```

The frontend reads `VITE_API_BASE` (defaults to `http://localhost:8880`).

### Tests

```bash
cd backend && pytest tests/ --ignore=tests/test_agno_native_steps.py
```

The `test_agno_native_steps.py` suite requires live LLM credentials
and is excluded from the default run.

## Commit convention

We follow [Conventional Commits](https://www.conventionalcommits.org/) with
optional scope:

```
feat(runtime): add cross-restart session persistence
fix(event-adapter): surface LLM errors via RunErrorEvent
docs: README rewrite for FlowWeaver branding
chore(release): drop internal docs
```

Subject line ≤ 72 chars. Wrap the body at ~72 cols. Use the imperative
mood ("add", not "added"). Reference the issue / spec section in the
footer when relevant.

## Code style

- Python: `ruff format` + `ruff check`. Imports sorted with `isort`.
- TypeScript: `tsc --noEmit`, `eslint .`, `prettier --write`.
- Backend tests follow `tests/test_*.py` naming; test classes group
  related cases (see `tests/test_runtime_api.py::TestBuildHttpFunction`).
- Frontend tests follow `*.test.ts` co-location; prefer `tsx --test`
  for unit tests; React Testing Library for component tests.

## Pull requests

1. Branch from `main`: `git checkout -b feat/<short-topic>`.
3. Keep changes scoped — one concern per PR. If a commit fixes two
   unrelated bugs, split into two PRs.
4. Ensure `pytest tests/ --ignore=tests/test_agno_native_steps.py`
   passes locally before pushing.
5. Reference the relevant section / file in the PR description. The
   team reviews for: API contract, test coverage, doc impact, and
   consistency with existing patterns.

## Reporting issues

Open a GitHub issue. For security vulnerabilities, **do not** file a
public issue — email `security@flowweaver.dev` (placeholder; replace
when the project has a security policy).

## License

By contributing, you agree that your contributions will be licensed
under the MIT License (see [`LICENSE`](LICENSE)).