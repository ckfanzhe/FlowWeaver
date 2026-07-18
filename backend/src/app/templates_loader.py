"""Built-in workflow templates — discovery + loading.

Why this module exists
----------------------
Templates used to be a giant `_TEMPLATES: list[dict]` literal in
`main.py`. That worked when there were 4 templates; with 13+ it became
the largest single source of noise in the codebase. New templates
shipped as multi-hundred-line diffs against `main.py`, which had
nothing to do with the FastAPI app itself.

This module replaces that literal with a folder of JSON files plus a
discovery loader:

  * `backend/src/app/templates/workflows/tpl-<slug>.json` — one file
    per template. File body is the same `{id, category, envelope}`
    shape the `_TEMPLATES` literal used, just persisted instead of
    inlined. Editing a template is now a JSON edit, not a Python edit.

  * Each file declares its own `locale` (e.g. `"en"`, `"zh-CN"`).
    The default is `"en"` so legacy files stay valid. Adding a
    Chinese version of an existing template is just another JSON
    file with a different `id` and `locale: "zh-CN"` — no loader
    or schema change required.

  * `discover_templates()` — walks the folder once, parses each file,
    returns a list of template entries in **deterministic order**
    (sorted by id) so seed order is reproducible.

Adding a new template is now: drop a JSON file in the folder. No code
change required. The folder is package-shipped — `importlib.resources`
gives us the right path even from a wheel install.

Single source of truth: the JSON files. `_seed_templates()` in
`main.py` calls `discover_templates()` and never re-derives the list.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

class TemplateEntry(TypedDict):
    """The shape every JSON file in this folder must conform to.

    `envelope` is the versioned workflow envelope (schemaVersion / kind /
    exportedAt / workflow) — same shape used by user-driven JSON import
    / export and by `_seed_templates` when it inserts the row.

    `locale` (added ) is the language tag for this template,
    read from the JSON's `locale` field. Defaults to `"en"` if the
    field is absent so legacy files stay valid.
    """

    id: str
    category: str
    envelope: dict
    locale: str

# ─────────────────────────────────────────────────────────────────
# Folder location
# ─────────────────────────────────────────────────────────────────
# `__file__` is `…/src/app/templates_loader.py`; the workflow JSONs
# live in the sibling `templates/workflows/` folder. Anchoring on
# `__file__` (not `cwd`) means the loader works the same whether it's
# called from a test, from the FastAPI lifespan, or from a packaged
# install.
TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates" / "workflows"

# ─────────────────────────────────────────────────────────────────
# Discovery — load every `*.json` file in the folder
# ─────────────────────────────────────────────────────────────────
def _entry_locale(data: dict) -> str:
    """Read the `locale` field, defaulting to `"en"` for legacy files
    that pre-date the field. The loader never raises on a missing
    field — it's a noisy loud error to forget the field on a new
    template, but it should never break the startup of legacy data."""
    raw = data.get("locale")
    if isinstance(raw, str) and raw:
        return raw
    return "en"

@lru_cache(maxsize=1)
def discover_templates() -> tuple[TemplateEntry, ...]:
    """Return every built-in template, sorted by id for determinism.

    The result is `tuple` (not `list`) and `lru_cache`'d so:
      * callers can iterate without worrying about mutation,
      * repeated calls within one process are O(1),
      * tests can call `_seed_templates()` multiple times without
        re-parsing the JSON each time.

    A malformed file is logged and skipped — we don't crash the app
    because one template has a typo. `_seed_templates` separately
    re-validates each entry via `workflow_io.parse` before INSERT, so
    schema-level errors still surface at startup.
    """
    if not TEMPLATES_DIR.is_dir():
        raise FileNotFoundError(
            f"templates directory missing: {TEMPLATES_DIR}. "
            "The built-in template JSONs are package-shipped — make sure "
            "they're included in your install (e.g. include-data in pyproject)."
        )

    entries: list[TemplateEntry] = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # Skip the bad file but raise loudly enough that a test
            # / operator notices. Logged by the seed function which
            # catches the exception.
            raise ValueError(f"template file {path.name} is not valid JSON: {e}") from e
        # Schema sanity — same shape the in-code `_TEMPLATES` literal used.
        for required in ("id", "category", "envelope"):
            if required not in data:
                raise ValueError(
                    f"template file {path.name} missing required field {required!r}"
                )
        if not data["id"].startswith("tpl-"):
            raise ValueError(
                f"template file {path.name} id {data['id']!r} must start with 'tpl-'"
            )
        # Stamp the locale (with default `en`) so every entry has it.
        data = dict(data)
        data["locale"] = _entry_locale(data)
        entries.append(data)  # type: ignore[arg-type]

    # Deterministic order — by id, so seed order is stable across
    # filesystems (filename order isn't always stable).
    entries.sort(key=lambda e: e["id"])
    return tuple(entries)

def reset_cache() -> None:
    """Clear the lru_cache. Test-only — production callers should treat
    the result as immutable."""
    discover_templates.cache_clear()

# ─────────────────────────────────────────────────────────────────
# Convenience — find one template by id
# ─────────────────────────────────────────────────────────────────
def find_template(template_id: str) -> TemplateEntry | None:
    """Return the entry with the given id, or None if not found."""
    for entry in discover_templates():
        if entry["id"] == template_id:
            return entry
    return None
