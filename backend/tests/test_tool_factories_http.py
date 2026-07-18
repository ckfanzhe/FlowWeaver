"""Tests for the runtime HTTP wrapper builder (`build_http_function`).

Background — the `dispatch_task` 422 bug.

The runtime HTTP wrapper in `core.tool_factories` was producing a
function whose `**kwargs` signature collapsed to a single
`{kwargs: dict}` parameter when `Function.from_callable` introspected
it. Worse, `_do_request` was treating the `bodySchema` (a JSON Schema
object) as the JSON body value — so every POST reached the server
with `{"type": "object", "properties": {...}}` as the body, instead of
the LLM's intended dispatch payload. The 422 we kept seeing was the
remote API rejecting the schema-as-body.

These tests pin the fix:
  * the wrapper signature carries NAMED parameters derived from
    `path` placeholders + `bodySchema.properties`;
  * `_do_request` accepts the body dict (not the schema) and sends
    it as JSON;
  * malformed bodySchema, empty properties, or no bodySchema all
    fall back to a no-body wrapper (legacy GET semantics).

The wrapper source itself is generated via `exec(...)` — the test
suite reaches into `Function.from_callable`'s introspection via
`inspect.signature(...)` to confirm the schema the LLM sees.
"""
from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from app.core.ir import IRNode

# ─────────────────────────────────────────────────────────────────
# Helpers — IRNode factories
# ─────────────────────────────────────────────────────────────────
def _http_node(node_id: str = "nh", **cfg_overrides) -> IRNode:
    """Build an IRNode shaped like a minimal HTTP-flavoured `tool` config.

    The factory under test (`build_http_function`) dispatches on
    `cfg.source='http'` from inside `ToolStrategy.build_tools()`,
    so the test node carries both the `tool` type literal AND
    the explicit `source='http'` discriminator.

    All `cfg_overrides` land under `data.config`. `baseUrl` defaults to
    a no-op so `build_http_function` accepts the node without raising.
    """
    cfg = {
        "source": "http",
        "toolName": "fetch_user",
        "toolDescription": "Fetch a user",
        "method": "GET",
        "baseUrl": "https://api.example.com",
        "path": "/users/{user_id}",
        "headers": {},
        "queryParams": {},
        "authToken": "",
        "bodySchema": "",
    }
    cfg.update(cfg_overrides)
    return IRNode(
        id=node_id, type="tool",
        data={"label": "HTTP", "config": cfg},
    )

# ─────────────────────────────────────────────────────────────────
# _parse_body_schema — JSON Schema → (properties, required)
# ─────────────────────────────────────────────────────────────────
class TestParseBodySchema:
    def test_empty_string_returns_none(self):
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema("") is None
        assert _parse_body_schema(None) is None

    def test_invalid_json_returns_none(self):
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema("{not-json") is None

    def test_non_object_root_returns_none(self):
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema('"a string"') is None
        assert _parse_body_schema("[1, 2, 3]") is None

    def test_missing_properties_returns_none(self):
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema('{"type": "object"}') is None

    def test_empty_properties_returns_none(self):
        """Empty `properties` map → no body params → wrapper sends
        no JSON body. The previous behaviour was to send the schema
        as the body; we now require at least one property."""
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema('{"type": "object", "properties": {}}') is None

    def test_dict_input_passes_through(self):
        """Some callers pre-parse the JSON; the helper accepts both."""
        from app.core.tool_factories import _parse_body_schema
        props, required = _parse_body_schema({
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        })
        assert props == {"x": {"type": "string"}}
        assert required == ["x"]

    def test_required_filters_unknown_keys(self):
        """A `required` entry that doesn't appear in `properties` is
        silently dropped — protects against a typo'd schema
        poisoning the wrapper signature."""
        from app.core.tool_factories import _parse_body_schema
        props, required = _parse_body_schema({
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x", "y_z_not_in_props"],
        })
        assert props == {"x": {"type": "string"}}
        assert required == ["x"]

    def test_flat_shorthand_schema_string_only(self):
        """Regression: a tool config carried flat shorthand
        `{"city": "string", "district": "string",
         "equipment_type": "string"}` — no nested
        `properties` wrapper, just a flat map. PRE-FIX the parser
        silently dropped this shape, so the generated wrapper had
        ZERO parameters and every LLM call returned
        `"Unexpected keyword argument 'city'"`. POST-FIX: detect
        the shorthand and expand each entry to its proper schema
        form, with all properties marked required (no `required`
        list was supplied)."""
        from app.core.tool_factories import _parse_body_schema
        props, required = _parse_body_schema(
            '{"city": "string", "district": "string", "equipment_type": "string"}'
        )
        assert props == {
            "city": {"type": "string"},
            "district": {"type": "string"},
            "equipment_type": {"type": "string"},
        }
        assert sorted(required) == ["city", "district", "equipment_type"]

    def test_flat_shorthand_accepts_all_json_schema_types(self):
        """The shorthand detector allows every JSON Schema type
        name so users can mix string / integer / boolean /
        array / object / number / null props without writing
        the full JSON Schema wrapper."""
        from app.core.tool_factories import _parse_body_schema
        props, required = _parse_body_schema({
            "name": "string",
            "age": "integer",
            "active": "boolean",
            "tags": "array",
            "meta": "object",
            "score": "number",
            "note": "null",
        })
        assert props == {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "active": {"type": "boolean"},
            "tags": {"type": "array"},
            "meta": {"type": "object"},
            "score": {"type": "number"},
            "note": {"type": "null"},
        }
        assert len(required) == 7

    def test_flat_shorthand_rejects_non_typename_values(self):
        """A dict whose values aren't JSON Schema type names is
        neither a proper schema nor a clean shorthand — fall
        through to None (no-body wrapper). Mixed entries like
        `{"x": "string", "y": "weird"}` aren't shorthand."""
        from app.core.tool_factories import _parse_body_schema
        assert _parse_body_schema(
            '{"x": "string", "y": "weird"}'
        ) is None
        # Mixed types (string + dict) also rejected.
        assert _parse_body_schema(
            '{"x": "string", "y": {"nested": "thing"}}'
        ) is None

# ─────────────────────────────────────────────────────────────────
# _json_schema_to_py_type — type mapping
# ─────────────────────────────────────────────────────────────────
class TestJsonSchemaToPyType:
    def test_string_maps_to_str(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "string"}) == "str"

    def test_integer_maps_to_int(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "integer"}) == "int"

    def test_number_maps_to_float(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "number"}) == "float"

    def test_boolean_maps_to_bool(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "boolean"}) == "bool"

    def test_array_maps_to_list(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "array"}) == "list"

    def test_object_maps_to_dict(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "object"}) == "dict"

    def test_unknown_type_falls_back_to_any(self):
        from app.core.tool_factories import _json_schema_to_py_type
        assert _json_schema_to_py_type({"type": "weird"}) == "Any"
        assert _json_schema_to_py_type({}) == "Any"
        assert _json_schema_to_py_type("not a dict") == "Any"

# ─────────────────────────────────────────────────────────────────
# _render_http_wrapper_src — the source generator
# ─────────────────────────────────────────────────────────────────
class TestRenderHttpWrapperSrc:
    def test_zero_args_when_no_path_no_body(self):
        from app.core.tool_factories import _render_http_wrapper_src
        src = _render_http_wrapper_src(
            tool_name="fetch_user", path_params=[], body_props={},
            body_required=[], method="GET", path="/users",
            base="https://api.example.com",
            headers={}, query={},
        )
        # No return annotation — pydantic's `validate_call` resolves
        # forward-refs against `sys.modules[func.__module__]`, which
        # for an exec'd wrapper is `__main__` and doesn't have
        # `typing.Any` imported. A missing return annotation is
        # harmless: pydantic treats it as `Any`.
        assert "def fetch_user():" in src
        assert "-> " not in src.split("\n")[0]
        # No path substitution lines.
        assert "replace(" not in src
        # No body construction (body = None for legacy GET-style).
        assert "_body = None" in src

    def test_path_params_become_named_arguments(self):
        from app.core.tool_factories import _render_http_wrapper_src
        src = _render_http_wrapper_src(
            tool_name="fetch_user", path_params=["user_id"],
            body_props={}, body_required=[],
            method="GET", path="/users/{user_id}",
            base="https://api.example.com",
            headers={}, query={},
        )
        assert "def fetch_user(user_id: str):" in src
        assert "replace('{user_id}', str(user_id))" in src
        assert "_body = None" in src

    def test_body_schema_becomes_typed_required_args(self):
        from app.core.tool_factories import _render_http_wrapper_src
        src = _render_http_wrapper_src(
            tool_name="submit", path_params=[],
            body_props={
                "substation_ids": {"type": "array"},
                "start_time": {"type": "string"},
                "limit": {"type": "integer"},
            },
            body_required=["substation_ids", "start_time"],
            method="POST", path="/dispatch",
            base="https://api.example.com",
            headers={}, query={},
        )
        # Required args get a type hint with no default.
        assert "substation_ids: list" in src
        assert "start_time: str" in src
        # Optional args get a default of None.
        assert "limit: int = None" in src
        # Body is constructed from the named params.
        assert '"substation_ids": substation_ids' in src
        assert '"start_time": start_time' in src
        assert '"limit": limit' in src
        # Method is passed verbatim (the wrapper still says which verb).
        assert "'POST'" in src
        # No `-> Any` return annotation (see TestZeroArgs comment).
        assert "-> " not in src.split("\n")[0]

    def test_path_and_body_combined(self):
        """Path params + body params together — must appear as one
        ordered signature, path first, then body."""
        from app.core.tool_factories import _render_http_wrapper_src
        src = _render_http_wrapper_src(
            tool_name="dispatch", path_params=["org_id"],
            body_props={"substation_ids": {"type": "array"}},
            body_required=["substation_ids"],
            method="POST", path="/orgs/{org_id}/dispatch",
            base="https://api.example.com",
            headers={}, query={},
        )
        assert "def dispatch(org_id: str, substation_ids: list):" in src
        assert "replace('{org_id}', str(org_id))" in src
        assert '"substation_ids": substation_ids' in src

# ─────────────────────────────────────────────────────────────────
# _do_request — body is the dict, NOT the schema
# ─────────────────────────────────────────────────────────────────
class TestDoRequest:
    """Direct unit tests for `_do_request` — the request layer doesn't
    introspect the wrapper signature, so we test it standalone with a
    fake `requests` module to verify the JSON body is the BODY dict,
    not the schema. The `dispatch_task` 422 bug came from
    this function being told to send a JSON Schema as the body."""

    def _patch_requests(self, monkeypatch, *, method: str = "POST",
                        status: int = 200, body: Any = None,
                        text: str = ""):
        """Install a fake `requests` module that records calls and
        returns the canned response. Returns the captured-call dict."""
        captured: dict[str, Any] = {}

        class _Resp:
            def __init__(self):
                self.status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

            def json(self):
                if body is not None:
                    return body
                raise ValueError("no json")

            @property
            def text(self):
                return text

        def _record(name):
            def _call(url, headers=None, params=None, json=None, timeout=None):
                captured.update(
                    name=name, url=url, headers=headers, params=params,
                    json=json, timeout=timeout,
                )
                return _Resp()
            return _call

        import sys
        fake = type(sys)("requests")
        fake.get = _record("get")
        fake.post = _record("post")
        fake.put = _record("put")
        fake.patch = _record("patch")
        fake.delete = _record("delete")
        monkeypatch.setitem(sys.modules, "requests", fake)
        return captured

    def test_get_sends_no_body(self, monkeypatch):
        from app.core.tool_factories import _do_request
        captured = self._patch_requests(monkeypatch, method="GET")
        out = _do_request("GET", "https://x/y", {}, {}, body=None)
        assert captured["name"] == "get"
        assert captured["json"] is None  # `json` kwarg only on POST.

    def test_post_sends_body_dict_as_json(self, monkeypatch):
        """The exact bug regression: previously `_do_request` was
        handed the bodySchema and sent THAT as the JSON body. The fix
        is that the wrapper passes the LLM's parameter values as a
        dict, and `_do_request` forwards them verbatim."""
        from app.core.tool_factories import _do_request
        captured = self._patch_requests(monkeypatch, method="POST",
                                        body={"ok": True})
        body = {"substation_ids": ["s1", "s2"], "start_time": "2025-01-01T00:00:00Z"}
        out = _do_request("POST", "https://x/y", {}, {}, body=body)
        assert captured["name"] == "post"
        assert captured["json"] == body, (
            "REGRESSION: _do_request must send the LLM-supplied dict "
            "as the JSON body, not a JSON Schema."
        )
        assert out == {"ok": True}

    def test_post_with_none_body_is_allowed(self, monkeypatch):
        """When the bodySchema has no properties, the wrapper passes
        `body=None` — requests should receive `json=None`."""
        from app.core.tool_factories import _do_request
        captured = self._patch_requests(monkeypatch, method="POST", text="ok")
        out = _do_request("POST", "https://x/y", {}, {}, body=None)
        assert captured["json"] is None
        assert out == "ok"  # text fallback when response isn't JSON.

    def test_unsupported_method_raises(self, monkeypatch):
        from app.core.tool_factories import _do_request
        self._patch_requests(monkeypatch, method="POST")
        with pytest.raises(ValueError, match="unsupported HTTP method"):
            _do_request("FOOBAR", "https://x/y", {}, {}, body=None)

# ─────────────────────────────────────────────────────────────────
# build_http_function — the wrapper the LLM actually sees
# ─────────────────────────────────────────────────────────────────
class TestBuildHttpFunction:
    """End-to-end checks via `Function.from_callable` introspection."""

    def _build(self, **cfg_overrides):
        from app.core.tool_factories import build_http_function
        return build_http_function(_http_node(**cfg_overrides))

    def test_no_path_no_body_produces_zero_arg_wrapper(self):
        fn = self._build(path="/users", method="GET")
        assert fn is not None
        sig = inspect.signature(fn.entrypoint)
        assert list(sig.parameters) == [], (
            "wrapper signature must be empty when there are no path "
            "or body params — otherwise Function.from_callable would "
            "expose a `kwargs: dict` parameter the LLM can't use."
        )

    def test_path_params_appear_in_signature(self):
        fn = self._build(path="/users/{user_id}", method="GET")
        assert fn is not None
        sig = inspect.signature(fn.entrypoint)
        assert "user_id" in sig.parameters
        # Parameter annotations are forward-ref strings under
        # `from __future__ import annotations`; evaluate them against
        # the exec'd wrapper's globals to compare to `str`. The exec
        # namespace has `__builtins__` so `str` resolves.
        ann_str = sig.parameters["user_id"].annotation
        assert eval(ann_str, fn.entrypoint.__globals__) is str

    def test_multi_path_param_signature(self):
        fn = self._build(path="/orgs/{org_id}/users/{user_id}", method="GET")
        sig = inspect.signature(fn.entrypoint)
        assert list(sig.parameters) == ["org_id", "user_id"]

    def test_body_schema_props_appear_in_signature(self):
        """The HTTP-wrapper regression: with `**kwargs` the LLM
        only saw `{kwargs: dict}`. The fix makes the wrapper accept
        the schema's properties as named parameters."""
        fn = self._build(
            path="/dispatch",
            method="POST",
            bodySchema=json.dumps({
                "type": "object",
                "properties": {
                    "substation_ids": {"type": "array"},
                    "start_time": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["substation_ids", "start_time"],
            }),
        )
        assert fn is not None
        sig = inspect.signature(fn.entrypoint)
        # All three properties appear.
        assert set(sig.parameters) == {
            "substation_ids", "start_time", "limit",
        }
        # Required → no default.
        assert sig.parameters["substation_ids"].default is inspect.Parameter.empty
        assert sig.parameters["start_time"].default is inspect.Parameter.empty
        # Optional → defaults to None.
        assert sig.parameters["limit"].default is None

    def test_malformed_body_schema_falls_back_to_no_body(self, caplog):
        """A bad bodySchema must NOT crash the wrapper build — it
        falls through to no-body mode (legacy GET semantics) with a
        warning. The LLM can call the tool, just without sending a
        body."""
        import logging
        with caplog.at_level(logging.WARNING,
                             logger="app.core.tool_factories"):
            fn = self._build(
                path="/dispatch", method="POST",
                bodySchema="{not valid json",
            )
        assert fn is not None
        sig = inspect.signature(fn.entrypoint)
        assert list(sig.parameters) == [], (
            "malformed bodySchema must NOT add phantom parameters to "
            "the wrapper signature"
        )

    def test_body_schema_without_properties_falls_back_to_no_body(self):
        fn = self._build(
            path="/dispatch", method="POST",
            bodySchema=json.dumps({"type": "object"}),
        )
        sig = inspect.signature(fn.entrypoint)
        assert list(sig.parameters) == []

    def test_path_and_body_combined_signature(self):
        fn = self._build(
            path="/orgs/{org_id}/dispatch",
            method="POST",
            bodySchema=json.dumps({
                "type": "object",
                "properties": {"substation_ids": {"type": "array"}},
                "required": ["substation_ids"],
            }),
        )
        sig = inspect.signature(fn.entrypoint)
        # Path first, body second — ordered.
        assert list(sig.parameters) == ["org_id", "substation_ids"]

    def test_wrapper_invokes_with_named_args_sends_body_dict(
        self, monkeypatch,
    ):
        """End-to-end: invoke the wrapper with named kwargs, confirm
        `_do_request` receives them as a JSON body dict (not a JSON
        Schema). This is the actual production bug — the wrapper
        used to drop the kwargs because of `**kwargs` AND send the
        bodySchema as the body."""
        from app.core import tool_factories

        captured: dict[str, Any] = {}

        def _fake_do_request(method, url, headers, query, body):
            captured.update(
                method=method, url=url, headers=headers,
                query=query, body=body,
            )
            return {"ok": True}

        monkeypatch.setattr(tool_factories, "_do_request", _fake_do_request)

        fn = self._build(
            path="/dispatch",
            method="POST",
            bodySchema=json.dumps({
                "type": "object",
                "properties": {"substation_ids": {"type": "array"}},
                "required": ["substation_ids"],
            }),
        )
        assert fn is not None
        out = fn.entrypoint(substation_ids=["s1", "s2"])
        assert out == {"ok": True}
        assert captured["method"] == "POST"
        assert captured["body"] == {"substation_ids": ["s1", "s2"]}, (
            "REGRESSION: wrapper must send the LLM-supplied dict as "
            "the JSON body, not the bodySchema."
        )

    def test_wrapper_invokes_path_param_substitution(self, monkeypatch):
        from app.core import tool_factories

        captured: dict[str, Any] = {}

        def _fake_do_request(method, url, headers, query, body):
            captured["url"] = url
            return {"ok": True}

        monkeypatch.setattr(tool_factories, "_do_request", _fake_do_request)

        fn = self._build(path="/users/{user_id}", method="GET")
        out = fn.entrypoint(user_id="alice")
        assert captured["url"] == "https://api.example.com/users/alice"

    def test_missing_base_url_raises_compile_error(self):
        """HTTP-wrapper invariant — `build_http_function` distinguishes
        "config is broken" (raise) from "config is valid but produces
        zero tools" (return []). `baseUrl` missing is the former."""
        from app.core.compile.errors import CompileError
        from app.core.tool_factories import build_http_function
        node = _http_node(baseUrl="")
        with pytest.raises(CompileError, match="missing `baseUrl`"):
            build_http_function(node)