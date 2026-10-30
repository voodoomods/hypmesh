"""The network between questions, derived from pinned references.

A question's record is one trust boundary (one chain, one board). Questions join into a network by *reference*:
a claim in question B cites a node of question A through a HypGraph ``source`` that pins the record URL, the
node, the version read and that version's hash (hypgraph.references). The mesh holds every record, so it can do
what one record cannot: resolve a reference to this mesh at write time (fill the pin from the live node, refuse a
withdrawn one), keep an index of who cites what, and read standing across records.

The index here is a cache. ``rebuild`` recomputes it from the records alone, and nothing in it is authoritative:
the pins live in the citing records' sources, where the chain holds them."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from hypgraph.references import normalize_record

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS refs (question TEXT NOT NULL, source TEXT NOT NULL, record TEXT NOT NULL, "
    "to_question TEXT NOT NULL DEFAULT '', to_node TEXT NOT NULL, to_version INTEGER NOT NULL, to_hash TEXT NOT NULL, "
    "at REAL NOT NULL, PRIMARY KEY (question, source))",
    "CREATE INDEX IF NOT EXISTS refs_to ON refs (to_question, to_node)",
]


def question_of(mesh_url: str, record: str) -> Optional[str]:
    """The question id when ``record`` is one of this mesh's question URLs (``<mesh>/q/<id>``), else None."""
    try:
        rec = normalize_record(record)
        base = normalize_record(mesh_url)
    except ValueError:
        return None
    r, b = urlsplit(rec), urlsplit(base)
    if r.scheme != b.scheme or r.netloc.lower() != b.netloc.lower():
        return None
    prefix = b.path.rstrip("/") + "/q/"
    if not r.path.startswith(prefix):
        return None
    qid = r.path[len(prefix):]
    return qid if qid and "/" not in qid else None


class References:
    """Who cites what across questions: (question, source node) → (record, question, node, version, hash)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        for stmt in SCHEMA:
            self._con.execute(stmt)

    def put(self, question: str, source: str, *, record: str, to_question: str, node: str, version: int, hash: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO refs (question, source, record, to_question, to_node, to_version, to_hash, at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(question, source) DO UPDATE SET record = excluded.record, to_question = excluded.to_question, "
                "to_node = excluded.to_node, to_version = excluded.to_version, to_hash = excluded.to_hash, at = excluded.at",
                (question, source, record, to_question, node, int(version), hash, time.time()))

    def get(self, question: str, source: str) -> Optional[dict]:
        r = self._con.execute("SELECT * FROM refs WHERE question = ? AND source = ?", (question, source)).fetchone()
        return dict(r) if r else None

    def outgoing(self, question: str) -> list[dict]:
        return [dict(r) for r in self._con.execute("SELECT * FROM refs WHERE question = ? ORDER BY source", (question,))]

    def citing(self, to_question: str, node: str = "") -> list[dict]:
        """The references (in any question) that pin ``to_question``, or one node of it."""
        if node:
            rows = self._con.execute("SELECT * FROM refs WHERE to_question = ? AND to_node = ? ORDER BY question, source", (to_question, node))
        else:
            rows = self._con.execute("SELECT * FROM refs WHERE to_question = ? ORDER BY question, source", (to_question,))
        return [dict(r) for r in rows]

    def edges(self) -> list[dict]:
        """Question → question, with how many references join them (references to other hosts go to ``''``)."""
        return [dict(r) for r in self._con.execute(
            "SELECT question AS src, to_question AS dst, COUNT(*) AS n FROM refs GROUP BY question, to_question ORDER BY question, to_question")]

    def rebuild(self, questions, mesh_url: str) -> int:
        """Recompute the whole index from the records. Returns how many references were found."""
        rows = []
        for q in questions.list():
            try:
                g = questions.graph(q["id"])
            except KeyError:
                continue
            for ref in g.references():
                rows.append((q["id"], ref["id"], ref["record"], question_of(mesh_url, ref["record"]) or "", ref["node"],
                             int(ref["version"]), ref["hash"], time.time()))
        with self._lock:
            self._con.execute("DELETE FROM refs")
            self._con.executemany("INSERT INTO refs (question, source, record, to_question, to_node, to_version, to_hash, at) "
                                  "VALUES (?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def close(self) -> None:
        self._con.close()


__all__ = ["References", "question_of"]
