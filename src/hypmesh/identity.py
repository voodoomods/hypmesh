"""Who is accountable: organisations vouch for researchers (an ORCID each), researchers run agents (a bearer token
each), and a ledger remembers which identity wrote every node of every record.

Author names inside a record stay whatever the agent chose (a HypSwarm seat is "analyst" or "reviewer"); the mesh
never rewrites them, because a swarm's orders and reviews are addressed by those names. Attribution is the
ledger's job: node → (researcher, agent). Two researchers' agents may both be called "analyst"; the ledger tells
them apart, and so do the boards.

Tokens are stored hashed. A token is shown once, when minted."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS orgs (id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT DEFAULT '', created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS researchers (orcid TEXT PRIMARY KEY, name TEXT NOT NULL, org TEXT REFERENCES orgs(id), "
    "verified INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, orcid TEXT NOT NULL REFERENCES researchers(orcid), name TEXT NOT NULL, "
    "token_sha256 TEXT NOT NULL UNIQUE, created REAL NOT NULL, revoked REAL)",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, orcid TEXT NOT NULL, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ledger (question TEXT NOT NULL, node TEXT NOT NULL, type TEXT NOT NULL, author TEXT NOT NULL, "
    "orcid TEXT NOT NULL, agent TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (question, node))",
    "CREATE INDEX IF NOT EXISTS ledger_who ON ledger (orcid, question)",
]

ORCID_RE = r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$"


@dataclass(frozen=True)
class Identity:
    orcid: str
    name: str
    org: Optional[str]          # org id, or None for an unaffiliated researcher
    agent: str                  # agent id ("" for a web session)
    agent_name: str

    @property
    def org_key(self) -> str:
        """What counts as a distinct organisation for independence: the org, or the person when unaffiliated."""
        return self.org or f"independent:{self.orcid}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Identities:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        for stmt in SCHEMA:
            self._con.execute(stmt)

    # ── organisations ──
    def add_org(self, org_id: str, name: str, domain: str = "") -> dict:
        org_id = org_id.strip().lower()
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO orgs (id, name, domain, created) VALUES (?,?,?,?)",
                              (org_id, name.strip(), domain.strip().lower(), time.time()))
        return self.org(org_id)

    def org(self, org_id: str) -> Optional[dict]:
        r = self._con.execute("SELECT * FROM orgs WHERE id = ?", (org_id.strip().lower(),)).fetchone()
        return dict(r) if r else None

    def orgs(self) -> list[dict]:
        return [dict(r) for r in self._con.execute("SELECT * FROM orgs ORDER BY name")]

    # ── researchers ──
    def add_researcher(self, orcid: str, name: str, org: str = "", *, verified: bool = False) -> dict:
        import re
        orcid = orcid.strip().upper()
        if not re.match(ORCID_RE, orcid):
            raise ValueError(f"not an ORCID iD: {orcid!r} (0000-0000-0000-0000)")
        if org and not self.org(org):
            raise ValueError(f"no organisation {org!r}")
        with self._lock:
            self._con.execute(
                "INSERT INTO researchers (orcid, name, org, verified, created) VALUES (?,?,?,?,?) "
                "ON CONFLICT(orcid) DO UPDATE SET name = excluded.name, verified = max(researchers.verified, excluded.verified)",
                (orcid, name.strip(), org.strip().lower() or None, 1 if verified else 0, time.time()))
        return self.researcher(orcid)  # type: ignore[return-value]

    def vouch(self, orcid: str, org: str) -> dict:
        """An organisation takes responsibility for a researcher (and their agents)."""
        if not self.org(org):
            raise ValueError(f"no organisation {org!r}")
        with self._lock:
            self._con.execute("UPDATE researchers SET org = ? WHERE orcid = ?", (org.strip().lower(), orcid.strip().upper()))
        r = self.researcher(orcid)
        if not r:
            raise ValueError(f"no researcher {orcid}")
        return r

    def researcher(self, orcid: str) -> Optional[dict]:
        r = self._con.execute("SELECT * FROM researchers WHERE orcid = ?", (orcid.strip().upper(),)).fetchone()
        return dict(r) if r else None

    def researchers(self, org: str = "") -> list[dict]:
        if org:
            rows = self._con.execute("SELECT * FROM researchers WHERE org = ? ORDER BY name", (org.lower(),))
        else:
            rows = self._con.execute("SELECT * FROM researchers ORDER BY name")
        return [dict(r) for r in rows]

    # ── agents ──
    def mint_agent(self, orcid: str, name: str) -> tuple[dict, str]:
        """A new agent under a researcher. Returns (agent row, the token — shown once, stored hashed)."""
        if not self.researcher(orcid):
            raise ValueError(f"no researcher {orcid}")
        token = "hm_" + secrets.token_urlsafe(32)
        agent_id = "a" + secrets.token_hex(5)
        with self._lock:
            self._con.execute("INSERT INTO agents (id, orcid, name, token_sha256, created) VALUES (?,?,?,?,?)",
                              (agent_id, orcid.strip().upper(), name.strip(), _hash(token), time.time()))
        return self.agent(agent_id), token  # type: ignore[return-value]

    def revoke_agent(self, agent_id: str) -> bool:
        with self._lock:
            cur = self._con.execute("UPDATE agents SET revoked = ? WHERE id = ? AND revoked IS NULL", (time.time(), agent_id))
        return cur.rowcount > 0

    def agent(self, agent_id: str) -> Optional[dict]:
        r = self._con.execute("SELECT id, orcid, name, created, revoked FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(r) if r else None

    def agents(self, orcid: str) -> list[dict]:
        return [dict(r) for r in self._con.execute(
            "SELECT id, orcid, name, created, revoked FROM agents WHERE orcid = ? ORDER BY created", (orcid.strip().upper(),))]

    def identity_for_token(self, token: str) -> Optional[Identity]:
        if not token or not token.startswith("hm_"):
            return None
        r = self._con.execute(
            "SELECT a.id AS agent, a.name AS agent_name, r.orcid, r.name, r.org FROM agents a JOIN researchers r ON r.orcid = a.orcid "
            "WHERE a.token_sha256 = ? AND a.revoked IS NULL", (_hash(token),)).fetchone()
        return Identity(r["orcid"], r["name"], r["org"], r["agent"], r["agent_name"]) if r else None

    # ── web sessions ──
    def open_session(self, orcid: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._con.execute("INSERT INTO sessions (id, orcid, created) VALUES (?,?,?)", (sid, orcid.strip().upper(), time.time()))
        return sid

    def session(self, sid: str) -> Optional[Identity]:
        if not sid:
            return None
        r = self._con.execute("SELECT r.orcid, r.name, r.org FROM sessions s JOIN researchers r ON r.orcid = s.orcid WHERE s.id = ? "
                              "AND s.created > ?", (sid, time.time() - 30 * 86400)).fetchone()
        return Identity(r["orcid"], r["name"], r["org"], "", "web") if r else None

    def close_session(self, sid: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    # ── the ledger: who wrote which node ──
    def record(self, question: str, node: str, type_: str, author: str, who: Identity) -> None:
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO ledger (question, node, type, author, orcid, agent, at) VALUES (?,?,?,?,?,?,?)",
                              (question, node, type_, author, who.orcid, who.agent, time.time()))

    def owner(self, question: str, node: str) -> Optional[Identity]:
        r = self._con.execute(
            "SELECT l.orcid, l.agent, r.name, r.org, COALESCE(a.name, l.agent) AS agent_name FROM ledger l "
            "JOIN researchers r ON r.orcid = l.orcid LEFT JOIN agents a ON a.id = l.agent "
            "WHERE l.question = ? AND l.node = ?", (question, node)).fetchone()
        return Identity(r["orcid"], r["name"], r["org"], r["agent"], r["agent_name"]) if r else None

    def nodes_by(self, question: str, orcid: str, type_: str = "") -> list[str]:
        if type_:
            rows = self._con.execute("SELECT node FROM ledger WHERE question = ? AND orcid = ? AND type = ?", (question, orcid, type_))
        else:
            rows = self._con.execute("SELECT node FROM ledger WHERE question = ? AND orcid = ?", (question, orcid))
        return [r["node"] for r in rows]

    def ledger_rows(self, question: str) -> list[dict]:
        return [dict(r) for r in self._con.execute("SELECT * FROM ledger WHERE question = ?", (question,))]

    def close(self) -> None:
        self._con.close()


__all__ = ["Identities", "Identity", "ORCID_RE"]
