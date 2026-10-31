"""The gate every write passes before it reaches a question's record.

HypGraph's own rules run after this (numbers bound to measurements, no self-review by author name, reviews pinned
to versions, refusals logged). The mesh adds the rules that need identity:

- a bearer token names an agent under a researcher; no token, no write;
- reserved authors (curator, instrument) are written only by the mesh's own tokens;
- an identity edits only what it created: revise, withdraw, retract, lease and the work orders of its own
  investigations; creating runs, results and claims anywhere is open, and so is reviewing;
- a review by another identity is made official (the mesh, as curator, adds the reviewer to the investigation's
  board) when that identity is eligible; a review by the author's own identity stays whatever the record makes of
  it (official if the swarm listed that seat as a reviewer — it counts as L1, never as L2);
- caps on open work: no more than N active, unreviewed claims per identity per question, and a writes-per-minute
  ceiling per identity. Trust unlocks nothing here; the caps are the same for everyone;
- a source that pins a node of another question *of this mesh* is resolved here: the pin (version, hash) is
  filled from the live node when the writer gave only record and node, checked against it when given, and refused
  when the node is withdrawn; the reference is then indexed (network.py). A reference to another host is taken as
  written and never fetched by the server (no server-side requests to writer-chosen URLs)."""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Optional, Union

from hypgraph import GraphError
from hypgraph import blind as B
from hypgraph import derive as D
from hypgraph.references import is_reference, verify_reference
from hypgraph.server import dispatch

from .identity import Identities, Identity
from .network import References, question_of
from .questions import CURATOR, INSTRUMENT, Questions

OWN_ONLY = {"revise", "withdraw", "retract", "lease", "issue_work", "return_work", "accept_work", "rework", "cancel_work",
            "cancel_moot", "settle"}
RESERVED = {CURATOR, INSTRUMENT, "importer", "system"}


@dataclass
class Refusal(Exception):
    status: int
    detail: str


class Gate:
    def __init__(self, questions: Questions, ids: Identities, *, open_claims: int = 3, writes_per_minute: int = 120,
                 instrument_token: str = "", mesh_url: Union[str, Callable[[], str]] = "", refs: Optional[References] = None):
        self.q = questions
        self.ids = ids
        self.open_claims = open_claims
        self.writes_per_minute = writes_per_minute
        self.instrument_token = instrument_token
        self._mesh_url = mesh_url                    # a callable: the mesh's base URL can be set after the gate is built
        self.refs = refs
        self._recent: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    # ── who ──
    def identify(self, token: str) -> Identity:
        if self.instrument_token and token and token == self.instrument_token:
            return Identity("dev:instrument", "the mesh's instrument", None, "instrument", INSTRUMENT)
        who = self.ids.identity_for_token(token)
        if not who:
            raise Refusal(401, "a bearer token minted under a researcher is required to write (hypmesh identity mint-agent, or /me)")
        return who

    def _rate(self, who: Identity) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._recent[who.rid]
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= self.writes_per_minute:
                raise Refusal(429, f"{self.writes_per_minute} writes a minute per identity; wait")
            dq.append(now)

    # ── the gate ──
    def write(self, qid: str, token: str, body: dict) -> tuple[int, object]:
        who = self.identify(token)
        g = self.q.graph(qid)
        q = self.q.get(qid)
        if q and q.get("state") == "closed":
            raise Refusal(423, f"question {qid} is closed: {q.get('closed_why', '')}")
        op = str(body.get("op") or "")
        author = str(body.get("author") or "").strip().lower()
        if who.agent == "instrument":
            if author != INSTRUMENT:
                raise Refusal(403, f"the instrument token writes as {INSTRUMENT!r} only")
            allowed = frozenset({INSTRUMENT})
        else:
            if author in RESERVED or not author:
                raise Refusal(403, f"{author!r} is reserved for the mesh")
            allowed = frozenset({author})
            self._rate(who)
            if op in OWN_ONLY:
                self._own(g, qid, who, body)
            if op == "create" and str(body.get("type")) == "claim":
                self._cap(g, qid, who)
            if op == "review":
                self._review(g, qid, who, author, body)
            if op in ("create", "revise") and isinstance(body.get("fields"), dict) and body["fields"].get("record"):
                self._reference(g, qid, body, who)
        with g.sealed_from(self._sealed_from(g, qid, who)):
            status, out = dispatch(g, "POST", "/write", body=body, writable=True, allowed=allowed)
        if status == 200 and isinstance(out, dict) and out.get("id"):
            if op in ("create", "revise") and self.refs is not None:
                self._index(g, qid, out["id"])
            if op == "review":
                # review() answers with the reviewed node; the review itself is the newest `reviews` edge by this author
                edges = [e for e in out.get("in", []) if e.get("rel") == "reviews" and e.get("by") == author]
                if edges:
                    self.ids.record(qid, max(edges, key=lambda e: e.get("id", 0))["src"], "review", author, who)
            else:
                self.ids.record(qid, out["id"], str(body.get("type") or op), author, who)
        return status, out

    def _sealed_from(self, g, qid: str, who: Identity) -> dict:
        """What a blind order seals from this agent token, whatever name it writes under: no check may read it on the
        token's behalf (hypgraph's Graph.sealed_from). The instrument reproduces what is filed; it isn't a reader."""
        if who.agent == "instrument":
            return {}
        s = g.snapshot()
        if not any(w.sealing for w in B.windows(s)):
            return {}
        mine = self.ids.nodes_of_agent(qid, who.agent)
        return B.view_for(s, lambda i, n: i in mine).hide

    @property
    def mesh_url(self) -> str:
        return self._mesh_url() if callable(self._mesh_url) else str(self._mesh_url or "")

    def _reference(self, g, qid: str, body: dict, who: Identity) -> None:
        """A source pinning a node of this mesh: fill or check the pin against the live node, refuse a fallen one, and
        refuse one sealed from the writer (the pin would copy its words into a record that isn't sealed)."""
        f = body["fields"]
        if body.get("op") == "create" and str(body.get("type")) != "source":
            return
        target_q = question_of(self.mesh_url, str(f.get("record") or ""))
        if target_q is None:
            return                                   # another host: taken as written, hypgraph checks the shape
        try:
            g2 = self.q.graph(target_q)
        except KeyError:
            raise Refusal(404, f"no question {target_q!r} on this mesh to cite")
        try:
            nid = g2.resolve(str(f.get("node") or ""), "")
        except Exception as e:
            raise Refusal(404, f"no node {f.get('node')!r} in question {target_q}: {e}")
        s2 = g2.snapshot()
        if any(w.sealing for w in B.windows(s2)):
            mine = self.ids.nodes_of_agent(target_q, who.agent)
            v = B.view_for(s2, lambda i, n: i in mine)
            if nid in v.hide:
                raise Refusal(409, f"{target_q}/{s2.nodes[nid]['code'] or nid} is sealed while blind work order "
                                   f"{v.hide[nid]} is open there; cite it once that order is returned")
        life = g2.state(nid).get("lifecycle", "active")
        if life != "active":
            raise Refusal(409, f"{target_q}/{nid} is {life}; cite what stands, or cite the node that replaced it")
        pin = g2.pin(nid, f"{self.mesh_url}/q/{target_q}")
        if f.get("version") in (None, "", 0) and not f.get("hash"):
            for k, v in pin.items():                  # the writer named the node; the mesh pins the version it reads now
                if f.get(k) in (None, "", [], {}):
                    f[k] = v
            f["record"], f["node"] = pin["record"], nid   # the resolved id, not a display code
            return

        def local(url: str) -> dict:                 # verify against our own copy, never over the network
            path = url.split("/api/graph", 1)[1].partition("?")[0]
            status, obj = dispatch(g2, "GET", path, query={"history": ["1"]})
            if status != 200:
                raise KeyError(obj)
            return json.loads(json.dumps(obj, default=str))
        v = verify_reference({**f, "record": pin["record"], "node": nid}, fetch=local)
        if not v.get("ok"):
            raise Refusal(409, f"the pin does not match {target_q}/{nid}: {v.get('problem')}")
        f["record"], f["node"] = pin["record"], nid

    def _index(self, g, qid: str, nid: str) -> None:
        try:
            n = g.get(nid)
        except KeyError:
            return
        if n.get("type") != "source" or not is_reference(n.get("body") or {}):
            return
        b = n["body"]
        self.refs.put(qid, nid, record=b["record"], to_question=question_of(self.mesh_url, b["record"]) or "",
                      node=b["node"], version=int(b["version"]), hash=b["hash"])

    def _own(self, g, qid: str, who: Identity, body: dict) -> None:
        target = body.get("node") or body.get("order") or body.get("inv") or ""
        if body.get("edge") and not target:
            return                                   # retracting an edge: the record's own rules apply
        if not target:
            return
        try:
            nid = g.resolve(str(target))
        except Exception:
            nid = str(target)
        owner = self.ids.owner(qid, nid)
        if owner is None:
            s = g.snapshot()
            n = s.nodes.get(nid) or {}
            if n.get("created_by") == CURATOR:
                raise Refusal(403, "the curator's investigation is edited by the curator; start your own investigation in this record")
            return                                   # written before the ledger, or by the mesh itself
        if owner.rid != who.rid:
            raise Refusal(403, f"{nid} belongs to another researcher; you may create, link and review, not edit")

    def _cap(self, g, qid: str, who: Identity) -> None:
        s = g.snapshot()
        open_ = 0
        for nid in self.ids.nodes_by(qid, who.rid, "claim"):
            if nid not in s.nodes:
                continue
            st = D.state(s, nid)
            if st.get("lifecycle", "active") == "active" and ((st.get("review") or {}).get("state") or "unchecked") == "unchecked":
                open_ += 1
        if open_ >= self.open_claims:
            raise Refusal(429, f"{open_} of your claims in this question await review; the cap is {self.open_claims}. "
                               "Get them checked, or withdraw one, before claiming more")

    def _review(self, g, qid: str, who: Identity, author: str, body: dict) -> None:
        try:
            target = g.resolve(str(body.get("target") or ""))
        except Exception as e:
            raise Refusal(404, f"no such node to review: {e}")
        owner = self.ids.owner(qid, target)
        if owner is None or owner.rid == who.rid:
            return                                   # the author's own identity: the record decides (L1 at most)
        from .standing import boards, eligible_reviewer
        ok, why = eligible_reviewer(boards(self.q, self.ids), who.rid)
        if not ok:
            raise Refusal(403, f"not eligible to review: {why}")
        s = g.snapshot()
        inv = (s.nodes.get(target) or {}).get("inv")
        inv_node = s.nodes.get(inv) or {}
        current = list((inv_node.get("body") or {}).get("reviewers") or [])
        if author not in current:
            try:
                g.revise(author=CURATOR, node=inv, fields={"reviewers": sorted(set(current) | {author})},
                         reason=f"the mesh seats {author} ({who.name}, another identity) as a reviewer")
            except GraphError as e:
                raise Refusal(409, f"could not seat the reviewer: {e}")


__all__ = ["Gate", "Refusal", "OWN_ONLY"]
