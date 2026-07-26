"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat_builder, llm_presets, mcp_servers, members, runtime, users, workflows
from app.config import settings
from app.core.workflow_io import WorkflowSchemaError, parse as parse_envelope
from app.db.models import Workflow
from app.db.session import init_db

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # `_seed_templates()` writes to the production DB unless told not to.
    # Tests set `AGNOBUILDER_SKIP_SEED=1` in conftest so the lifespan
    # stays out of the way; the test fixtures re-invoke the seed against
    # the in-memory DB via a monkey-patched `session_scope`.
    #
    # LLM presets are NOT seeded here. The platform's LLM config is the
    # user-managed `LlmPreset` table (Settings → LLM Models); `.env.llm`
    # is a test-only file and must not leak into production.
    import os
    skip_seed = bool(os.environ.get("AGNOBUILDER_SKIP_SEED"))
    if not skip_seed:
        _seed_templates()

    # / session: periodic cleanup of idle slim sessions. The
    # in-process `_sessions` dict would otherwise grow without
    # bound — `dispatchResetMessages` (session) only
    # drops the *active* session; abandoned sessions persist
    # until process restart. A background task ticks every
    # `CLEANUP_INTERVAL_SECONDS` and drops sessions whose
    # `last_seen_at` is older than `SESSION_TTL_SECONDS`. Tests
    # opt out via `AGNOBUILDER_SKIP_CLEANUP=1` so the test
    # fixtures don't race the background tick.
    import asyncio
    from app.runtime.session import session_store
    skip_cleanup = bool(os.environ.get("AGNOBUILDER_SKIP_CLEANUP"))
    SESSION_TTL_SECONDS = 30 * 60  # 30 min
    CLEANUP_INTERVAL_SECONDS = 60  # 1 min

    async def _cleanup_loop() -> None:
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                dropped = session_store().cleanup_idle(SESSION_TTL_SECONDS)
                if dropped:
                    log.info(
                        "session: cleanup loop dropped %d idle session(s) "
                        "past ttl=%ds",
                        dropped, SESSION_TTL_SECONDS,
                    )
            except asyncio.CancelledError:
                # Shutdown — exit cleanly so the lifespan exit
                # log doesn't surface a traceback.
                break
            except Exception:  # noqa: BLE001
                # The loop MUST survive transient errors. A bug in
                # `cleanup_idle` shouldn't kill the cleanup task
                # forever.
                log.exception("session cleanup loop iteration failed")

    cleanup_task: asyncio.Task | None = None
    if not skip_cleanup:
        cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

# ─────────────────────────────────────────────────────────────────
# Built-in templates
# ─────────────────────────────────────────────────────────────────
# Templates live as JSON files under `app/templates/workflows/` so
# adding one is a JSON edit, not a Python edit. The loader
# (`app.templates_loader.discover_templates`) walks that folder and
# returns the entries in deterministic order. `_seed_templates()`
# below syncs the DB against that list on every startup.
#
# `id` is a stable `tpl-<slug>` so the frontend can fetch by id and
# the seed is idempotent — a present `is_template=True` row blocks
# re-inserting. If a row's content has drifted from the JSON source
# (corruption from older migrations), the row is rewritten in place.
#
# `category` groups templates in the gallery (e.g. "starter",
# "branching", "loop", "tools", "complex").
# ─────────────────────────────────────────────────────────────────

from app.templates_loader import TemplateEntry, discover_templates

def _seed_templates() -> None:
    """Sync the built-in templates table with the JSON files under
    `app/templates/workflows/` on every startup.

    Templates are discovered via `app.templates_loader.discover_templates`,
    which returns every `*.json` file in the folder in deterministic
    (id-sorted) order. Adding a new template is therefore a JSON edit,
    not a Python edit — drop a file in the folder, restart, and the seed
    inserts it on the next startup.

    Behaviour:
      * For every entry discovered from the folder:
          - If no row with that id exists → INSERT.
          - If a row exists with the SAME content (nodes + edges match
            the JSON declaration) → leave it alone (cheap fast path).
          - If a row exists with DIFFERENT content → UPDATE in place.
            This is the self-heal path for rows that lost their edges
            due to an older version's overzealous migration: the row's
            `id` is the same `tpl-<slug>`, but its nodes/edges don't
            match the current declaration, so it's been corrupted and
            needs to be rewritten from the source of truth.
      * After migration, any `is_template=True` row whose id is NOT in
        the discovered list is DELETED. The JSON files are the source
        of truth — orphans accumulate when a template is retired from
        the folder without first wiping the DB, and the gallery must
        not show 14 templates when only 5 JSON files remain. Operator-
        modified rows are already overwritten on every restart by the
        per-entry UPDATE path, so the previous "don't delete orphans"
        carve-out was inconsistent with the rest of the seed.

    This replaces the previous "seed-if-empty + mutate-on-validate"
    design which (a) never recovered from a corruption, and (b) relied
    on the validator to repair edges, which it can't do safely because
    it can't distinguish "intentional legacy edge" from "corruption".

    Validation runs through `workflow_io.parse` so a malformed template
    surfaces here at startup, not later when a user tries to instantiate
    it from the gallery.
    """
    # Local import so tests can monkey-patch `app.db.session.session_scope`
    # and have it picked up here — the module-level reference would be
    # bound at import time and bypass the patch.
    from app.db.session import session_scope

    # Pre-parse each template once. A bad envelope doesn't crash the
    # seed — it logs and skips, so the rest of the gallery still loads.
    parsed: list[tuple[TemplateEntry, dict]] = []
    for entry in discover_templates():
        try:
            wf = parse_envelope(entry["envelope"])
        except WorkflowSchemaError as e:
            log.error("seed template %r failed validation: %s", entry["id"], e)
            continue
        parsed.append((entry, wf))

    with session_scope() as db:
        existing_by_id = {
            row.id: row
            for row in db.query(Workflow).filter(Workflow.is_template.is_(True)).all()
        }

        for entry, wf in parsed:
            desired_nodes = wf["nodes"]
            desired_edges = wf["edges"]
            # Locale is sourced from the JSON's `locale` field (defaults
            # to "en" in the loader). The DB column mirrors it so the
            # API can filter the gallery by language without re-reading
            # the JSON files.
            locale = entry.get("locale", "en")
            row = existing_by_id.get(entry["id"])
            if row is None:
                db.add(Workflow(
                    id=entry["id"],
                    name=wf["name"],
                    description=wf.get("description"),
                    nodes=desired_nodes,
                    edges=desired_edges,
                    is_template=True,
                    category=entry["category"],
                    locale=locale,
                ))
                log.info("seeded template %r (locale=%s)", entry["id"], locale)
                continue
            # Locale is ALWAYS reconciled to the JSON's declaration,
            # even when the row's content matches. Pre-locale-column
            # rows got `locale='en'` from the ALTER TABLE DEFAULT,
            # so a healthy row's locale may be wrong — and we want
            # the seed to fix that, not just preserve it. Without
            # this, an upgrade path that adds new Chinese templates
            # ends up with the Chinese rows tagged 'en' in the DB.
            if (row.locale or "") != locale:
                log.info(
                    "template %r locale drift (%r -> %r) — reconciling",
                    entry["id"], row.locale, locale,
                )
                row.locale = locale
                db.add(row)
            # Self-heal: if nodes or edges don't match the current
            # declaration, rewrite the row in place. The id is stable
            # so any Load-menu references survive the rewrite.
            if row.nodes != desired_nodes or row.edges != desired_edges:
                log.warning(
                    "template %r content drift — rewriting from source of truth "
                    "(was %d nodes/%d edges, now %d nodes/%d edges)",
                    entry["id"],
                    len(row.nodes or []), len(row.edges or []),
                    len(desired_nodes), len(desired_edges),
                )
                row.name = wf["name"]
                row.description = wf.get("description")
                row.nodes = desired_nodes
                row.edges = desired_edges
                row.category = entry["category"]
                row.locale = locale
                db.add(row)
        # Always run the migration too — it now only strips truly
        # incompatible edges (tool-source / loop-body-as-edge).
        _migrate_template_edges(db)

        # Garbage-collect orphans: template rows whose JSON source no
        # longer exists in the folder. Runs AFTER migration so the
        # orphan's legacy edges/nodes get cleaned up before the row is
        # removed (avoids a transient dirty row in any audit query that
        # races the seed).
        desired_ids = {entry["id"] for entry, _ in parsed}
        orphans = [
            row for row in db.query(Workflow).filter(Workflow.is_template.is_(True)).all()
            if row.id not in desired_ids
        ]
        for row in orphans:
            log.info(
                "template %r: no matching JSON — deleting orphan row",
                row.id,
            )
            db.delete(row)

def _migrate_template_edges(db) -> None:
    """Auto-repair connection-rule violations on workflow rows in place.

    The migration is idempotent and runs on every startup so existing
    rows that pre-date the connection-rules validator (and therefore
    carry tool-source edges, loop-body-as-edge, or — after the
    input/output removal — orphan input/output nodes) get cleaned up.

    Two specific cases are auto-repaired:
      * tool-source edges — `tools` / `mcp` / `http` as the source of an
        edge. These are dead in the runtime (no handler follows them)
        and now rejected by the validator. The wiring they were trying to
        express is already established via `cfg.toolsRef`, so removing
        the edge is the correct fix.
      * `loop`-body-as-edge — a loop with `cfg.bodyTarget` set AND an
        outgoing edge to the same node. Removing the edge prevents the
        body from being executed twice at runtime.
      * Legacy `input` / `output` nodes — pre-removal workflows that
        still carry an entry/exit node. The input/output semantics are
        now supplied by `Workflow.run(input=...)` and the last Step's
        output, so the dedicated nodes are dead code. We drop them
        along with any edges that touched them.

    Other violations are logged so an operator can investigate, but we
    don't auto-mutate them — touching them risks breaking a template
    the operator may have customised.
    """
    from app.core.connection_rules import validate_connections

    rows = db.query(Workflow).filter(Workflow.is_template.is_(True)).all()
    for row in rows:
        nodes = list(row.nodes or [])
        edges = list(row.edges or [])
        dirty = False

        # Backfill : legacy rows that pre-date the `locale`
        # column get `locale='en'` from the column default, so this is
        # a no-op for them. If a row somehow ended up with NULL or
        # empty, default it to `en` so the API always returns a value.
        # Runs FIRST and unconditionally so a row that's otherwise
        # healthy (no input/output nodes, no connection-rule errors)
        # still gets the backfill — the early-continue paths below
        # would otherwise skip it.
        if not getattr(row, "locale", None):
            row.locale = "en"
            dirty = True

        # 1. Drop legacy input / output nodes. Their semantics are now
        #    supplied by `Workflow.run(input=...)` and the last Step's
        #    output, so the dedicated nodes are dead code. Remove any
        #    edge that pointed at or out of them.
        legacy_ids = {n.get("id") for n in nodes if n.get("type") in ("input", "output")}
        if legacy_ids:
            nodes = [n for n in nodes if n.get("id") not in legacy_ids]
            edges = [e for e in edges
                     if e.get("source") not in legacy_ids and e.get("target") not in legacy_ids]
            log.info(
                "template %r: dropped %d legacy input/output node(s)",
                row.id, len(legacy_ids),
            )
            dirty = True

        if not edges:
            if dirty:
                row.nodes = nodes
                row.edges = []
                db.add(row)
            continue

        errors = validate_connections(nodes, edges)
        if not errors:
            if dirty:
                row.nodes = nodes
                row.edges = edges
                db.add(row)
            continue
        # Collect edge ids that should be auto-removed.
        auto_fix_codes = {"incompatibleSource", "loopBodyViaEdge"}
        bad_edge_ids = {
            e.edge_id for e in errors
            if e.code in auto_fix_codes and e.edge_id
        }
        if not bad_edge_ids:
            log.warning(
                "template %r has %d unfixed connection-rule violation(s): %s",
                row.id, len(errors),
                "; ".join(e.message for e in errors),
            )
            continue
        new_edges = [e for e in edges if e.get("id") not in bad_edge_ids]
        if len(new_edges) != len(edges) or dirty:
            log.info(
                "template %r: removed %d connection-rule-violating edge(s): %s",
                row.id, len(edges) - len(new_edges),
                ", ".join(sorted(bad_edge_ids)),
            )
            row.nodes = nodes
            row.edges = new_edges
            db.add(row)

app = FastAPI(
    title="AgnoBuilder API",
    description="Visual workflow builder for agno framework",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Expose SSE-protocol headers so the browser's fetch() lets us read
    # them from JS. Without this, `res.headers.get('x-session-id')`
    # returns null in the browser (curl ignores CORS, which is why the
    # bug stayed invisible to manual curl tests). The frontend uses
    # `X-Session-Id` to know that a confirmation message has a paired
    # paused session — without it, the chat dispatcher loops by
    # treating every "answer" as a fresh `send()` and re-entering the
    # same `human_input` pause.
    expose_headers=[
        "X-Session-Id",
        "Cache-Control",
        "X-Accel-Buffering",
        "Content-Disposition",
    ],
)

app.include_router(mcp_servers.router)
app.include_router(workflows.router)
app.include_router(members.router)
app.include_router(users.router)
app.include_router(runtime.router)
app.include_router(llm_presets.router)
app.include_router(chat_builder.router)

@app.get("/")
async def root():
    return {"name": "AgnoBuilder API", "version": "0.1.0", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/api/v1/node-types")
async def node_types():
    """Manifest-driven node-type metadata for the canvas.

    The frontend fetches this once at startup and uses it to render
    the palette, the context-menu, the chip colors in template cards,
    and the node visuals on the canvas. Everything the canvas needs
    to decide "this is an agent, in indigo, with a robot icon" is
    here.

    `input` and `output` are intentionally absent — the workflow's
    input comes from `Workflow.run(input=...)` and the output is the
    last Step's result.

    The v2 fields:

      * `kind`              — `executable` | `compound` | `tool_source`.
                              Frontend uses it to decide which form
                              fields to render and which palette group
                              to assign.
      * `extends`           — for preset types, the parent they
                              inherit from. Frontend uses it for the
                              form fallback chain (`extends: "http"`
                              → `HttpForm`).
      * `ui`                — the manifest's `ui` block verbatim:
                              `group`, `form`, `paletteOrder`. The
                              top-level `paletteOrder` field is kept
                              for backwards compat.
      * `capabilities`      — `compoundPass`, `isToolSource`,
                              `needsToolWiring`, `skipPass1`,
                              `stepWrapper`. Frontend reads these to
                              decide routing in the canvas's
                              drop-handler (e.g. a `needsToolWiring`
                              agent accepts tool-attachment edges).
      * `defaultConfig`     — the resolved default config (preset
                              inheritance applied). Lets the frontend
                              pre-populate form fields when a new
                              node is dropped on the canvas.
    """
    from app.core.node_types import NODE_TYPES, PALETTE_ORDER
    return {
        "schemaVersion": 2,
        "types": [name for name in PALETTE_ORDER],
        "entries": {
            spec.name: {
                "category": spec.category,
                "displayName": spec.display_name,
                "i18nKey": spec.i18n_key,
                "color": spec.color,
                "textColor": spec.text_color,
                "icon": spec.icon,
                "paletteOrder": spec.palette_order,
                "kind": spec.kind,
                "extends": spec.extends,
                "ui": {
                    "group": spec.ui.group,
                    "form": spec.ui.form,
                    "paletteOrder": spec.ui.paletteOrder,
                },
                "capabilities": {
                    "compoundPass": spec.capabilities.compoundPass,
                    "isToolSource": spec.capabilities.isToolSource,
                    # RAG / knowledge source — new in
                    # [[gleaming-munching-grove]]. Knowledge nodes live in
                    # `ctx.knowledge_objects` and are wired to an agent's
                    # `knowledge=...` parameter by `_pass3_knowledge_wiring`,
                    # parallel to `isToolSource` for tool attachments.
                    "isKnowledgeSource": spec.capabilities.isKnowledgeSource,
                    "needsToolWiring": spec.capabilities.needsToolWiring,
                    # RAG / knowledge — the agent side's "needs wiring"
                    # flag. Currently always false — the runtime sets
                    # `agent.knowledge = kb` post-build via
                    # `_pass3_knowledge_wiring`, no build-time kwarg
                    # needed. Surfaced here so the schema's full shape
                    # is visible to consumers.
                    "needsKnowledgeWiring": spec.capabilities.needsKnowledgeWiring,
                    "skipPass1": spec.capabilities.skipPass1,
                    "stepWrapper": spec.capabilities.stepWrapper,
                },
                "defaultConfig": dict(spec.default_config),
                # The `toolkitMethods` field was removed from the per-type
                # API response — per-preset toolkit method lists now live in
                # `app.core.strategies.tool.PRESET_REGISTRY` and are
                # surfaced through the `ToolNodeConfig.enabled_methods`
                # schema field on the `tool` node (preset discriminator).
                "io": {
                    "inputs": list(spec.io.inputs),
                    "outputs": list(spec.io.outputs),
                    "tools": list(spec.io.tools),
                },
            }
            for spec in (NODE_TYPES[n] for n in PALETTE_ORDER)
        },
    }
