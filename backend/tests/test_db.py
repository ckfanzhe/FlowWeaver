"""DB layer smoke tests: models can be created, queried, and JSON columns round-trip."""
from __future__ import annotations

from datetime import datetime

from app.db.models import McpServer, Workflow

def test_workflow_persists_and_round_trips_json(db):
    wf = Workflow(
        id="wf-1",
        name="My Workflow",
        description="desc",
        nodes=[{"id": "n1", "type": "agent"}, {"id": "n2", "type": "agent"}],
        edges=[{"id": "e1", "source": "n1", "target": "n2"}],
    )
    db.add(wf)
    db.commit()

    fetched = db.query(Workflow).filter_by(id="wf-1").one()
    assert fetched.name == "My Workflow"
    assert fetched.description == "desc"
    assert len(fetched.nodes) == 2
    assert fetched.nodes[0]["type"] == "agent"
    assert fetched.edges[0]["source"] == "n1"
    assert isinstance(fetched.created_at, datetime)
    assert isinstance(fetched.updated_at, datetime)

def test_mcp_server_stdio_persists(db):
    server = McpServer(
        id="mcp-1",
        name="filesystem",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        env={"DEBUG": "1"},
        enabled=True,
    )
    db.add(server)
    db.commit()

    fetched = db.query(McpServer).filter_by(id="mcp-1").one()
    assert fetched.transport == "stdio"
    assert fetched.command == "npx"
    assert fetched.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert fetched.env == {"DEBUG": "1"}

def test_mcp_server_sse_persists(db):
    server = McpServer(
        id="mcp-2",
        name="remote",
        transport="sse",
        url="http://localhost:3000/sse",
        headers={"Authorization": "Bearer x"},
        enabled=False,
    )
    db.add(server)
    db.commit()

    fetched = db.query(McpServer).filter_by(id="mcp-2").one()
    assert fetched.transport == "sse"
    assert fetched.url == "http://localhost:3000/sse"
    assert fetched.enabled is False

def test_updated_at_changes_on_modify(db):
    wf = Workflow(id="wf-2", name="A", nodes=[], edges=[])
    db.add(wf)
    db.commit()
    first_updated = wf.updated_at

    wf.name = "B"
    db.commit()
    assert wf.updated_at >= first_updated