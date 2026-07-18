"""Generate the samples in /examples from synthetic workflow JSONs.

Run from the repo root:
    cd backend
    .venv/bin/python scripts/dump_examples.py

This is a developer convenience, not part of the runtime or tests.

P2-C (2026-08): the legacy `app.core.generator.generate(workflow)`
entry point was retired in the single-engine refactor — the same
graph that powers the runtime ALSO emits its Python source. Use
`app.core.compile.serialize.to_python_source(...)` to render the
sample.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `app` importable when run from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.core.compile.serialize import to_python_source  # noqa: E402

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


# ─────────────────────────────────────────────────────────────────
# Minimal agent
# ─────────────────────────────────────────────────────────────────
MINIMAL = {
    "name": "minimal_agent",
    "nodes": [
        {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "Greeter", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "Say hello to the user."}}},
    ],
    "edges": [],
}

# ─────────────────────────────────────────────────────────────────
# Agent with custom tools
# ─────────────────────────────────────────────────────────────────
AGENT_WITH_TOOLS = {
    "name": "agent_with_tools",
    "nodes": [
        {"id": "nt", "type": "tools", "position": {"x": 0, "y": 0},
         "data": {"label": "MyTools", "config": {
             "functions": [
                 {"name": "add", "description": "Add two numbers",
                  "parameters": [
                      {"name": "a", "type": "number", "required": True},
                      {"name": "b", "type": "number", "required": True},
                  ],
                  "code": "def add(a, b):\n    \"\"\"Add two numbers.\"\"\"\n    return a + b\n"},
                 {"name": "now_iso", "description": "Current time as ISO string",
                  "parameters": [],
                  "code": (
                      "def now_iso():\n"
                      "    \"\"\"Return current time as an ISO 8601 string.\"\"\"\n"
                      "    from datetime import datetime\n"
                      "    return datetime.now().isoformat()\n"
                  )},
             ],
         }}},
        {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "CalcAgent", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": (
                 "You can call `add(a, b)` to sum two numbers and "
                 "`now_iso()` to get the current time."
             ),
             "toolsRef": ["nt"]}}},
    ],
    "edges": [],
}

# ─────────────────────────────────────────────────────────────────
# Human Input — text
# ─────────────────────────────────────────────────────────────────
HUMAN_INPUT_TEXT = {
    "name": "human_input_text",
    "nodes": [
        {"id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
         "data": {"label": "AskName", "config": {
             "prompt": "What is your name?",
             "inputType": "text",
         }}},
        {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "Greeter", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "Greet the user by name.",
             "toolsRef": ["nh"]}}},
    ],
    "edges": [],
}

# ─────────────────────────────────────────────────────────────────
# HTTP node — fetch a user
# ─────────────────────────────────────────────────────────────────
HTTP_CALL = {
    "name": "http_call",
    "nodes": [
        {"id": "nh", "type": "http", "position": {"x": 0, "y": 0},
         "data": {"label": "GetUser", "config": {
             "toolName": "fetch_user",
             "toolDescription": "Look up a user by id",
             "baseUrl": "https://jsonplaceholder.typicode.com",
             "method": "GET",
             "path": "/users/{user_id}",
             # no authToken → no Authorization header
         }}},
        {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "UserAgent", "config": {
             "model": {"provider": "anthropic", "modelId": "claude-sonnet-4-5", "apiKey": "REPLACE_ME"},
             "instructions": (
                 "When the user mentions a user id, call `fetch_user(user_id)` "
                 "to retrieve their info and summarise it."
             ),
             "toolsRef": ["nh"]}}},
    ],
    "edges": [],
}

# ─────────────────────────────────────────────────────────────────
# Router — two branches by input prefix
# ─────────────────────────────────────────────────────────────────
ROUTER = {
    "name": "router_two_branches",
    "nodes": [
        {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
         "data": {"label": "Route", "config": {
             "condition": "True",  # default; each branch overrides
             "branches": [
                 {"target": "na_news", "condition": "'news' in inp"},
                 {"target": "na_chat", "condition": "not ('news' in inp)"},
             ],
         }}},
        {"id": "na_news", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "News", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "Headline the news."}}},
        {"id": "na_chat", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "Chat", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "Just chat."}}},
    ],
    "edges": [
        {"id": "e2", "source": "nr", "target": "na_news"},
        {"id": "e3", "source": "nr", "target": "na_chat"},
    ],
}

# ─────────────────────────────────────────────────────────────────
# Parallel fan-out
# ─────────────────────────────────────────────────────────────────
PARALLEL = {
    "name": "parallel_fanout",
    "nodes": [
        {"id": "np", "type": "flow", "position": {"x": 0, "y": 0},
         "data": {"label": "FanOut", "config": {
             "mode": "parallel",
             "branches": [
                 {"target": "na_short"},
                 {"target": "na_long"},
             ],
         }}},
        {"id": "na_short", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "ShortReply", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o-mini", "apiKey": "REPLACE_ME"},
             "instructions": "Reply in one sentence."}}},
        {"id": "na_long", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "LongReply", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "Reply in three paragraphs."}}},
    ],
    "edges": [
        {"id": "e2", "source": "np", "target": "na_short"},
        {"id": "e3", "source": "np", "target": "na_long"},
    ],
}

# ─────────────────────────────────────────────────────────────────
# Full kitchen-sink — tools + agent + http + human_input
# + router + parallel (omits mcp because it requires `pip install mcp`)
# ─────────────────────────────────────────────────────────────────
FULL_STACK = {
    "name": "full_stack",
    "nodes": [
        {"id": "nt", "type": "tools", "position": {"x": 0, "y": 0},
         "data": {"label": "MyTools", "config": {
             "functions": [
                 {"name": "add", "description": "Add two numbers",
                  "parameters": [],
                  "code": "def add(a, b):\n    return a + b\n"},
             ],
         }}},
        {"id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
         "data": {"label": "Confirm", "config": {
             "prompt": "Proceed? (y/n)", "inputType": "confirm",
         }}},
        {"id": "nhttp", "type": "http", "position": {"x": 0, "y": 0},
         "data": {"label": "Lookup", "config": {
             "toolName": "lookup", "toolDescription": "Lookup endpoint",
             "baseUrl": "https://api.example.com", "method": "GET",
             "path": "/items/{item_id}",
         }}},
        {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "Triage", "config": {
             "model": {"provider": "anthropic", "modelId": "claude-sonnet-4-5", "apiKey": "REPLACE_ME"},
             "instructions": "Use the tools available.",
             "toolsRef": ["nt", "nh", "nhttp"]}}},
        {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
         "data": {"label": "Route", "config": {
             "condition": "True",
             "branches": [{"target": "np_a"}, {"target": "np_b"}],
         }}},
        {"id": "np_a", "type": "flow", "position": {"x": 0, "y": 0},
         "data": {"label": "FanA", "config": {
             "mode": "parallel",
             "branches": [{"target": "na_a1"}, {"target": "na_a2"}],
         }}},
        {"id": "np_b", "type": "flow", "position": {"x": 0, "y": 0},
         "data": {"label": "FanB", "config": {
             "mode": "parallel",
             "branches": [{"target": "na_b1"}],
         }}},
        {"id": "na_a1", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "A1", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "a1"}}},
        {"id": "na_a2", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "A2", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "a2"}}},
        {"id": "na_b1", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "B1", "config": {
             "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "REPLACE_ME"},
             "instructions": "b1"}}},
    ],
    "edges": [
        {"id": "e5", "source": "na", "target": "nr"},
        {"id": "e6", "source": "nr", "target": "np_a"},
        {"id": "e7", "source": "nr", "target": "np_b"},
        {"id": "e8", "source": "np_a", "target": "na_a1"},
        {"id": "e9", "source": "np_a", "target": "na_a2"},
        {"id": "e10", "source": "np_b", "target": "na_b1"},
    ],
}


SAMPLES = [
    ("minimal_agent.py", MINIMAL),
    ("agent_with_tools.py", AGENT_WITH_TOOLS),
    ("human_input_text.py", HUMAN_INPUT_TEXT),
    ("http_call.py", HTTP_CALL),
    ("router_two_branches.py", ROUTER),
    ("parallel_fanout.py", PARALLEL),
    ("full_stack.py", FULL_STACK),
]


def main() -> None:
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, workflow in SAMPLES:
        code = to_python_source(workflow)
        path = EXAMPLES_DIR / filename
        path.write_text(code, encoding="utf-8")
        print(f"wrote {path}  ({len(code)} bytes)")


if __name__ == "__main__":
    main()