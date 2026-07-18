"""Tool-source node → agno tool factory.

The public `build_tools_for_node(...)` is a 3-line dispatcher that
hands off to the strategy registered for the node's type. The
actual tool construction lives in the per-type strategy subclasses
(`app.core.strategies.http.HttpToolStrategy.build_tools`, etc.).

Why the split:

  * The strategies own the BEHAVIOUR (when does a node produce
    tools? Which strategy class? What's its kind?). Putting the
    `build_tools` implementations next to the corresponding `build`
    / `to_source` keeps all the type-specific logic in one place.
  * `tool_factories` owns the SHARED HELPERS that two or more
    strategies need (`_SAFE_BUILTINS` for the `tools` sandbox,
    `_do_request` for HTTP body handling). These don't belong to
    any one type — they're platform-level utilities.
  * Adding a new tool-source type means dropping a strategy class
    with `build_tools()` and a manifest entry whose `kind` is
    `tool_source` — no edits here.

Back-compat
-----------

`build_tools_for_node(node, ir_nodes, *, user_id=None)` keeps its
public signature. The legacy handlers in `tool_handlers.py` are
preserved unchanged for direct unit tests (`test_tool_handlers.py`).
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from typing import Any

from app.core.ir import IRNode
from app.core.node_types import NODE_TYPES
from app.core.compile.errors import CompileError

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Public entry point — strategy dispatcher (3 lines of business logic)
# ─────────────────────────────────────────────────────────────────
def build_tools_for_node(
    node: IRNode,
    ir_nodes: dict[str, IRNode],
    *,
    user_id: str | None = None,
) -> list[Any]:
    """Build the agno tool list for one tool-source node.

    Dispatches to `NODE_TYPES[node.type].strategy.build_tools(...)`.
    Returns `[]` (not raises) when:

      * the node type isn't a tool-source strategy
      * the node's config is missing required fields
      * the strategy's `build_tools` returns `None`

    The agent is allowed to run without an attached tool, and we'd
    rather have a partial workflow than a hard crash at build time.
    """
    spec = NODE_TYPES.get(node.type)
    if spec is None:
        return []
    strategy = spec.strategy
    if not strategy.IS_TOOL_SOURCE:
        return []
    return list(
        strategy.build_tools(node.id, node, ir_nodes, user_id=user_id) or []
    )

# ─────────────────────────────────────────────────────────────────
# Shared helpers — used by strategy implementations, NOT by callers
# ─────────────────────────────────────────────────────────────────
# `tools` strategies share the same safe-builtins policy. `http`
# strategies share the `_do_request` body. Keeping these helpers
# here (and not on each strategy) means a single change updates all
# tool-source types at once.

# ─────────────────────────────────────────────────────────────────
# Preset toolkits
# ─────────────────────────────────────────────────────────────────
# Built-in presets like `tavily_search` / `duckduckgo` / `calculator`
# / `arxiv_search` need access to agno's `Toolkit` subclasses
# (`agno.tools.tavily.TavilyTools`, etc.). The user-functions path
# runs code through the safe-builtins sandbox (no `__import__`), so
# we can't reuse it. Instead we instantiate the toolkit directly
# here and wrap its methods with `Function.from_callable(...)`.
#
# The prior `extends: "tool"` preset manifest entries were collapsed
# into a single `tool` node with a `preset` config discriminator.
# Per-preset metadata (toolkit_class + toolkit_methods + default_config)
# now lives in `app.core.strategies.tool.PRESET_REGISTRY` — the manifest
# is no longer the SoT for toolkit_class (it never was for runtime;
# the manifest only carried metadata for display). The dispatch path:
#
#   1. `ToolStrategy.build_tools()` sees `cfg.preset` set
#   2. Calls `build_toolkit_for_preset(nid, spec, ir_node)` here with
#      the resolved `PresetSpec` from `PRESET_REGISTRY`
#   3. Returns a `Function.from_callable(...)` per declared method
#
# Bypassing the safe-builtins sandbox is safe for **preset** toolkits
# because the toolkit code is shipped with the platform — it's not
# user input. The user-functions sandbox still applies to the `tools`
# node type proper (untouched by P2).
def build_toolkit_for_preset(
    nid: str,
    preset_spec: Any,  # `app.core.strategies.tool.PresetSpec` (forward-ref to avoid cycle)
    node: IRNode,
) -> list[Any]:
    """Build `Function.from_callable(...)` instances for a toolkit preset.

    The preset is now identified by `cfg.preset` (not `node.type`);
    the dispatcher in `ToolStrategy.build_tools` looks the name up in
    `PRESET_REGISTRY` (in `app.core.strategies.tool`) and passes the
    resolved `PresetSpec` here. This function only handles the
    toolkit-class presets (`tavily_search` / `duckduckgo` /
    `calculator` / `arxiv_search`); the wikipedia preset falls
    through to the HTTP path in `ToolStrategy` (no toolkit class to
    instantiate).

    Reads `enabled_methods` / `toolkit_options` from `node.data.config`
    to filter / parameterize the toolkit instantiation. Each declared
    method becomes one `Function` exposed to the agent.

    Returns `[]` (and logs a warning) when:
      - the toolkit class can't be imported
      - instantiation fails (e.g. missing API key for a paid toolkit)
      - any declared method is missing from the toolkit instance

    Returns the list of constructed `Function` instances otherwise.
    The agent's `tools=[...]` list is the union across all attached
    tool-source nodes, same as `HttpToolStrategy` / `McpToolStrategy`.
    """
    from agno.tools.function import Function

    toolkit_class_path = preset_spec.toolkit_class
    method_names = list(preset_spec.toolkit_methods or [])

    # Dynamic import of the toolkit class. The path comes from
    # `PRESET_REGISTRY` so the platform ships the preset definition;
    # this code path only resolves shipped class names.
    try:
        module_path, _, class_name = toolkit_class_path.rpartition(".")
        if not module_path:
            raise ImportError(f"toolkit class path {toolkit_class_path!r} has no module")
        module = importlib.import_module(module_path)
        toolkit_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        log.warning(
            "preset node %s (%s): failed to import %s (%s); the agent "
            "will run without this tool",
            nid, preset_spec.display_name, toolkit_class_path, exc,
        )
        return []

    # Resolve per-node config: the `enabled_methods` list filters
    # which toolkit methods to expose; `toolkit_options` are passed
    # as **kwargs to the toolkit constructor (api_key, enable_*, etc.).
    # Both come from `ToolNodeConfig` (the `tool` parent now carries
    # the schema since the presets collapsed into `tool`).
    cfg = (node.data or {}).get("config") or {}
    enabled_methods = cfg.get("enabled_methods") or []
    toolkit_options = dict(cfg.get("toolkit_options") or {})

    # If the user picked specific methods, intersect against the
    # preset-declared allowed list. Unknown names are dropped with a
    # warning (silent at runtime; surfaces in test failure if asserted).
    if enabled_methods:
        allowed = set(method_names)
        filtered = [m for m in enabled_methods if m in allowed]
        unknown = [m for m in enabled_methods if m not in allowed]
        for u in unknown:
            log.warning(
                "preset node %s (%s): enabled_method %r is not in "
                "PRESET_REGISTRY toolkit_methods %r; ignoring",
                nid, preset_spec.display_name, u, method_names,
            )
        method_names = filtered

    # Instantiate the toolkit. Some constructors need API keys
    # (tavily → TAVILY_API_KEY). The toolkit itself logs a friendly
    # warning when the key is missing — we don't need to duplicate
    # that here. A `TypeError` from a missing required arg is
    # treated the same way: warn and skip.
    try:
        toolkit = toolkit_cls(**toolkit_options)
    except Exception as exc:  # noqa: BLE001 — toolkit constructors vary
        log.warning(
            "preset node %s (%s): %s(%s) instantiation failed (%s); "
            "check toolkit_options + required env vars / constructor args",
            nid, preset_spec.display_name, toolkit_class_path,
            sorted(toolkit_options), exc,
        )
        return []

    out: list[Any] = []
    for method_name in method_names:
        method = getattr(toolkit, method_name, None)
        if method is None or not callable(method):
            log.warning(
                "preset node %s (%s): %s.%s is not callable; skipping",
                nid, preset_spec.display_name, toolkit_class_path, method_name,
            )
            continue
        try:
            # `strict=False` matches the user-functions path — most
            # agno toolkit methods don't carry explicit type hints
            # for every parameter, and `strict=True` would refuse
            # to wrap them.
            func = Function.from_callable(method, name=method_name, strict=False)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "preset node %s (%s): Function.from_callable(%s.%s) failed (%s)",
                nid, preset_spec.display_name, toolkit_class_path,
                method_name, exc,
            )
            continue
        out.append(func)
    return out

# ─────────────────────────────────────────────────────────────────
# `tools` node — user-defined Python functions
# ─────────────────────────────────────────────────────────────────
# Same safe-builtins policy as the legacy `_tools_handler`: the user
# code is `exec()`'d in a controlled namespace with no `open`,
# `eval`, `__import__`.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "map": map, "max": max, "min": min, "print": print,
    "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "json": json, "re": re,
}

def build_tools_user_functions(node: IRNode) -> list[Any]:
    """One `Function.from_callable(...)` per user-defined function.

    Imported by `UserFunctionsToolStrategy.build_tools`. Kept here
    (not on the strategy class) because the safe-builtins policy is
    a platform-level invariant shared by every `tools` node.

    Returns the list of constructed `Function` instances. Empty /
    missing `functions` returns `[]`.
    """
    from agno.tools.function import Function

    cfg = node.data.get("config") or {}
    fns = cfg.get("functions") or []
    out: list[Any] = []
    for fn in fns:
        fn_name = fn.get("name") or "tool"
        fn_code = (fn.get("code") or "").rstrip()
        fn_desc = fn.get("description") or ""
        if not fn_code.strip():
            log.warning("tools node %s: function %r has no code, skipping", node.id, fn_name)
            continue

        # Execute the user code in a sandboxed namespace. We re-exec
        # each time so the function captures `cfg.functions[i]`-
        # specific state, but the namespace is one-shot per function
        # so there's no leakage between siblings.
        ns: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS, "__name__": "__sandbox__"}
        try:
            exec(fn_code, ns)  # noqa: S102 — intentional, controlled input
        except Exception as e:  # noqa: BLE001
            log.warning("tools node %s: function %r failed to load: %s", node.id, fn_name, e)
            continue

        if fn_name not in ns or not callable(ns[fn_name]):
            log.warning("tools node %s: function %r not defined in code", node.id, fn_name)
            continue

        try:
            # `strict=False` matches the generator's emission so the
            # generated tools accept untyped params (most user-defined
            # functions don't carry type hints).
            func = Function.from_callable(ns[fn_name], name=fn_name, strict=False)
        except Exception as e:  # noqa: BLE001
            log.warning("tools node %s: Function.from_callable(%r) failed: %s", node.id, fn_name, e)
            continue

        # Description becomes the tool's docstring so the agent sees it
        # in its system prompt. Function doesn't expose `description`
        # directly — set the underlying `entrypoint` docstring instead.
        if fn_desc:
            try:
                ns[fn_name].__doc__ = fn_desc
            except (AttributeError, TypeError):
                pass
        out.append(func)
    return out

# ─────────────────────────────────────────────────────────────────
# `http` node — build a real request function and wrap it
# ─────────────────────────────────────────────────────────────────
def build_http_function(node: IRNode):
    """Build a `Function.from_callable(...)` for one HTTP node.

    The wrapped callable mirrors the generator's `http_wrapper_block`
    output (URL substitution, headers/query/auth, JSON body for
    non-GET/DELETE). Returns `None` if `baseUrl` is missing.

    Wrapper signature: derived from `path` (every `{name}` placeholder
    becomes a typed `name: str` parameter) and from `bodySchema` (every
    property in `bodySchema.properties` becomes a typed parameter; the
    `required` list keeps them required, others default to `None`).
    Without this, `Function.from_callable` would see `**kwargs` and
    publish a `{kwargs: dict}` parameter — the LLM had no way to pass
    body values (and the previous version sent the bodySchema itself
    as the JSON body, so every POST request reached the server with
    `{"type": "object", "properties": {...}}` as the body — see the
    `dispatch_task` 422 failure on ).

    Imported by `HttpToolStrategy.build_tools`. The `_do_request`
    helper is shared with any future tool-source type that needs to
    perform an HTTP call (kept here as a free function so the
    closures stay tiny).
    """
    from agno.tools.function import Function

    cfg = node.data.get("config") or {}
    base_url = (cfg.get("baseUrl") or "").strip()
    if not base_url:
        # Previously this logged a warning and returned None, which the
        # strategy converted to an empty tool list — the user got an
        # agent with NO tools attached and no indication that the http
        # node was the cause. Distinguish "config is broken" (raise so
        # the UI surfaces the error) from "config is valid but produces
        # zero tools" (an explicit list, return []). The export path
        # uses its own template (generator/http_wrappers.py) and is not
        # affected by this
        # change.
        raise CompileError(
            f"http node {node.id!r} is missing `baseUrl`; the agent has no "
            "tool to call. Open the node and fill in the URL before running."
        )

    tool_name = cfg.get("toolName") or "http_call"
    tool_desc = cfg.get("toolDescription") or f"HTTP {cfg.get('method', 'GET')} wrapper"
    method = (cfg.get("method") or "GET").upper()
    path = cfg.get("path") or ""
    path_params = re.findall(r"\{([^{}]+)\}", path)
    headers = dict(cfg.get("headers") or {})
    auth_token = cfg.get("authToken") or ""
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    query = dict(cfg.get("queryParams") or {})

    base = base_url.rstrip("/")

    # Introspect the bodySchema (a JSON Schema string) to derive the
    # wrapper's body parameters. Returns (properties, required) or None
    # when the schema is missing / malformed / empty — those wrappers
    # send no JSON body (legacy GET-style behaviour).
    parsed_body = _parse_body_schema(cfg.get("bodySchema"))
    body_props: dict[str, Any] = {}
    body_required: list[str] = []
    if parsed_body is not None:
        body_props, body_required = parsed_body

    # Generate the wrapper source with named parameters, then `exec()`
    # it. The runtime introspection in `Function.from_callable` walks
    # the function's signature via `inspect.signature(...)` — so the
    # named params have to exist on the actual function object, not
    # just in a docstring. `exec()` is the same mechanism used by
    # `build_tools_user_functions` for user-supplied code; the source
    # here is generated by us (not user input), so the trust level is
    # the same as the export wrapper in `generator/http_wrappers.py`.
    src = _render_http_wrapper_src(
        tool_name=tool_name,
        path_params=path_params,
        body_props=body_props,
        body_required=body_required,
        method=method,
        path=path,
        base=base,
        headers=headers,
        query=query,
    )
    # `get_type_hints(func)` (used inside `Function.from_callable`)
    # resolves annotations via `func.__globals__`. With
    # `from __future__ import annotations` in this module, the exec'd
    # wrapper's annotations become forward-ref strings; we have to put
    # a real `typing.Any` in the namespace so the resolution finds it.
    # The same applies to the JSON-schema mapper
    # (`agno.utils.json_schema.get_json_schema`), which reaches for
    # type names like `list` / `dict` — those live in `__builtins__`.
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "Any": Any,
        "_do_request": _do_request,
    }
    try:
        exec(src, namespace)  # noqa: S102 — generated source
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "http node %s: failed to build wrapper src (%s); src was:\n%s",
            node.id, exc, src,
        )
        return None
    http_call = namespace[tool_name]
    http_call.__doc__ = tool_desc

    try:
        return Function.from_callable(http_call, name=tool_name, strict=False)
    except Exception as e:  # noqa: BLE001
        log.warning("http node %s: Function.from_callable failed: %s", node.id, e)
        return None

def _parse_body_schema(raw: Any) -> tuple[dict[str, Any], list[str]] | None:
    """Parse a `bodySchema` JSON Schema string.

    Returns `(properties, required)` when `raw` decodes to a JSON
    Schema object — the platform's `HttpNodeConfig.bodySchema` field
    (always stored as a string per `schemas.node_configs`). Two
    formats accepted:

      * **Proper JSON Schema** (the placeholder shown in
        `PropertyPanel/ToolForm.tsx`):
          `{"type": "object", "properties": {"city": {"type": "string"}},
           "required": ["city"]}`
        → `(properties, required)` extracted directly from
        `properties` + `required`.

      * **Flat shorthand** (a flat dict whose values are JSON
        Schema type names, e.g.
        `{"city": "string", "district": "string",
         "equipment_type": "string"}` — without the wrapping
        `{"type": "object", "properties": {...}}`):
        → detected when the parsed dict has no nested `properties`
        key and every value is a plain type-name string
        (`"string"` / `"number"` / `"integer"` / `"boolean"` /
        `"array"` / `"object"`). Each top-level key becomes a
        property whose schema is `{"type": <value>}`; all
        shorthand properties default to required (since no
        `required` list is supplied, every field is a param the
        LLM must provide).

    Anything else (malformed JSON, a non-object root, an empty
    `properties` map, or a flat dict whose values aren't
    type-name strings) returns None — the wrapper then falls
    through to the no-body path (legacy GET-style). Calling the
    agent shouldn't break just because the user typed a typo.

    PRE-FIX : only the proper JSON Schema format
    was accepted. The flat shape silently fell through to
    no-body, so the generated wrapper had zero parameters; the
    LLM correctly guessed `city`/`district`/`equipment_type` from
    the schema text, but the wrapper raised
    `TypeError: query_substations() got an unexpected keyword
    argument 'city'`. The mock server (using pydantic for its
    own request validation) surfaced the TypeError back to the
    agent as `"3 validation errors for query_substations...
    Unexpected keyword argument"`, and the agent gave up
    after two retries with the same error.
    """
    parsed = _parse_body_schema_raw(raw)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        return None

    # Format A — proper JSON Schema.
    if isinstance(parsed.get("properties"), dict) and parsed["properties"]:
        props = parsed["properties"]
        required_raw = parsed.get("required") or []
        if not isinstance(required_raw, list):
            required_raw = []
        required = [r for r in required_raw if isinstance(r, str) and r in props]
        return props, required

    # Format B — flat shorthand: keys are property names, values
    # are JSON Schema type names (e.g. "string", "number"). The
    # parser detects the shorthand by checking that no nested
    # `properties` key is present and every value is a plain
    # type-name string, then expands each entry to its proper
    # schema form. Without this branch, the previous parser
    # silently dropped the shorthand and the generated wrapper
    # had zero parameters — so every LLM call came back as
    # "Unexpected keyword argument".
    if _looks_like_flat_shorthand(parsed):
        shorthand_props = {}
        shorthand_required = []
        for key, type_name in parsed.items():
            if not isinstance(key, str) or not isinstance(type_name, str):
                # Mixed-type entries mean this isn't a clean shorthand;
                # fall through to None.
                return None
            shorthand_props[key] = {"type": type_name}
            shorthand_required.append(key)
        return shorthand_props, shorthand_required

    return None

def _parse_body_schema_raw(raw: Any) -> Any | None:
    """Decode the raw `bodySchema` field — accept a pre-parsed dict
    or a JSON string. Returns the parsed value on success, None on
    malformed input."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            log.warning(
                "http node bodySchema is not valid JSON; wrapper will "
                "send no body: %r",
                raw[:80],
            )
            return None
    return None

def _looks_like_flat_shorthand(parsed: dict) -> bool:
    """True iff `parsed` looks like the  shorthand —
    every value is a JSON Schema type-name string AND no key is
    a JSON Schema reserved word (which would indicate a proper
    JSON Schema, possibly malformed, rather than user shorthand).

    A dict like `{"type": "object"}` IS a (valid, empty) JSON
    Schema — we must NOT mis-detect it as shorthand and invent a
    `type` property. Same for `{"properties": {...}}` or
    `{"required": [...]}`. Only when the keys are all
    user-supplied property names do we treat the dict as shorthand.
    """
    if not parsed:
        return False
    reserved = {
        "type", "properties", "required", "additionalProperties",
        "patternProperties", "allOf", "anyOf", "oneOf", "not",
        "$ref", "$schema", "definitions", "additionalItems",
        "dependencies", "pattern", "format", "enum", "const",
        "if", "then", "else",
    }
    valid_types = {"string", "number", "integer", "boolean", "array", "object", "null"}
    for key, value in parsed.items():
        if key in reserved:
            return False
        if not isinstance(value, str):
            return False
        if value not in valid_types:
            return False
    return True

def _json_schema_to_py_type(info: Any) -> str:
    """Map a JSON Schema property's `type` to a Python type-hint name.

    Used to generate the runtime wrapper's parameter type annotations
    so `Function.from_callable`'s introspection produces a JSON
    schema the LLM can read (typed `properties` + correct `type`).
    Unknown / missing types fall back to `Any` — the introspection
    then emits `{}` which is permissive enough to keep the LLM
    flowing.
    """
    if not isinstance(info, dict):
        return "Any"
    t = info.get("type")
    if t == "string":
        return "str"
    if t == "integer":
        return "int"
    if t == "number":
        return "float"
    if t == "boolean":
        return "bool"
    if t == "array":
        return "list"
    if t == "object":
        return "dict"
    return "Any"

def _render_http_wrapper_src(
    *,
    tool_name: str,
    path_params: list[str],
    body_props: dict[str, Any],
    body_required: list[str],
    method: str,
    path: str,
    base: str,
    headers: dict[str, str],
    query: dict[str, str],
) -> str:
    """Build the wrapper function's Python source for one HTTP node.

    Three wrapper shapes:

      1. No path params, no body props → zero-arg function.
      2. Path params only → `name: str` per placeholder (required).
      3. Body schema → `name: <py_type>` per property; required items
         lack a default, optional ones get `= None`.

    The body construction walks `body_props` so the body dict keeps a
    stable key order matching the schema. `_do_request` is supplied
    via the exec namespace so we don't have to import anything inside
    the wrapper source.
    """
    args: list[str] = []
    for p in path_params:
        args.append(f"{p}: str")
    for name, info in body_props.items():
        py_type = _json_schema_to_py_type(info)
        if name in body_required:
            args.append(f"{name}: {py_type}")
        else:
            args.append(f"{name}: {py_type} = None")
    sig = ", ".join(args)

    body_lines: list[str] = []
    # NB: no `-> Any:` return annotation. pydantic's `validate_call`
    # (used inside agno's `Function._wrap_callable`) resolves forward
    # refs against `sys.modules[func.__module__].__dict__` — but the
    # exec'd function's `__module__` is `__main__`, which doesn't have
    # `typing.Any` imported. A missing return annotation is harmless:
    # pydantic treats it as `Any` anyway. Parameter annotations
    # (`user_id: str`, ...) are fine because `str` is a builtin.
    body_lines.append(f"def {tool_name}({sig}):")
    body_lines.append(f"    _path = {path!r}")
    for p in path_params:
        body_lines.append(
            f"    _path = _path.replace('{{{p}}}', str({p}))"
        )
    body_lines.append(f"    _url = {base!r} + _path")
    body_lines.append(f"    _headers = dict({headers!r})")
    body_lines.append(f"    _query = dict({query!r})")
    if body_props:
        # Build the JSON body from the named parameters. Always
        # include every property so the wrapper is deterministic; an
        # optional `None` still serialises to `null`, which most
        # REST APIs treat as "field omitted" (and `json.dumps(...,
        # skipkeys=True)` keeps the wrapper from blowing up on a
        # stray None key).
        kv_pairs = ", ".join(f'"{n}": {n}' for n in body_props)
        body_lines.append(f"    _body = {{{kv_pairs}}}")
    else:
        body_lines.append("    _body = None")
    body_lines.append(
        f"    return _do_request({method!r}, _url, _headers, _query, _body)"
    )
    return "\n".join(body_lines) + "\n"

def _do_request(method: str, url: str, headers: dict, query: dict, body: Any) -> Any:
    """The body of an HTTP wrapper call.

    Kept as a free function so the wrapper closures stay tiny. `body`
    is the already-shaped JSON body (a dict the LLM supplied via the
    wrapper's named parameters) — NOT a JSON Schema. The previous
    version took the bodySchema and JSON-serialised it as the body,
    which meant POST requests reached the server with
    `{"type": "object", "properties": {...}}` instead of the LLM's
    intended values (the  `dispatch_task` 422 failure).

    30s timeout, raises on non-2xx.
    """
    import requests

    if method == "GET":
        resp = requests.get(url, headers=headers, params=query, timeout=30)
    elif method == "DELETE":
        resp = requests.delete(url, headers=headers, params=query, timeout=30)
    else:
        verb = getattr(requests, method.lower(), None)
        if verb is None:
            raise ValueError(f"unsupported HTTP method {method!r}")
        resp = verb(url, headers=headers, params=query, json=body, timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text

# ─────────────────────────────────────────────────────────────────
# `mcp` node — real `MCPTools(...)` instance per the McpServer row
# ─────────────────────────────────────────────────────────────────
def build_mcp_tools(node: IRNode, *, user_id: str | None = None):
    """Build a real `MCPTools` for one MCP node.

    Imported by `McpToolStrategy.build_tools`. Dispatches on the
    linked `McpServer` row:

      * `transport="stdio"`        → `MCPTools(command=..., args=[...], transport="stdio")`
      * `transport="sse"`          → `MCPTools(url=..., transport="sse")`
      * `transport="streamable-http"` → `MCPTools(url=..., transport="streamable-http")`

    Returns `None` (and logs a warning) if the server is missing,
    disabled, its config is invalid, OR the `mcp` Python package
    isn't installed (`MCPTools` requires it as a peer dep). The agent
    runs without MCP rather than crashing the whole workflow.

     (round 2) — when `user_id` is provided, the row lookup is
    strictly scoped to `(user_id = <X>)`. The shared system tier
    (`user_id IS NULL`) is gone — every user configures their own MCP
    servers. `None` keeps the legacy "any visible row" behaviour so
    unit tests don't have to thread `user_id` through (most of them
    seed rows without setting `user_id`).

    This is **the same code path the exported `.py` uses**, just
    invoked at runtime instead of written to source. That's the
    "Model B" invariant: tool-source nodes are definitions the agent
    consumes; both runtime and export converge on agno's `MCPTools`.
    """
    cfg = node.data.get("config") or {}
    server_id = cfg.get("serverId") or ""
    prefix = cfg.get("toolNamePrefix") or ""

    if not server_id:
        # Distinguish "config is broken" from "config is valid but
        # resolves to no tools". A missing `serverId` is the former —
        # surface it so the user knows why the agent has no MCP tool
        # instead of silently running with zero tools attached.
        raise CompileError(
            f"mcp node {node.id!r} is missing `serverId`; the agent has no "
            "tool to call. Open the node and pick an MCP server before running."
        )

    try:
        from agno.tools.mcp import MCPTools  # noqa: F401  (imported lazily)
    except ImportError as e:
        # `mcp` peer dep isn't on this venv — common in unit-test envs
        # that don't need real MCP. Skip rather than crash.
        log.warning(
            "mcp node %s: agno.tools.mcp unavailable (%s); "
            "install `mcp` to enable MCP support", node.id, e,
        )
        return None

    try:
        from app.db.models import McpServer
        from app.db.session import session_scope
    except Exception as e:  # noqa: BLE001
        log.warning("mcp node %s: db unavailable (%s), skipping", node.id, e)
        return None

    server = None
    try:
        with session_scope() as db:
            q = db.query(McpServer).filter_by(id=server_id)
            #  (round 2): strict per-user visibility —
            # only the owner's MCP servers are reachable. Avoids
            # cross-tenant tool leakage when workflows are shared.
            # `None` keeps the legacy "any visible row" behaviour
            # for unit tests that don't thread `user_id` through.
            if user_id is not None:
                q = q.filter(McpServer.user_id == user_id)
            server = q.one_or_none()
    except Exception as e:  # noqa: BLE001
        log.warning("mcp node %s: db lookup failed (%s), skipping", node.id, e)
        return None

    if server is None:
        log.warning("mcp node %s: server %r not found, skipping", node.id, server_id)
        return None
    if not server.enabled:
        log.warning("mcp node %s: server %r disabled, skipping", node.id, server.name)
        return None

    common_kwargs: dict[str, Any] = {}
    if prefix:
        common_kwargs["tool_name_prefix"] = prefix

    try:
        if server.transport == "sse":
            return MCPTools(
                url=server.url,
                transport="sse",
                **common_kwargs,
            )
        if server.transport == "streamable-http":
            return MCPTools(
                url=server.url,
                transport="streamable-http",
                **common_kwargs,
            )
        # Default: stdio
        return MCPTools(
            command=server.command,
            args=list(server.args or []),
            transport="stdio",
            **common_kwargs,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("mcp node %s: MCPTools() construction failed: %s", node.id, e)
        return None