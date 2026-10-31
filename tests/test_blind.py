"""Blind work orders on the mesh: while one is open, every reader but the agent that wrote a node sees it sealed — the
checker's seat, other researchers, and anyone reading the public page."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from hypgraph import GraphError

from hypmesh.seat import RemoteGraph
from hypmesh.server import Mesh, make_server

ADA, BLAISE = "dev:ada", "dev:blaise"
OBJECTIVE = "Count the primes below 10,000 by a method of your own, and record the count and the largest one."


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path, base_url="http://127.0.0.1:0")
    m.ids.add_org("u1", "University One")
    m.ids.add_org("l2", "Lab Two")
    m.ids.add_researcher(ADA, "Ada", "u1")
    m.ids.add_researcher(BLAISE, "Blaise", "l2")
    tokens = {"claude": m.ids.mint_agent(ADA, "claude-code")[1], "codex": m.ids.mint_agent(BLAISE, "codex")[1]}
    m.q.open("How many primes are there below 10,000?", "Count the primes below 10,000 with code anyone can rerun.",
             hazard_reviewed=True, qid="primes")
    srv = make_server(m, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    m.base_url = base
    yield m, base, tokens
    srv.shutdown()
    m.close()


def seat(base, tokens, who):
    return RemoteGraph(f"{base}/q/primes", tokens[who], seat=who)


def setup_lead(base, tokens):
    """Claude's attempt: a hypothesis that predicts the count and a result that measured it."""
    c = seat(base, tokens, "claude")
    inv = c.create(author="claude", type="investigation", fields={
        "title": "Claude counts the primes", "objective": "Count the primes below 10,000 and check the count blind.",
        "reviewers": ["codex"]})["id"]
    h1 = c.create(author="claude", type="hypothesis", inv=inv, fields={
        "statement": "There are exactly 1229 primes below 10,000, the largest 9973.",
        "predictions": ["A sieve counts 1229, the largest being 9973."]})["id"]
    run = c.create(author="claude", type="run", inv=inv, fields={"kind": "agent", "name": "sieve", "state": "succeeded"})["id"]
    rs1 = c.create(author="claude", type="result", inv=inv, fields={
        "summary": "The sieve counts 1229 primes below 10,000; the largest is 9973.", "outcome": "positive",
        "values": {"count": 1229, "largest": 9973}},
        links=[{"rel": "produced", "from": run}, {"rel": "supports", "to": h1, "why": "the count it predicts"}])["id"]
    return c, inv, h1, rs1


def get(url, token=""):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_sealed_from_the_checker_and_the_public_until_it_returns(mesh):
    m, base, t = mesh
    c, inv, h1, rs1 = setup_lead(base, t)
    o = c.issue_work(author="claude", inv=inv, target="H1", assignee="codex", objective=OBJECTIVE, blind=True)
    x = seat(base, t, "codex")
    assert "BLIND" in x.order_text(o["order"]) and "1229" not in x.order_text(o["order"])
    assert x.get(rs1)["body"] == {"sealed": f"until {o['code']} is returned"}
    assert "1229" not in json.dumps(x.subgraph(inv)) and x.search("sieve") == []
    with pytest.raises(GraphError, match="sealed while blind work order"):
        x.report(inv)
    # the public page's reads, with no token at all
    status, body = get(f"{base}/q/primes/api/graph/node/{rs1}")
    assert status == 200 and "1229" not in body
    assert get(f"{base}/q/primes/bundle.json")[0] == 409
    assert get(f"{base}/q/primes/api/graph/telemetry")[0] == 409
    status, body = get(f"{base}/q/primes/api/graph/stream?max_seconds=0.3&poll=0.1")
    assert status == 200 and "event: record" in body and "1229" not in body and "9973" not in body
    # the agent that wrote it still reads it
    assert c.get(rs1)["body"]["values"]["count"] == 1229
    # the checker measures, blind
    run = x.create(author="codex", type="run", inv=inv, fields={"kind": "agent", "name": "trial division", "state": "succeeded"})["id"]
    rs2 = x.create(author="codex", type="result", inv=inv, fields={
        "summary": "Trial division counts 1229 primes below 10,000; the largest is 9973.", "outcome": "positive",
        "values": {"count": 1229, "largest": 9973}}, links=[{"rel": "produced", "from": run}])
    assert rs2["state"]["blind"] == o["code"] and rs2["state"]["sealed"]
    assert "agrees_blind" not in rs2["state"] and "restates" not in rs2["state"]   # no comparison while sealed
    x.return_work(author="codex", order=o["order"], note="Counted by trial division.")
    assert x.get(rs1)["body"]["values"]["count"] == 1229                     # returned: the seal lifts
    assert x.get(rs2["id"])["state"]["agrees_blind"] == [rs1]
    assert get(f"{base}/q/primes/bundle.json")[0] == 200


def test_a_seat_that_already_looked_cannot_be_given_a_blind_order(mesh):
    m, base, t = mesh
    c, inv, h1, rs1 = setup_lead(base, t)
    x = seat(base, t, "codex")
    x.investigations()                                  # a seat's start-up check is not a look
    c.issue_work(author="claude", inv=inv, target="H1", assignee="codex", objective=OBJECTIVE,
                 blind=True, lease_minutes=30)
    # a second seat name that has read the record is refused
    y = seat(base, t, "codex")
    y.seat = "gpt"
    y.subgraph(inv)
    run = c.create(author="claude", type="run", inv=inv, fields={"kind": "agent", "name": "second sieve", "state": "succeeded"})["id"]
    with pytest.raises(GraphError, match="gpt has already read this question's record"):
        c.issue_work(author="claude", inv=inv, target=run, assignee="gpt", objective=OBJECTIVE, blind=True)


def test_standing_table_hides_sealed_statements(mesh):
    m, base, t = mesh
    c, inv, h1, rs1 = setup_lead(base, t)
    c.create(author="claude", type="claim", inv=inv, fields={
        "statement": "There are 1229 primes below 10,000.", "scope": "the integers below 10,000"},
        links=[{"rel": "supports", "from": rs1, "why": "the sieve's count"}])
    c.issue_work(author="claude", inv=inv, target="H1", assignee="codex", objective=OBJECTIVE, blind=True)
    status, body = get(f"{base}/q/primes/standing")
    rows = json.loads(body)["all"]
    assert status == 200 and rows and all(r["statement"].startswith("sealed until") for r in rows)
    assert "1229" not in body


def post(url, token, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_a_sealed_token_learns_nothing_by_guessing(mesh):
    """A right guess of a sealed number answers like a wrong one, through reads, refused writes, and writes under
    another name than the order's (the token is what is sealed)."""
    m, base, t = mesh
    c, inv, h1, rs1 = setup_lead(base, t)
    c.issue_work(author="claude", inv=inv, target="H1", assignee="codex", objective=OBJECTIVE, blind=True)
    g = f"{base}/q/primes/api/graph"

    def answers(x):
        claim = {"op": "create", "type": "claim", "inv": inv, "fields": {
            "statement": f"There are {x} primes below 10,000.", "scope": "the integers below 10,000", "kind": "finding",
            "measurements": [{"result": rs1, "key": "count"}]}}
        out = [get(f"{g}/resolve?ref={x}", t["codex"]), get(f"{g}/node/{x}", ""),
               post(f"{g}/write", t["codex"], {"op": "create", "author": "codex", "type": f"t {x}", "inv": inv, "fields": {}}),
               post(f"{g}/write", t["codex"], {**claim, "author": "codex"}),
               post(f"{g}/write", t["codex"], {**claim, "author": "probe"})]
        return [(st, body.replace(str(x), "<X>")) for st, body in out]
    right, wrong = answers(1229), answers(1231)
    assert right == wrong
    assert right[4][0] == 400 and "sealed from you" in right[4][1]       # the other name is sealed too
