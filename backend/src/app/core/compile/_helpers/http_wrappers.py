"""HTTP node wrapper functions.

Each `tool` node on the canvas with `config.source == 'http'` becomes
a Python function that makes a real HTTP request. This module
produces:

  - `http_wrappers_metadata()`: a list of dicts describing each wrapper
    (the agent emitter uses the metadata to wire `tools=[...]`).
  - `http_wrapper_block(meta)`: the literal source code for one wrapper.

The actual `<nid>_step` is NOT emitted here — `tool` is a tool-source
type, not an executable type. The wrapper function is what an Agent
ends up calling.

: the prior `http` node type collapsed into the
unified `tool` type. Filter narrows from
`n["type"] == "http"` to `n["type"] == "tool"` AND
`cfg.source == "http"` so function-mode / MCP-mode tool nodes don't
get wrapper functions emitted for them.
"""
from __future__ import annotations

import re

from .utils import q, safe_ident

# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────
def http_wrappers_metadata(nodes_by_id: dict[str, dict]) -> list[dict]:
    """Build wrapper-function metadata for every `tool` node whose
    `source` discriminator is `'http'`."""
    out: list[dict] = []
    for node in nodes_by_id.values():
        if node["type"] != "tool":
            continue
        cfg = (node.get("data") or {}).get("config") or {}
        if cfg.get("source", "function") != "http":
            continue
        path = cfg.get("path") or ""
        path_params = re.findall(r"\{([^{}]+)\}", path)
        params_signature = ", ".join(f"{p}: str" for p in path_params)
        out.append({
            "func_name": safe_ident(cfg.get("toolName") or "http_call"),
            "docstring": cfg.get("toolDescription") or "",
            "base_url": cfg.get("baseUrl") or "",
            "path": path,
            "path_params": path_params,
            "params_signature": params_signature,
            "method": (cfg.get("method") or "GET").upper(),
            "headers_repr": q(cfg.get("headers") or {}),
            "query_repr": q(cfg.get("queryParams") or {}),
            "auth_token": cfg.get("authToken") or "",
            "node_id": node["id"],
        })
    return out

def http_wrapper_block(w: dict) -> str:
    """Render a `def fetch_xxx(...) -> dict:` function from one HTTP node."""
    path = w["path"]
    method = w["method"]
    body: list[str] = []
    body.append(f"def {w['func_name']}({w['params_signature']}) -> dict:")
    body.append(f"    {_docstring(w['docstring'] or f'HTTP {method} wrapper')}")
    body.append(f"    path = {q(path)}")
    for p in w["path_params"]:
        body.append(f"    path = path.replace({q('{' + p + '}')}, str({p}))")
    body.append(f"    url = {q(w['base_url'])} + path")
    body.append(f"    headers = dict({w['headers_repr']})")
    if w["auth_token"]:
        body.append(f"    headers['Authorization'] = {q('Bearer ' + w['auth_token'])}")
    if method == "GET":
        body.append(
            f"    resp = requests.get(url, headers=headers, params={w['query_repr']}, timeout=30)"
        )
    elif method == "DELETE":
        body.append(
            f"    resp = requests.delete(url, headers=headers, params={w['query_repr']}, timeout=30)"
        )
    else:
        # POST/PUT/PATCH — emit a JSON body placeholder; user can edit.
        body.append("    body = {}")
        verb = f"requests.{method.lower()}"
        body.append(
            f"    resp = {verb}(url, headers=headers, params={w['query_repr']}, json=body, timeout=30)"
        )
    body.append("    resp.raise_for_status()")
    body.append("    return resp.json()")
    return "\n".join(body) + "\n"

# ─────────────────────────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────────────────────────
def _docstring(text: str) -> str:
    r'''Render a single-line docstring as `"""text"""`.

    Falls back to a plain string literal if the text contains triple
    quotes (which would prematurely close the docstring).
    '''
    safe = text.replace('"""', "'''")
    return f'"""{safe}"""'