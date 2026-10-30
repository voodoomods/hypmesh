"""The questions: one HypGraph record each, opened by a curator, held open for anyone with an identity to work on.

A question record carries a curator's investigation that states the question. Every swarm or agent that works on
it starts its own investigation in the same record (that is what `hypswarm run` does), so the record shows every
attempt side by side and the mesh ranks the claims across them.

The curator is the only role that opens or closes a question. It cannot settle one: "solved" is derived from the
record (standing.py), never declared. The curator also keeps the hazard list: a question whose title or objective
names an excluded domain is refused at opening, and every opening states that the hazard review was done."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from hypgraph import Graph, Policy

CURATOR = "curator"
INSTRUMENT = "instrument"            # the same name HypSwarm's instrument writes as, so its runs are the mesh's lab runs
POLICY = Policy(reviewers=frozenset(), instrument=INSTRUMENT, operator=CURATOR, max_creates_per_minute=600)

# Domains the mesh does not host. A curator can extend the list (config); never shorten it below this.
EXCLUDED_DEFAULT = (
    "pathogen enhancement", "gain of function", "bioweapon", "biological weapon", "chemical weapon", "nerve agent",
    "toxin synthesis", "nuclear weapon", "weapons-grade", "explosive synthesis", "malware", "exploit development",
    "surveillance of individuals", "deanonymi", "doxx",
)


class Questions:
    def __init__(self, data_dir: Path, excluded: tuple[str, ...] = ()):
        self.dir = Path(data_dir) / "questions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = Path(data_dir) / "questions.json"
        self.excluded = tuple(sorted(set(EXCLUDED_DEFAULT) | set(x.lower() for x in excluded)))
        self._graphs: dict[str, Graph] = {}
        self._lock = threading.Lock()

    # ── the index ──
    def _load(self) -> list[dict]:
        if not self.index.exists():
            return []
        return json.loads(self.index.read_text(encoding="utf-8"))

    def _save(self, rows: list[dict]) -> None:
        tmp = self.index.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.index)

    def list(self) -> list[dict]:
        return self._load()

    def get(self, qid: str) -> Optional[dict]:
        return next((q for q in self._load() if q["id"] == qid), None)

    def hazard(self, *texts: str) -> str:
        """The excluded domain a text names, or ''."""
        blob = " ".join(texts).lower()
        return next((x for x in self.excluded if x in blob), "")

    def open(self, title: str, objective: str, *, domain: str = "", opened_by: str = "", hazard_reviewed: bool = False,
             qid: str = "", part_of: str = "") -> dict:
        if not hazard_reviewed:
            raise ValueError("a question is opened only after its hazard review; pass hazard_reviewed=True to say it was done")
        hit = self.hazard(title, objective, domain)
        if hit:
            raise ValueError(f"the mesh does not host this domain: {hit!r}")
        qid = qid or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
        if not qid:
            raise ValueError("the title needs some letters")
        rows = self._load()
        if any(q["id"] == qid for q in rows):
            raise ValueError(f"question {qid!r} already exists")
        if part_of and not any(q["id"] == part_of for q in rows):
            raise ValueError(f"no question {part_of!r} for this one to be part of")
        path = self.dir / f"{qid}.hypgraph"
        g = Graph(str(path), policy=POLICY)
        inv = g.author(CURATOR).investigation(title, objective, reviewers=[])
        g.close()
        row = {"id": qid, "title": title, "objective": objective, "domain": domain, "opened_by": opened_by,
               "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "curator_inv": inv["id"], "state": "open",
               "part_of": part_of or ""}
        rows.append(row)
        self._save(rows)
        return row

    def close(self, qid: str, why: str) -> dict:
        rows = self._load()
        q = next((q for q in rows if q["id"] == qid), None)
        if not q:
            raise ValueError(f"no question {qid!r}")
        q["state"], q["closed_why"], q["closed_at"] = "closed", why, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save(rows)
        return q

    # ── records ──
    def path(self, qid: str) -> Path:
        return self.dir / f"{qid}.hypgraph"

    def graph(self, qid: str) -> Graph:
        """One open Graph per question, shared by every request (hypgraph's connection is thread-safe and locked)."""
        with self._lock:
            g = self._graphs.get(qid)
            if g is None:
                if not self.path(qid).exists():
                    raise KeyError(qid)
                g = Graph(str(self.path(qid)), policy=POLICY)
                self._graphs[qid] = g
            return g

    def close_all(self) -> None:
        with self._lock:
            for g in self._graphs.values():
                g.close()
            self._graphs.clear()


__all__ = ["Questions", "CURATOR", "INSTRUMENT", "POLICY", "EXCLUDED_DEFAULT"]
