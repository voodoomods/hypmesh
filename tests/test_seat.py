"""Seats: Claude and Codex at a mesh question through HypGraph's own MCP tools. The MCP server runs over a
RemoteGraph, so every call is the mesh's HTTP API with the agent's token: the gate attributes and refuses, the
record checks, and the question page shows who is at the table."""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

import pytest

pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from hypmesh.seat import RemoteGraph, build_server  # noqa: E402
from hypmesh.server import Mesh, make_server  # noqa: E402

ADA, BLAISE = "dev:ada", "dev:blaise"


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path, base_url="http://127.0.0.1:0")
    m.ids.add_org("u1", "University One")
    m.ids.add_org("l2", "Lab Two")
    m.ids.add_researcher(ADA, "Ada", "u1")
    m.ids.add_researcher(BLAISE, "Blaise", "l2")
    tokens = {"claude": m.ids.mint_agent(ADA, "claude-code")[1], "codex": m.ids.mint_agent(BLAISE, "codex")[1]}
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    srv = make_server(m, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    m.base_url = base
    yield m, base, tokens
    srv.shutdown()
    m.close()


def call(server, tool: str, **args) -> str:
    async def go():
        async with Client(server) as c:
            r = await c.call_tool(tool, args)
            return "\n".join(getattr(b, "text", "") for b in r.content)
    return asyncio.run(go())


def tools(server) -> list[str]:
    async def go():
        async with Client(server) as c:
            return sorted(t.name for t in await c.list_tools())
    return asyncio.run(go())


def seats(base: str) -> list[dict]:
    with urllib.request.urlopen(f"{base}/q/x-vs-y/seats") as r:
        return json.load(r)["seats"]


def test_a_seat_serves_hypgraphs_tools_over_the_mesh(mesh):
    m, base, t = mesh
    claude = build_server(base, "x-vs-y", "claude", t["claude"])
    names = tools(claude)
    for needed in ("overview", "start_investigation", "add", "link", "review", "work_queue", "claim_work", "my_work", "return_work"):
        assert needed in names
    assert "seated at HypMesh question 'x-vs-y'" in claude.instructions and "(@ada)" in claude.instructions
    out = call(claude, "start_investigation", title="Claude's attempt at X against Y",
               objective="Measure X and Y on the data and compare them, with the count.", reviewers=["codex"])
    assert "INV" in out
    inv = [i for i in m.q.graph("x-vs-y").investigations() if i["title"].startswith("Claude's")][0]
    owner = m.ids.owner("x-vs-y", inv["id"])
    assert owner and owner.rid == ADA and owner.agent_name == "claude-code"      # the ledger attributes it to Ada
    h = call(claude, "add", type="hypothesis", investigation=inv["id"],
             fields={"statement": "X is larger than Y on this data.", "predictions": ["Counting both gives X above Y."]})
    assert "H1" in h
    assert "H1" in call(claude, "overview", investigation=inv["id"])
    assert "X is larger than Y" in call(claude, "find", text="larger")


def test_the_gate_and_the_record_still_refuse(mesh):
    m, base, t = mesh
    claude = build_server(base, "x-vs-y", "claude", t["claude"])
    call(claude, "start_investigation", title="Claude's attempt at X against Y",
         objective="Measure X and Y on the data and compare them, with the count.")
    with pytest.raises(ToolError) as e:                       # a claim with a number nothing measured
        call(claude, "add", type="claim", investigation="INV2", fields={"statement": "X beats Y by 40 percent on the data.",
                                                                         "scope": "the data in this record"})
    assert "40" in str(e.value)
    row = [r for r in seats(base) if r["author"] == "claude"][0]
    assert row["refused"] == 1 and row["writes"] == 1 and row["last_action"] == "refused: create claim"
    with pytest.raises(SystemExit, match="reserved"):          # the mesh's reserved names, before anything is written
        build_server(base, "x-vs-y", "curator", t["claude"])
    with pytest.raises(SystemExit, match="refused the token"):
        build_server(base, "x-vs-y", "claude", "hm_not_a_token")
    with pytest.raises(SystemExit, match="cannot open question"):
        build_server(base, "no-such-question", "claude", t["claude"])
    curator_inv = m.q.get("x-vs-y")["curator_inv"]
    with pytest.raises(ToolError, match="curator"):            # edit-only-your-own, through the gate
        call(claude, "revise", node=curator_inv, fields={"objective": "Something else entirely, said in a sentence."},
             reason="trying to edit the curator's question")


def test_two_seats_at_the_table_and_the_live_panel(mesh):
    m, base, t = mesh
    claude = build_server(base, "x-vs-y", "claude", t["claude"])
    codex = build_server(base, "x-vs-y", "codex", t["codex"])
    call(claude, "start_investigation", title="Claude's attempt at X against Y",
         objective="Measure X and Y on the data and compare them, with the count.", reviewers=["codex"])
    inv = [i for i in m.q.graph("x-vs-y").investigations() if i["title"].startswith("Claude's")][0]["id"]
    call(claude, "add", type="hypothesis", investigation=inv,
         fields={"statement": "X is larger than Y on this data.", "predictions": ["Counting both gives X above Y."]})
    call(codex, "overview", investigation=inv)
    rows = {r["author"]: r for r in seats(base)}
    assert set(rows) == {"claude", "codex"}
    assert rows["claude"]["seated"] and rows["claude"]["writes"] == 2 and rows["claude"]["name"] == "Ada"
    assert rows["claude"]["last_action"].startswith("create hypothesis") and rows["claude"]["org_name"] == "University One"
    assert rows["codex"]["writes"] == 0 and rows["codex"]["reads"] >= 1 and rows["codex"]["agent_name"] == "codex"
    with urllib.request.urlopen(f"{base}/q/x-vs-y") as r:
        page = r.read().decode("utf-8")
    assert "At the table" in page and "hypmesh seat --mesh" in page and "[mcp_servers.hypmesh-x-vs-y]" in page
    assert f"#view=live&inv={inv}" in page                  # the live view follows the attempt being worked
    assert 'id="q-stats"' in page and 'id="q-solved"' in page   # the header counts refresh live with the standing table
    assert "setInterval(visible(standing), 5000)" in page and "setInterval(visible(seats), 3000)" in page
    with urllib.request.urlopen(f"{base}/q/x-vs-y/seats") as r:
        assert json.load(r)["active_inv"] == inv
    # a request without a token reads the record but takes no seat
    with urllib.request.urlopen(f"{base}/q/x-vs-y/api/graph/investigations") as r:
        r.read()
    assert {r["author"] for r in seats(base)} == {"claude", "codex"}


def test_remote_graph_reads_match_the_local_record(mesh):
    m, base, t = mesh
    g = RemoteGraph(f"{base}/q/x-vs-y", t["claude"], seat="claude")
    local = m.q.graph("x-vs-y")
    inv = local.investigations()[0]["id"]
    assert [i["id"] for i in g.investigations()] == [i["id"] for i in local.investigations()]
    assert g.resolve("INV1") == inv and g.get(inv)["body"]["title"] == local.get(inv)["body"]["title"]
    assert g.verify()["ok"] and g.verify()["head"] == local.verify_chain()["head"]
    with pytest.raises(KeyError):
        g.get("n-nowhere")
