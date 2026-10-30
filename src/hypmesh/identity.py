"""Who is accountable: organisations vouch for researchers, researchers run agents (a bearer token each), and a
ledger remembers which identity wrote every node of every record.

A researcher is one person, named by a researcher id that says how the mesh knows them:

    github:<user id>     signed in with GitHub (the numeric user id, which survives a rename; the login is the handle)
    dev:<handle>         made on the host by an operator (`hypmesh identity add-researcher`), for dev and closed meshes

Author names inside a record stay whatever the agent chose (a HypSwarm seat is "analyst" or "reviewer"); the mesh
never rewrites them, because a swarm's orders and reviews are addressed by those names. Attribution is the
ledger's job: node → (researcher, agent). Two researchers' agents may both be called "analyst"; the ledger tells
them apart, and so do the boards.

Tokens are stored hashed. A token is shown once, when minted."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS orgs (id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT DEFAULT '', created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS researchers (rid TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL DEFAULT '', "
    "provider TEXT NOT NULL DEFAULT 'dev', org TEXT REFERENCES orgs(id), verified INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, rid TEXT NOT NULL REFERENCES researchers(rid), name TEXT NOT NULL, "
    "token_sha256 TEXT NOT NULL UNIQUE, created REAL NOT NULL, revoked REAL)",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, rid TEXT NOT NULL, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ledger (question TEXT NOT NULL, node TEXT NOT NULL, type TEXT NOT NULL, author TEXT NOT NULL, "
    "rid TEXT NOT NULL, agent TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (question, node))",
    "CREATE INDEX IF NOT EXISTS ledger_who ON ledger (rid, question)",
]

RID_RE = re.compile(r"^(github|dev):[A-Za-z0-9._-]{1,64}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROVIDERS = ("github", "dev")


@dataclass(frozen=True)
class Identity:
    rid: str                    # researcher id: github:<user id> | dev:<handle>
    name: str
    org: Optional[str]          # org id, or None for an unaffiliated researcher
    agent: str                  # agent id ("" for a web session)
    agent_name: str
    handle: str = ""            # the GitHub login, or the dev handle

    @property
    def org_key(self) -> str:
        """What counts as a distinct organisation for independence: the org, or the person when unaffiliated."""
        return self.org or f"independent:{self.rid}"

    @property
    def provider(self) -> str:
        return self.rid.split(":", 1)[0]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def dev_rid(handle: str) -> str:
    """The researcher id an operator gives a dev identity; a full researcher id passes through unchanged."""
    handle = handle.strip()
    if RID_RE.match(handle):
        return handle
    if not HANDLE_RE.match(handle):
        raise ValueError(f"not a handle: {handle!r} (letters, digits, '.', '_', '-'; up to 64)")
    return f"dev:{handle.lower()}"


class Identities:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._check_schema()
        for stmt in SCHEMA:
            self._con.execute(stmt)

    def _check_schema(self) -> None:
        """A data dir made before the identity rework (0.1.0a5) has no researcher ids. HypMesh is alpha, so there is
        no migration: refuse it plainly instead of failing later."""
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(researchers)")}
        if cols and "rid" not in cols:
            raise RuntimeError(f"{self.path.parent} was made by a HypMesh older than 0.1.0a5, before researcher ids; "
                               "move it aside and start a new data dir (alpha releases carry no migration)")

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
    def add_researcher(self, rid: str, name: str, org: str = "", *, handle: str = "", verified: bool = False) -> dict:
        """Add (or rename) a researcher. ``rid`` is a researcher id (``github:<id>``, ``dev:<handle>``) or a bare
        handle, which becomes ``dev:<handle>``."""
        rid = dev_rid(rid)
        provider = rid.split(":", 1)[0]
        handle = (handle or rid.split(":", 1)[1]).strip()
        if org and not self.org(org):
            raise ValueError(f"no organisation {org!r}")
        with self._lock:
            self._con.execute(
                "INSERT INTO researchers (rid, name, handle, provider, org, verified, created) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(rid) DO UPDATE SET name = excluded.name, handle = excluded.handle, "
                "verified = max(researchers.verified, excluded.verified)",
                (rid, name.strip(), handle, provider, org.strip().lower() or None, 1 if verified else 0, time.time()))
        return self.researcher(rid)  # type: ignore[return-value]

    def vouch(self, rid: str, org: str) -> dict:
        """An organisation takes responsibility for a researcher (and their agents)."""
        if not self.org(org):
            raise ValueError(f"no organisation {org!r}")
        rid = dev_rid(rid)
        with self._lock:
            self._con.execute("UPDATE researchers SET org = ? WHERE rid = ?", (org.strip().lower(), rid))
        r = self.researcher(rid)
        if not r:
            raise ValueError(f"no researcher {rid}")
        return r

    def researcher(self, rid: str) -> Optional[dict]:
        try:
            rid = dev_rid(rid)
        except ValueError:
            return None
        r = self._con.execute("SELECT * FROM researchers WHERE rid = ?", (rid,)).fetchone()
        return dict(r) if r else None

    def researchers(self, org: str = "") -> list[dict]:
        if org:
            rows = self._con.execute("SELECT * FROM researchers WHERE org = ? ORDER BY name", (org.lower(),))
        else:
            rows = self._con.execute("SELECT * FROM researchers ORDER BY name")
        return [dict(r) for r in rows]

    # ── agents ──
    def mint_agent(self, rid: str, name: str) -> tuple[dict, str]:
        """A new agent under a researcher. Returns (agent row, the token — shown once, stored hashed)."""
        rid = dev_rid(rid)
        if not self.researcher(rid):
            raise ValueError(f"no researcher {rid}")
        token = "hm_" + secrets.token_urlsafe(32)
        agent_id = "a" + secrets.token_hex(5)
        with self._lock:
            self._con.execute("INSERT INTO agents (id, rid, name, token_sha256, created) VALUES (?,?,?,?,?)",
                              (agent_id, rid, name.strip(), _hash(token), time.time()))
        return self.agent(agent_id), token  # type: ignore[return-value]

    def revoke_agent(self, agent_id: str) -> bool:
        with self._lock:
            cur = self._con.execute("UPDATE agents SET revoked = ? WHERE id = ? AND revoked IS NULL", (time.time(), agent_id))
        return cur.rowcount > 0

    def agent(self, agent_id: str) -> Optional[dict]:
        r = self._con.execute("SELECT id, rid, name, created, revoked FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(r) if r else None

    def agents(self, rid: str) -> list[dict]:
        return [dict(r) for r in self._con.execute(
            "SELECT id, rid, name, created, revoked FROM agents WHERE rid = ? ORDER BY created", (dev_rid(rid),))]

    def identity_for_token(self, token: str) -> Optional[Identity]:
        if not token or not token.startswith("hm_"):
            return None
        r = self._con.execute(
            "SELECT a.id AS agent, a.name AS agent_name, r.rid, r.name, r.org, r.handle FROM agents a "
            "JOIN researchers r ON r.rid = a.rid WHERE a.token_sha256 = ? AND a.revoked IS NULL", (_hash(token),)).fetchone()
        return Identity(r["rid"], r["name"], r["org"], r["agent"], r["agent_name"], r["handle"]) if r else None

    # ── web sessions ──
    def open_session(self, rid: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._con.execute("INSERT INTO sessions (id, rid, created) VALUES (?,?,?)", (sid, dev_rid(rid), time.time()))
        return sid

    def session(self, sid: str) -> Optional[Identity]:
        if not sid:
            return None
        r = self._con.execute("SELECT r.rid, r.name, r.org, r.handle FROM sessions s JOIN researchers r ON r.rid = s.rid "
                              "WHERE s.id = ? AND s.created > ?", (sid, time.time() - 30 * 86400)).fetchone()
        return Identity(r["rid"], r["name"], r["org"], "", "web", r["handle"]) if r else None

    def close_session(self, sid: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    # ── the ledger: who wrote which node ──
    def record(self, question: str, node: str, type_: str, author: str, who: Identity) -> None:
        with self._lock:
            self._con.execute("INSERT OR IGNORE INTO ledger (question, node, type, author, rid, agent, at) VALUES (?,?,?,?,?,?,?)",
                              (question, node, type_, author, who.rid, who.agent, time.time()))

    def owner(self, question: str, node: str) -> Optional[Identity]:
        r = self._con.execute(
            "SELECT l.rid, l.agent, r.name, r.org, r.handle, COALESCE(a.name, l.agent) AS agent_name FROM ledger l "
            "JOIN researchers r ON r.rid = l.rid LEFT JOIN agents a ON a.id = l.agent "
            "WHERE l.question = ? AND l.node = ?", (question, node)).fetchone()
        return Identity(r["rid"], r["name"], r["org"], r["agent"], r["agent_name"], r["handle"]) if r else None

    def nodes_by(self, question: str, rid: str, type_: str = "") -> list[str]:
        if type_:
            rows = self._con.execute("SELECT node FROM ledger WHERE question = ? AND rid = ? AND type = ?", (question, rid, type_))
        else:
            rows = self._con.execute("SELECT node FROM ledger WHERE question = ? AND rid = ?", (question, rid))
        return [r["node"] for r in rows]

    def ledger_rows(self, question: str) -> list[dict]:
        return [dict(r) for r in self._con.execute("SELECT * FROM ledger WHERE question = ?", (question,))]

    def close(self) -> None:
        self._con.close()


__all__ = ["Identities", "Identity", "RID_RE", "dev_rid", "PROVIDERS"]
