"""Questions citing questions. Record B's claim rests on record A's claim through a source that pins A's node by
version and hash (hypgraph.references). The mesh fills the pin from the live node, refuses a fallen one, indexes the
reference, caps B's standing at A's, marks B at risk when A falls, and draws the network from the index."""
from __future__ import annotations

import threading

import pytest

from hypmesh.network import question_of
from hypmesh.server import Mesh, make_server
from test_mesh import ADA, BLAISE, CYRUS, _ada_investigation, call, standing, write


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path, base_url="http://127.0.0.1:0", open_claims=3)
    m.ids.add_org("u1", "University One")
    m.ids.add_org("l2", "Lab Two")
    m.ids.add_researcher(ADA, "Ada", "u1")
    m.ids.add_researcher(BLAISE, "Blaise", "l2")
    m.ids.add_researcher(CYRUS, "Cyrus")
    tokens = {name: m.ids.mint_agent(rid, f"{name}-agent")[1] for name, rid in (("ada", ADA), ("blaise", BLAISE), ("cyrus", CYRUS))}
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="a")
    m.q.open("Does X beat Y at scale?", "Take the pilot's answer and test it on the full data.", hazard_reviewed=True, qid="b")
    srv = make_server(m, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    m.base_url = base
    yield m, base, tokens
    srv.shutdown()
    m.close()


def _blaise_cites(base, token, record_url, node, **more):
    """Blaise's investigation in B, a source pinning A's node, and a claim citing it."""
    st, inv = write(base, "b", token, op="create", author="blaise-harness", type="investigation",
                    fields={"title": "Blaise builds on the pilot", "objective": "Use the pilot's count as the premise for the scale-up.", "reviewers": []})
    assert st == 200, inv
    st, src = write(base, "b", token, op="create", author="blaise-analyst", type="source", inv=inv["id"],
                    fields={"record": record_url, "node": node, **more})
    if st != 200:
        return inv["id"], None, (st, src)
    st, cl = write(base, "b", token, op="create", author="blaise-analyst", type="claim", inv=inv["id"],
                   fields={"statement": "The pilot found X larger than Y, so the scale-up starts from that premise.",
                           "scope": "the pilot's result as recorded in question a"},
                   links=[{"rel": "cites", "to": src["id"]}])
    assert st == 200, cl
    return inv["id"], src, cl


def test_question_of():
    assert question_of("http://127.0.0.1:8800", "http://127.0.0.1:8800/q/demo") == "demo"
    assert question_of("https://mesh.example.org", "https://mesh.example.org/q/demo/") == "demo"
    assert question_of("https://mesh.example.org", "https://mesh.example.org/q/demo/#node=x") == "demo"
    assert question_of("https://mesh.example.org", "https://other.example.org/q/demo") is None
    assert question_of("https://mesh.example.org", "https://mesh.example.org/boards") is None
    assert question_of("https://mesh.example.org", "not a url") is None


def test_the_mesh_fills_the_pin_and_indexes_the_reference(mesh):
    m, base, t = mesh
    inv, qn, run, res, claim = _ada_investigation(base, "a", t["ada"])
    _, src, cl = _blaise_cites(base, t["blaise"], f"{base}/q/a", "CL1")        # a display code is enough
    b = src["body"]
    assert b["record"] == f"{base}/q/a" and b["node"] == claim and b["version"] == 1 and len(b["hash"]) == 64
    assert b["citation"].startswith("CL1 (claim) in") and "7 against 5" in b["says"]
    row = m.refs.get("b", src["id"])
    assert row and row["to_question"] == "a" and row["to_node"] == claim and row["to_hash"] == b["hash"]
    assert [r["question"] for r in m.refs.citing("a", claim)] == ["b"]
    c, _ = standing(base, "b", cl["id"])
    assert c["rests_on"] == [{"question": "a", "node": claim, "code": "CL1", "level": "L0", "revised": False, "withdrawn": False, "hash_ok": True}]
    assert c["level"] == "L0" and c["own_level"] == "L0" and c["capped_by"] == ""


def test_standing_is_capped_by_the_premise_and_rises_with_it(mesh):
    m, base, t = mesh
    inv, qn, run, res, claim = _ada_investigation(base, "a", t["ada"])
    _, src, cl = _blaise_cites(base, t["blaise"], f"{base}/q/a", claim)
    # Cyrus verifies Blaise's claim: on its own it would be L2, but it rests on an L0 claim
    st, out = write(base, "b", t["cyrus"], op="review", author="cyrus", target=cl["id"], verdict="checked",
                    method="Read the pilot record and the premise.", notes="The premise is stated as the pilot recorded it.")
    assert st == 200, out
    c, _ = standing(base, "b", cl["id"])
    assert c["own_level"] == "L2" and c["level"] == "L0" and c["capped_by"] == "a/CL1"
    # Blaise verifies the pilot's claim in A: A rises to L2, and so does B's claim
    st, out = write(base, "a", t["blaise"], op="review", author="blaise", target=claim, verdict="checked",
                    method="Counted X and Y myself.", notes="7 and 5, as claimed.")
    assert st == 200, out
    c, _ = standing(base, "b", cl["id"])
    assert c["level"] == "L2" and c["capped_by"] == "" and c["rests_on"][0]["level"] == "L2"
    # the boards credit Cyrus's review for the claim as reviewed, not for its premises
    st, boards = call(base, "/api/boards")
    cyrus = next(r for r in boards["researcher"] if r["name"] == "Cyrus")
    assert cyrus["reviews_given"] == 1 and cyrus["reviews_stood"] == 1
    # Ada withdraws the pilot's claim: B's claim is at risk, and says why
    st, out = write(base, "a", t["ada"], op="withdraw", author="analyst", node=claim, why="the pairs were matched after the scores were seen")
    assert st == 200, out
    c, d = standing(base, "b", cl["id"])
    assert c["level"] == "at_risk" and c["own_level"] == "L2" and c["capped_by"] == "a/CL1"
    assert c["rests_on"][0]["withdrawn"] is True and d["by_level"].get("at_risk") == 1
    cyrus = next(r for r in call(base, "/api/boards")[1]["researcher"] if r["name"] == "Cyrus")
    assert cyrus["reviews_stood"] == 1 and cyrus["reviews_fell"] == 0        # his review of B still stands on its own


def test_a_revised_premise_is_reported_not_penalised(mesh):
    m, base, t = mesh
    inv, qn, run, res, claim = _ada_investigation(base, "a", t["ada"])
    _, src, cl = _blaise_cites(base, t["blaise"], f"{base}/q/a", claim)
    st, out = write(base, "a", t["ada"], op="revise", author="analyst", node=claim, reason="the scope was too narrow",
                    fields={"scope": "the test data in this record, both halves"})
    assert st == 200, out
    c, _ = standing(base, "b", cl["id"])
    assert c["rests_on"][0]["revised"] is True and c["rests_on"][0]["hash_ok"] is True and c["level"] == "L0"


def test_refusals_at_the_gate(mesh):
    m, base, t = mesh
    inv, qn, run, res, claim = _ada_investigation(base, "a", t["ada"])
    _, _, (st, out) = _blaise_cites(base, t["blaise"], f"{base}/q/nope", claim)
    assert st == 404 and "no question" in out["detail"]
    _, _, (st, out) = _blaise_cites(base, t["blaise"], f"{base}/q/a", "n-nowhere")
    assert st == 404 and "no node" in out["detail"]
    st, pin = call(base, f"/q/a/api/graph/pin/{claim}?record={base}/q/a")
    assert st == 200 and pin["version"] == 1
    _, _, (st, out) = _blaise_cites(base, t["blaise"], f"{base}/q/a", claim, version=1, hash="0" * 64)
    assert st == 409 and "does not match" in out["detail"]
    _, src, cl = _blaise_cites(base, t["blaise"], f"{base}/q/a", claim, **{k: pin[k] for k in ("version", "hash", "citation")})
    assert src is not None and src["body"]["hash"] == pin["hash"]
    st, out = write(base, "a", t["ada"], op="withdraw", author="analyst", node=claim, why="the pairs were matched after the scores were seen")
    assert st == 200, out
    _, _, (st, out) = _blaise_cites(base, t["blaise"], f"{base}/q/a", claim)
    assert st == 409 and "withdrawn" in out["detail"]


def test_a_reference_to_another_host_is_taken_as_written(mesh):
    m, base, t = mesh
    other = "https://other.example.org/q/z"
    _, _, (st, out) = _blaise_cites(base, t["blaise"], other, "n1", citation="A claim in a record on another mesh.")
    assert st == 400 and "record, node, version and hash" in out["detail"]      # half a reference: HypGraph's own shape refusal
    _, src, cl = _blaise_cites(base, t["blaise"], other, "n1", version=3, hash="a" * 64, citation="A claim in a record on another mesh.")
    assert src is not None and m.refs.get("b", src["id"])["to_question"] == ""
    c, _ = standing(base, "b", cl["id"])
    assert c["rests_on"] == [] and c["level"] == "L0"                         # not read, not capped: it is not ours to read
    st, net = call(base, "/api/network")
    assert {"from": "b", "to": "", "kind": "cites", "count": 1} in net["edges"]


def test_network_api_page_rebuild_and_part_of(mesh):
    m, base, t = mesh
    inv, qn, run, res, claim = _ada_investigation(base, "a", t["ada"])
    _blaise_cites(base, t["blaise"], f"{base}/q/a", claim)
    m.q.open("Does X beat Y on site two?", "The same measurement on the second site's data.", hazard_reviewed=True, qid="c", part_of="a")
    with pytest.raises(ValueError, match="part of"):
        m.q.open("Orphan", "A sub-question of nothing.", hazard_reviewed=True, qid="d", part_of="nope")
    st, net = call(base, "/api/network")
    assert [q["id"] for q in net["questions"]] == ["a", "b", "c"] and net["questions"][2]["part_of"] == "a"
    assert {"from": "b", "to": "a", "kind": "cites", "count": 1} in net["edges"]
    assert {"from": "c", "to": "a", "kind": "part_of", "count": 0} in net["edges"]
    st, refs = call(base, "/q/a/references")
    assert st == 200 and [r["question"] for r in refs["cited_by"]] == ["b"] and refs["rests_on"] == []
    st, refs = call(base, "/q/b/references")
    assert refs["rests_on"][0]["rests_on"][0]["question"] == "a" and len(refs["outgoing"]) == 1
    before = m.refs.outgoing("b")
    assert m.refs.rebuild(m.q, base) == 1 and m.refs.outgoing("b")[0]["to_hash"] == before[0]["to_hash"]
    for path in ("/network", "/", "/q/b", "/q/a"):
        st, _, _ = __import__("test_deploy").request(int(base.rsplit(":", 1)[1]), "GET", path)
        assert st == 200, path
