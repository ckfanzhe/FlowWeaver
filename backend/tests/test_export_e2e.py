"""End-to-end smoke for the code generator.

Build a workflow that exercises every node type, render to Python, write it
to disk, then verify:
  - the file is syntactically valid
  - it imports nothing from this platform
  - it only depends on `agno` (+ stdlib + requests)
  - it has an `if __name__ == "__main__"` block
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

def test_export_e2e_full_workflow(tmp_path):
    from app.core.compile import to_python_source as render_python

    wf = {
        "name": "E2E Demo",
        "nodes": [
            # Tool node (source='function') with one user function.
            # The legacy `tools` type is now `tool` with
            # `source='function'`.
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "MyTools", "config": {
                 "source": "function",
                 "functions": [{
                     "name": "double",
                     "description": "Multiply by 2",
                     "parameters": [{"name": "x", "type": "number", "required": True}],
                     "code": "def double(x: int) -> int:\n    return x * 2\n",
                 }],
             }}},

            # HTTP tool node (source='http') — wrapper function.
            {"id": "nh", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "FetchUser", "config": {
                 "source": "http",
                 "toolName": "fetch_user",
                 "toolDescription": "Look up a user",
                 "baseUrl": "https://api.example.com",
                 "method": "GET",
                 "path": "/users/{user_id}",
                 "authToken": "TOKEN-XYZ",
             }}},

            # MCP tool node (source='mcp') — user must start server.
            {"id": "nm", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "FS", "config": {
                 "source": "mcp",
                 "serverId": "mcp-fs",
                 "toolNamePrefix": "fs_",
             }}},

            # Human input node
            {"id": "nhi", "type": "human_input", "position": {"x": 0, "y": 0},
             "data": {"label": "Ask", "config": {
                 "prompt": "Continue? (y/n)",
                 "inputType": "confirm",
             }}},

            # Router node (one branch)
            {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "Route", "config": {
                 "condition": "True",
                 "branches": [{"label": "main", "target": "na"}],
             }}},

            # Parallel node (two branches) — : type='flow' with mode='parallel'.
            {"id": "np", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "FanOut", "config": {
                 "mode": "parallel",
                 "branches": [
                     {"label": "a", "target": "na_a"},
                     {"label": "b", "target": "na_b"},
                 ],
             }}},

            # Agents
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Main",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o",
                                            "apiKey": "sk-X"},
                                 "instructions": "Be helpful",
                                 "toolsRef": ["nt", "nh", "nm"]}}},
            {"id": "na_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "anthropic",
                                            "modelId": "claude-sonnet-4-5",
                                            "apiKey": "ak-X"},
                                 "instructions": "say A"}}},
            {"id": "na_b", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "B",
                      "config": {"model": {"provider": "openai",
                                            "modelId": "gpt-4o-mini",
                                            "apiKey": "sk-X"},
                                 "instructions": "say B"}}},
        ],
        "edges": [
            # Tool-source nodes (nt, nh, nm) are NOT wired by edges —
            # they're referenced from the agent's `toolsRef`. The
            # executable flow is: router → agent → parallel → agents.
            {"id": "e6", "source": "nr", "target": "na"},
            {"id": "e7", "source": "na", "target": "np"},
            {"id": "e8", "source": "np", "target": "na_a"},
            {"id": "e9", "source": "np", "target": "na_b"},
        ],
    }

    code = render_python(wf)

    # 1. syntactically valid
    tree = ast.parse(code)

    # 2. only allowed imports
    allowed_prefixes = ("__future__", "agno", "os", "sys", "typing", "json", "requests", "re", "textwrap")
    bad = []
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.Import):
            for n in node.names:
                mod = n.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
        if mod and not any(mod == a or mod.startswith(a + ".") for a in allowed_prefixes):
            bad.append(mod)
    assert not bad, f"unexpected imports: {bad}"

    # 3. every node type appears in the generated code
    assert "from agno.models.openai import OpenAIChat" in code
    assert "from agno.models.anthropic import Claude" in code
    assert "def double(x: int) -> int:" in code
    assert "def fetch_user(" in code
    assert "MCPTools(" in code
    assert "requires_user_input=True" in code
    assert "Router(" in code
    assert "Parallel(" in code
    assert "Agent(" in code

    # 4. main block exists
    assert 'if __name__ == "__main__":' in code or "if __name__ == '__main__':" in code

    # 5. write to disk, then re-parse via subprocess to be doubly sure
    out = tmp_path / "e2e_demo.py"
    out.write_text(code, encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-c", f"import ast; ast.parse(open({str(out)!r}).read())"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr

    # 6. filename safety
    assert re.match(r"^[a-z0-9_]+\.py$", out.name)