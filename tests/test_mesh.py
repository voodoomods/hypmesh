"""The mesh end to end, over HTTP, in process: identities, the gate, standing from L0 to L3, the boards, the hazard
list. Three researchers in three organisations work one question; the record and the ledger decide what stands."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from hypmesh.server import Mesh, make_server

ADA, BLAISE, CYRUS = "0000-0002-1825-0097", "0000-0001-5109-3700", "0000-0002-9079-593X"


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path, base_url="http://127.0.0.1:0", open_claims=2)
    m.ids.add_org("u1", "University One")
    m.ids.add_org("l2", "Lab Two")
    m.ids.add_researcher(ADA, "Ada", "u1")
    m.ids.add_researcher(BLAISE, "Blaise", "l2")
    m.ids.add_researcher(CYRUS, "Cyrus")                       # unaffiliated: counts as an organisation of one
    tokens = {name: m.ids.mint_agent(orcid, f"{name}-agent")[1] for name, orcid in (("ada", ADA), ("blaise", BLAISE), ("cyrus", CYRUS))}
    tokens["ada2"] = m.ids.mint_agent(ADA, "ada-second-agent")[1]
    q = m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    srv = make_server(m, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    m.base_url = base
    yield m, base, tokens, q
    srv.shutdown()
    m.close()


def call(base, path, body=None, token="", method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def write(base, qid, token, **body):
    return call(base, f"/q/{qid}/api/graph/write", body, token)


def test_hazard_list_refuses_at_opening(mesh):
    m, *_ = mesh
    with pytest.raises(ValueError, match="does not host"):
        m.q.open("Gain of function for H5N1", "…", hazard_reviewed=True)
    with pytest.raises(ValueError, match="hazard review"):
        m.q.open("Harmless", "…")


def test_no_token_no_write_and_reserved_names(mesh):
    m, base, t, q = mesh
    st, out = write(base, q["id"], "", op="create", author="ada", type="investigation", fields={"title": "t", "objective": "o"})
    assert st == 401 and "token" in out["detail"]
    st, out = write(base, q["id"], t["ada"], op="create", author="curator", type="investigation", fields={"title": "t", "objective": "o"})
    assert st == 403 and "reserved" in out["detail"]
    st, out = write(base, q["id"], t["ada"], op="create", author="instrument", type="run", fields={"kind": "lab", "name": "x", "state": "succeeded"})
    assert st == 403
    st, me = call(base, "/api/me", token=t["ada"])
    assert st == 200 and me["orcid"] == ADA and me["agent_name"] == "ada-agent"


def _ada_investigation(base, qid, token):
    """Ada's swarm: an investigation with her own reviewer seat on the board, a question, a run with source, a result
    with values, and a claim bound to them."""
    st, inv = write(base, qid, token, op="create", author="harness", type="investigation",
                    fields={"title": "Ada's attempt at X vs Y", "objective": "Compute X and Y from the data and compare.", "reviewers": ["ada-reviewer"]})
    assert st == 200, inv
    inv = inv["id"]
    st, qn = write(base, qid, token, op="create", author="harness", type="question", inv=inv, fields={"text": "Is X larger than Y?"})
    assert st == 200, qn
    st, run = write(base, qid, token, op="create", author="analyst", type="run", inv=inv,
                    fields={"kind": "agent", "name": "count X and Y", "state": "succeeded",
                            "config": {"source": "x, y = 7, 5\nprint('x', x)\nprint('y', y)"}})
    assert st == 200, run
    st, res = write(base, qid, token, op="create", author="analyst", type="result", inv=inv,
                    fields={"kind": "computed", "outcome": "positive", "summary": "X is 7 and Y is 5 on the data.", "values": {"x": 7, "y": 5}},
                    links=[{"rel": "produced", "from": run["id"]}])
    assert st == 200, res
    st, cl = write(base, qid, token, op="create", author="analyst", type="claim", inv=inv,
                   fields={"statement": "X is larger than Y on the data: 7 against 5.", "scope": "the test data in this record",
                           "measurements": [{"name": "x", "key": "x", "value": 7, "result": res["id"]}, {"name": "y", "key": "y", "value": 5, "result": res["id"]}]},
                   links=[{"rel": "answers", "to": qn["id"]}, {"rel": "derived_from", "to": res["id"]},
                          {"rel": "supports", "from": res["id"], "why": "the measurement of exactly this"}])
    assert st == 200, cl
    return inv, qn["id"], run["id"], res["id"], cl["id"]


def standing(base, qid, claim_id):
    st, d = call(base, f"/q/{qid}/standing")
    assert st == 200
    return next(c for c in d["all"] if c["id"] == claim_id), d


def test_standing_from_L0_to_L3(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    inv, qn, run, res, claim = _ada_investigation(base, qid, t["ada"])
    c, d = standing(base, qid, claim)
    assert c["level"] == "L0" and c["owner"] == ADA and not d["solved"]

    # Ada's own reviewer seat (same identity): the record counts it, the mesh calls it checked — L1, never more
    st, out = write(base, qid, t["ada2"], op="review", author="ada-reviewer", target=claim, verdict="checked",
                    method="Re-read the result's values against the claim's statement.", notes="The two numbers match the statement.")
    assert st == 200, out
    c, _ = standing(base, qid, claim)
    assert c["level"] == "L1" and c["independent"] == []

    # Blaise (another ORCID, another org): the mesh seats him on the board, his check makes the claim verified — L2
    st, out = write(base, qid, t["blaise"], op="review", author="blaise", target=claim, verdict="checked",
                    method="Loaded the data myself and counted X and Y with a different script.", notes="I count 7 and 5 as well.")
    assert st == 200, out
    c, _ = standing(base, qid, claim)
    assert c["level"] == "L2" and [r["org"] for r in c["independent"]] == ["l2"]
    st, invn = call(base, f"/q/{qid}/api/graph/node/{inv}")
    assert "blaise" in invn["body"]["reviewers"] and "ada-reviewer" in invn["body"]["reviewers"]

    # the instrument reproduces the run behind the result (as hypswarm's reproducer files it)
    st, lab = write(base, qid, m.instrument_token, op="create", author="instrument", type="run", inv=inv,
                    fields={"kind": "lab", "name": "re-execution of the analyst's run", "state": "succeeded",
                            "note": "re-ran the code: 2 of 2 registered values confirmed by the output",
                            "config": {"reproduces": run, "confirmed": ["x", "y"], "unprinted": [], "contradicted": []}},
                    links=[{"rel": "replicates", "to": run, "data": {"differs": "same_code"}}])
    assert st == 200, lab
    c, _ = standing(base, qid, claim)
    assert c["reproduced"] is True and c["level"] == "L2"            # one organisation so far

    # Cyrus, unaffiliated, is a second organisation: L3, and the question is solved
    st, out = write(base, qid, t["cyrus"], op="review", author="cyrus", target=claim, verdict="replicated",
                    method="Re-ran the registered code in my own environment.", notes="Same values, 7 and 5.")
    assert st == 200, out
    c, d = standing(base, qid, claim)
    assert c["level"] == "L3" and sorted(c["orgs"]) == [f"independent:{CYRUS}", "l2"] and d["solved"]
    st, qs = call(base, "/api/questions")
    assert qs["questions"][0]["status"]["solved"] is True


def test_an_identity_edits_only_its_own(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    inv, qn, run, res, claim = _ada_investigation(base, qid, t["ada"])
    st, out = write(base, qid, t["blaise"], op="withdraw", author="blaise", node=claim, why="I disagree with the counting.")
    assert st == 403 and "another researcher" in out["detail"]
    st, out = write(base, qid, t["blaise"], op="revise", author="blaise", node=q["curator_inv"], fields={"title": "mine now"}, reason="taking over")
    assert st == 403 and "curator" in out["detail"]
    st, out = write(base, qid, t["ada2"], op="withdraw", author="analyst", node=claim, why="A counting error on my side; redoing it.")
    assert st == 200, out                                              # a second agent of the same researcher may
    c, _ = standing(base, qid, claim)
    assert c["level"] == "withdrawn"


def test_open_work_cap_is_the_same_for_everyone(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    inv, qn, run, res, claim = _ada_investigation(base, qid, t["ada"])
    st, out = write(base, qid, t["ada"], op="create", author="analyst", type="claim", inv=inv,
                    fields={"statement": "Y is smaller than X on the data: 5 against 7.", "scope": "the test data in this record",
                            "measurements": [{"name": "x", "key": "x", "value": 7, "result": res}, {"name": "y", "key": "y", "value": 5, "result": res}]},
                    links=[{"rel": "answers", "to": qn}, {"rel": "derived_from", "to": res}])
    assert st == 200, out
    st, out = write(base, qid, t["ada"], op="create", author="analyst", type="claim", inv=inv,
                    fields={"statement": "X exceeds Y by 2 on the data.", "scope": "the test data in this record",
                            "measurements": [{"name": "x", "key": "x", "value": 7, "result": res}, {"name": "y", "key": "y", "value": 5, "result": res}]},
                    links=[{"rel": "answers", "to": qn}, {"rel": "derived_from", "to": res}])
    assert st == 429 and "await review" in out["detail"]


def test_the_record_still_refuses_overclaims_through_the_mesh(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    inv, qn, run, res, claim = _ada_investigation(base, qid, t["ada"])
    st, out = write(base, qid, t["ada"], op="create", author="analyst", type="claim", inv=inv,
                    fields={"statement": "X is larger than Y by 42 on the data.", "scope": "the test data in this record",
                            "measurements": [{"name": "x", "key": "x", "value": 7, "result": res}]},
                    links=[{"rel": "answers", "to": qn}, {"rel": "derived_from", "to": res}])
    assert st == 400 and "42" in out["detail"]            # the record refused it; dispatch answers 400
    st, rr = call(base, f"/q/{qid}/api/graph/refusals")
    assert st == 200


def test_boards_count_what_survived(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    inv, qn, run, res, claim = _ada_investigation(base, qid, t["ada"])
    st, out = write(base, qid, t["blaise"], op="review", author="blaise", target=claim, verdict="checked",
                    method="Counted X and Y again from the data.", notes="I also count 7 and 5 on the data.")
    assert st == 200, out
    st, b = call(base, "/api/boards")
    assert st == 200
    ada = next(r for r in b["researcher"] if r["name"] == "Ada")
    blaise = next(r for r in b["researcher"] if r["name"] == "Blaise")
    assert ada["claims"] == {"L2": 1} and ada["points"] == 3
    assert blaise["reviews_given"] == 1 and blaise["review_quality"] == 1 and blaise["points"] == 2
    assert next(r for r in b["org"] if r["key"] == "o:u1")["points"] == 3
    assert next(r for r in b["agent"] if "ada-agent" in r["name"])["claims"] == {"L2": 1}


def test_read_api_and_pages(mesh):
    m, base, t, q = mesh
    qid = q["id"]
    _ada_investigation(base, qid, t["ada"])
    st, invs = call(base, f"/q/{qid}/api/graph/investigations")
    assert st == 200 and len(invs["investigations"]) == 2
    for path in ("/", f"/q/{qid}", "/boards", "/terms", "/healthz", f"/q/{qid}/bundle.json", "/app/hypgraph.js", "/assets/mesh.css"):
        with urllib.request.urlopen(base + path) as r:
            assert r.status == 200, path
    with urllib.request.urlopen(base + f"/q/{qid}/api/graph/stream?max_seconds=0.3") as r:
        body = r.read().decode()
    assert "event: record" in body and "event: end" in body
