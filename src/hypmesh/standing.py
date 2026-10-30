"""What the mesh derives from a question's record and the ledger — never set by anyone.

A claim's mesh standing, from the record's own derived review state plus who the reviewers were:

    withdrawn   the author withdrew it (or it rests on something that fell)
    contested   an independent reviewer objected and the objection stands
    L0          proposed: active, no official positive review
    L1          checked: an official positive review — possibly by the author's own swarm's reviewer seat
    L2          verified: a positive review by someone else (another researcher), none against
    L3          replicated: L2, positive reviews from two different organisations, and the mesh's instrument
                reproduced the computation the claim rests on

"Independent" means another researcher; "another organisation" means another org, or another unaffiliated person.
A question is solved when a claim in it reaches L3. The boards count what survived, not what was written.

Across questions (network.py): a claim that cites a pinned reference to a claim of another question of this mesh
*rests on* it. A chain is as strong as its weakest link, so the claim's level is capped at the lowest level of the
claims it rests on; if one of them fell (withdrawn, contested), or its pinned version no longer matches, the
citing claim is ``at_risk``. A reference that has been revised since it was read is reported, not penalised."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Optional

from hypgraph import Graph
from hypgraph import derive as D
from hypgraph.references import body_hash

from .identity import Identities, Identity
from .questions import INSTRUMENT

LEVELS = ("withdrawn", "contested", "at_risk", "L0", "L1", "L2", "L3")
WEIGHT = {"L3": 5, "L2": 3, "L1": 1, "L0": 0, "at_risk": -1, "contested": -1, "withdrawn": -2}
_L = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


@dataclass
class ClaimStanding:
    id: str
    code: str
    inv: str
    statement: str
    level: str
    owner: Optional[str]               # researcher id
    owner_org: str
    reviews_all: int                   # official positive reviews, any identity
    independent: list[dict]            # [{rid, org, verdict, review}]
    orgs: list[str]                    # distinct org keys among independent positives
    reproduced: bool
    record_review: str                 # hypgraph's own review state for the current version
    rests_on: list = field(default_factory=list)     # [{question, node, code, level, revised, withdrawn, hash_ok}]
    own_level: str = ""                # the level before the cross-question cap (equal to level when uncapped)
    capped_by: str = ""                # "question/code" of the weakest premise, when the cap bit


def _reviews_of(s, claim: str) -> list[dict]:
    """Official reviews pinned to the claim's current version: the record says which authors count (D.state)."""
    st = D.state(s, claim)
    rv = st.get("review") or {}
    counted = {x.split(":", 1)[0]: x.split(":", 1)[1] for x in (rv.get("positive") or []) + (rv.get("negative") or []) if ":" in x}
    out = []
    for e in s.inn.get(claim, []):
        if e["rel"] != "reviews":
            continue
        n = s.nodes.get(e["src"]) or {}
        body = n.get("body") or {}
        author = n.get("created_by", "")
        if not body.get("official") or author not in counted:
            continue
        out.append({"review": e["src"], "author": author, "verdict": counted[author]})
    return out


def _reproduced(s, claim: str) -> bool:
    """An instrument run replicating the run behind the claim's result, with nothing contradicted."""
    results = [e["dst"] for e in s.out.get(claim, []) if e["rel"] == "derived_from"]
    runs = {e["src"] for r in results for e in s.inn.get(r, []) if e["rel"] == "produced"}
    for run in runs:
        for e in s.inn.get(run, []):
            if e["rel"] != "replicates":
                continue
            lab = s.nodes.get(e["src"]) or {}
            if lab.get("created_by") != INSTRUMENT:
                continue
            cfg = (lab.get("body") or {}).get("config") or {}
            if not cfg.get("contradicted") and cfg.get("confirmed"):
                return True
    return False


POSITIVE = {"checked", "replicated"}
NEGATIVE = {"objection", "contested"}


def _rests_on(g, s, claim: str, qid: str, ids: Identities, questions, refs, memo: dict) -> list[dict]:
    """The claims of other questions this claim cites by pinned reference, with their standing as it is now."""
    out = []
    for e in s.out.get(claim, []):
        if e["rel"] != "cites":
            continue
        src = s.nodes.get(e["dst"]) or {}
        b = src.get("body") or {}
        if src.get("type") != "source" or not b.get("record") or not b.get("hash"):
            continue
        row = refs.get(qid, e["dst"]) if refs is not None else None
        tq = (row or {}).get("to_question") or ""
        if not tq or tq == qid:
            continue
        gone = {"question": tq, "node": b["node"], "code": "", "level": "", "revised": False, "withdrawn": True, "hash_ok": False}
        try:
            g2 = questions.graph(tq)
        except KeyError:
            out.append(gone)
            continue
        s2 = g2.snapshot()
        n2 = s2.nodes.get(b["node"])
        if n2 is None:
            out.append(gone)
            continue
        pinned = next((v for v in s2.versions.get(b["node"], []) if int(v["v"]) == int(b["version"])), None)
        hash_ok = bool(pinned) and body_hash(pinned["body"]) == b["hash"]
        level = ""
        if n2.get("type") == "claim":
            level = next((c.level for c in claims(g2, ids, tq, questions=questions, refs=refs, _memo=memo) if c.id == b["node"]), "")
        out.append({"question": tq, "node": b["node"], "code": n2.get("code", ""), "level": level,
                    "revised": int(n2.get("v", 1)) > int(b["version"]),
                    "withdrawn": D.lifecycle(s2, b["node"]) != "active", "hash_ok": hash_ok})
    return out


def claims(g: Graph, ids: Identities, qid: str, *, questions=None, refs=None, _memo: Optional[dict] = None) -> list[ClaimStanding]:
    """Every claim's standing in ``qid``. With ``questions`` and ``refs`` the cross-question rule applies; ``_memo``
    shares the work across questions (and stops a cycle of references from recursing)."""
    memo = {} if _memo is None else _memo
    if qid in memo:
        return memo[qid]
    memo[qid] = []                                   # a cycle reads the question it is inside as "no claims yet"
    s = g.snapshot()
    out = []
    for nid, n in s.nodes.items():
        if n.get("type") != "claim":
            continue
        st = D.state(s, nid)
        owner = ids.owner(qid, nid)
        reviews = _reviews_of(s, nid)
        independent = []
        for r in reviews:
            who = ids.owner(qid, r["review"])
            if who and owner and who.rid != owner.rid:
                independent.append({"rid": who.rid, "handle": who.handle, "org": who.org_key, "verdict": r["verdict"], "review": r["review"]})
        pos = [r for r in independent if r["verdict"] in POSITIVE]
        neg = [r for r in independent if r["verdict"] in NEGATIVE]
        orgs = sorted({r["org"] for r in pos})
        reproduced = _reproduced(s, nid)
        life = st.get("lifecycle", "active")
        rr = (st.get("review") or {}).get("state") or "unchecked"
        if life != "active":
            level = "withdrawn"
        elif neg:
            level = "contested"
        elif pos and len(orgs) >= 2 and reproduced:
            level = "L3"
        elif pos:
            level = "L2"
        elif any(r["verdict"] in POSITIVE for r in reviews):
            level = "L1"
        else:
            level = "L0"
        own, capped_by = level, ""
        rests = _rests_on(g, s, nid, qid, ids, questions, refs, memo) if questions is not None else []
        if life == "active":
            for r in rests:
                if r["withdrawn"] or not r["hash_ok"] or r["level"] in ("withdrawn", "contested", "at_risk"):
                    level, capped_by = "at_risk", f"{r['question']}/{r['code'] or r['node']}"
                    break
                if r["level"] in _L and level in _L and _L[r["level"]] < _L[level]:
                    level, capped_by = r["level"], f"{r['question']}/{r['code'] or r['node']}"
        out.append(ClaimStanding(nid, n.get("code", ""), n.get("inv", ""), (n.get("body") or {}).get("statement", ""), level,
                                 owner.rid if owner else None, owner.org_key if owner else "",
                                 sum(1 for r in reviews if r["verdict"] in POSITIVE), independent, orgs, reproduced, rr,
                                 rests, own, capped_by))
    order = {lv: i for i, lv in enumerate(reversed(LEVELS))}
    out.sort(key=lambda c: (order.get(c.level, 9), c.code))
    memo[qid] = out
    return out


def question_status(g: Graph, ids: Identities, qid: str, *, questions=None, refs=None, _memo: Optional[dict] = None) -> dict:
    cs = claims(g, ids, qid, questions=questions, refs=refs, _memo=_memo)
    best = next((c.level for c in cs if c.level.startswith("L")), None)
    counts = Counter(c.level for c in cs)
    return {"solved": any(c.level == "L3" for c in cs), "best": best, "claims": len(cs), "by_level": dict(counts),
            "investigations": len(g.investigations()), "participants": len({c.owner for c in cs if c.owner}),
            "top": [asdict(c) for c in cs[:5]]}


@dataclass
class Score:
    key: str
    name: str
    kind: str                           # agent | researcher | org
    claims: Counter = field(default_factory=Counter)
    reviews_given: int = 0              # independent official reviews
    reviews_stood: int = 0              # … whose target still stands at L1 or better
    reviews_fell: int = 0               # … positive reviews whose target later fell (withdrawn / contested)
    refusals: int = 0                   # writes the record refused (honest reaching, counted, not punished)
    questions: set = field(default_factory=set)

    @property
    def claim_points(self) -> int:
        return sum(WEIGHT.get(lv, 0) * n for lv, n in self.claims.items())

    @property
    def review_quality(self) -> int:
        return self.reviews_stood - self.reviews_fell

    @property
    def points(self) -> int:
        return self.claim_points + 2 * self.review_quality

    def row(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind, "points": self.points, "claim_points": self.claim_points,
                "claims": dict(self.claims), "reviews_given": self.reviews_given, "reviews_stood": self.reviews_stood,
                "reviews_fell": self.reviews_fell, "review_quality": self.review_quality, "refusals": self.refusals,
                "questions": len(self.questions)}


def eligible_reviewer(scores: dict[str, Score], rid: str) -> tuple[bool, str]:
    """Review eligibility is the thing that is earned: a verified identity whose independent reviews have not been
    overturned more than twice net. A newcomer is eligible; a reviewer who keeps blessing claims that fall is not."""
    sc = scores.get(f"r:{rid}")
    if sc and sc.review_quality <= -3:
        return False, f"your independent reviews were overturned {sc.reviews_fell} times against {sc.reviews_stood} that stood"
    return True, ""


def boards(questions, ids: Identities, refs=None) -> dict[str, Score]:
    """Every agent, researcher and organisation with a row, from every question's record and the ledger."""
    scores: dict[str, Score] = {}
    memo: dict = {}

    def sc(key: str, name: str, kind: str) -> Score:
        if key not in scores:
            scores[key] = Score(key, name, kind)
        return scores[key]

    def credit(who: Identity, q: str, fn) -> None:
        a = sc(f"a:{who.agent}", f"{who.name} · {who.agent_name}", "agent") if who.agent else None
        r = sc(f"r:{who.rid}", who.name, "researcher")
        o = sc(f"o:{who.org}", (ids.org(who.org) or {}).get("name", who.org), "org") if who.org else None
        for x in (a, r, o):
            if x is not None:
                fn(x)
                x.questions.add(q)

    for q in questions.list():
        qid = q["id"]
        try:
            g = questions.graph(qid)
        except KeyError:
            continue
        cs = claims(g, ids, qid, questions=questions, refs=refs, _memo=memo)
        by_id = {c.id: c for c in cs}
        for c in cs:
            owner = ids.owner(qid, c.id)
            if owner:
                credit(owner, qid, lambda x, lv=c.level: x.claims.update([lv]))
            for r in c.independent:
                who = ids.owner(qid, r["review"])
                if not who:
                    continue
                stood = c.own_level in ("L1", "L2", "L3")          # a reviewer answers for the claim, not its premises
                fell = r["verdict"] in POSITIVE and c.own_level in ("withdrawn", "contested")
                def bump(x, stood=stood, fell=fell):
                    x.reviews_given += 1
                    x.reviews_stood += 1 if stood else 0
                    x.reviews_fell += 1 if fell else 0
                credit(who, qid, bump)
        # refusals: the record logs them per author string; the ledger maps that author (in that question) to an identity
        authors: dict[str, Identity] = {}
        for row in ids.ledger_rows(qid):
            who = ids.owner(qid, row["node"])
            if who:
                authors.setdefault(row["author"], who)
        try:
            for ref in g.refusals(limit=5000):
                who = authors.get(ref.get("author", ""))
                if who:
                    credit(who, qid, lambda x: setattr(x, "refusals", x.refusals + 1))
        except Exception:
            pass
    return scores


def board_rows(scores: dict[str, Score], kind: str, limit: int = 50) -> list[dict]:
    rows = [s.row() for s in scores.values() if s.kind == kind]
    rows.sort(key=lambda r: (-r["points"], -r["reviews_given"], r["name"]))
    return rows[:limit]


__all__ = ["ClaimStanding", "Score", "claims", "question_status", "boards", "board_rows", "eligible_reviewer", "LEVELS"]
