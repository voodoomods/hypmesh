"""Take a seat at a question: HypGraph's MCP server, pointed at a mesh question instead of a local file.

    hypmesh seat --mesh https://mesh.example.org --question demo-q --as claude --token-file ~/.hypmesh/claude.token

Claude Code, Codex or any MCP client starts this over stdio and gets HypGraph's own tools (overview, add, link,
review, work_queue, claim_work, return_work …). Every call goes to the mesh's HypGraph API for that question,
``/q/<id>/api/graph/*``, with the agent's bearer token, so every write passes the mesh gate: the token names the
researcher who answers for it, reserved names are refused, an identity edits only what it created, and the record's
own rules refuse overclaims as they are written. Nothing is stored here; the record is the mesh's.

``RemoteGraph`` is the piece that makes this work: a ``hypgraph.Graph`` whose methods are the mesh's HTTP routes,
so ``hypgraph.mcp.build`` serves it unchanged. Every request carries ``X-HypMesh-Seat: <author>``, which the mesh
uses to show who is seated at the question right now."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from hypgraph import Graph, GraphError

from .questions import POLICY


class RemoteGraph(Graph):
    """A mesh question's record, read and written over the mesh's HypGraph API with an agent token."""

    def __init__(self, question_url: str, token: str, *, seat: str = "", timeout: float = 30.0):  # noqa: D401
        # Graph.__init__ is not called on purpose: there is no local file, every method below is an HTTP call
        self.base = question_url.rstrip("/") + "/api/graph"
        self.question_url = question_url.rstrip("/")
        self.token = token.strip()
        self.seat = seat
        self.timeout = timeout
        self.policy = POLICY
        self.path = None

    # ── plumbing ──
    def _req(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        url = self.base + path
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if self.seat:
            headers["X-HypMesh-Seat"] = self.seat
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail") or e.reason
            except Exception:
                detail = e.reason
            if e.code == 404:
                raise KeyError(str(detail))
            raise GraphError(f"{detail}" if e.code in (400, 409, 422) else f"the mesh answered {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise GraphError(f"could not reach the mesh at {self.question_url}: {e.reason}")

    def _get(self, path: str, **query: Any) -> Any:
        return self._req("GET", path, query=query)

    def _write(self, op: str, author: str, **kw: Any) -> Any:
        return self._req("POST", "/write", body={"op": op, "author": author, **{k: v for k, v in kw.items() if v is not None}})

    def close(self) -> None:
        pass

    def __repr__(self) -> str:
        return f"RemoteGraph({self.question_url!r}, seat={self.seat!r})"

    # ── reads ──
    def investigations(self) -> list[dict]:
        return self._get("/investigations")["investigations"]

    def get(self, nid: str, history: bool = False) -> dict:
        return self._get(f"/node/{urllib.parse.quote(nid, safe='')}", history="1" if history else "")

    def resolve(self, ref: str, inv: str = "") -> str:
        ref = str(ref or "").strip()
        if not ref:
            raise KeyError("no node ''")
        return self._get("/resolve", ref=ref, inv=inv)["id"]

    def subgraph(self, inv: str = "", types: list | None = None, limit: int = 5000) -> dict:
        return self._get("/subgraph", inv=inv, types=",".join(types or []))

    def why(self, nid: str) -> dict:
        return self._get(f"/why/{urllib.parse.quote(nid, safe='')}")

    def impact(self, nid: str) -> dict:
        return self._get(f"/impact/{urllib.parse.quote(nid, safe='')}")

    def search(self, text: str = "", type: str = "", inv: str = "", limit: int = 50) -> list[dict]:
        return self._get("/search", text=text, type=type, inv=inv, limit=limit)["results"]

    def work_map(self, inv: str = "") -> dict:
        return self._get("/work", inv=inv)

    def next_experiments(self, inv: str, limit: int = 10) -> dict:
        return self._get("/next", inv=inv, limit=limit)

    def report(self, inv: str) -> dict:
        return self._get("/report", inv=inv)

    def completion(self, inv: str) -> dict:
        return self._get("/completion", inv=inv)

    def stages(self, inv: str) -> list[dict]:
        return self._get("/stages", inv=inv)["stages"]

    def stage(self, node: str, inv: str = "") -> dict:
        return self._get(f"/stage/{urllib.parse.quote(node, safe='')}", inv=inv)

    def orders(self, inv: str, status: str = "") -> list[dict]:
        return self._get("/orders", inv=inv, status=status)["orders"]

    def order_text(self, order: str) -> str:
        return self._get(f"/order/{urllib.parse.quote(order, safe='')}")["text"]

    def question_board(self, inv: str) -> list[dict]:
        return self._get("/board", inv=inv)["board"]

    def verify(self, path: str = "") -> dict:
        return self._get("/verify")

    def verify_chain(self) -> dict:
        return self.verify()

    def refusal_report(self, inv: str = "", since: str = "") -> dict:
        return self._get("/refusals", inv=inv, since=since)

    def refusals(self, inv: str = "", author: str = "", since: str = "", limit: int = 500) -> list[dict]:
        rep = self.refusal_report(inv, since)
        rows = rep.get("rows") or rep.get("refusals") or []
        return [r for r in rows if not author or r.get("author") == author][:limit]

    def check_text(self, text: str, inv: str = "") -> dict:
        return self._req("POST", "/check", body={"text": text, "inv": inv})

    def check_work(self, order: str, text: str = "") -> dict:
        return self._req("POST", "/check_work", body={"order": order, "text": text})

    def briefing(self, inv: str, *, roster: list | None = None, since_seq: int = 0, budget_usd: float = 0) -> dict:
        return self._req("POST", "/briefing", body={"inv": inv, "roster": roster, "since_seq": since_seq})

    # ── writes: the mesh's write API, behind its gate ──
    def create(self, *, author: str, type: str, fields: dict, inv: str = "", links: list | None = None,
               reason: str = "", **_: Any) -> dict:
        return self._write("create", author, type=type, fields=fields, inv=inv, links=links or [], reason=reason)

    def revise(self, *, author: str, node: str, fields: dict, reason: str) -> dict:
        return self._write("revise", author, node=node, fields=fields, reason=reason)

    def link(self, *, author: str, src: str, rel: str, dst: str, why: str = "", data: dict | None = None) -> dict:
        return self._write("link", author, src=src, rel=rel, dst=dst, why=why, data=data)

    def retract(self, *, author: str, edge: int, why: str) -> dict:
        return self._write("retract", author, edge=edge, why=why)

    def withdraw(self, *, author: str, node: str, why: str, **_: Any) -> dict:
        return self._write("withdraw", author, node=node, why=why)

    def review(self, *, author: str, target: str, verdict: str, method: str, notes: str, **_: Any) -> dict:
        return self._write("review", author, target=target, verdict=verdict, method=method, notes=notes)

    def lease(self, *, author: str, node: str, task: str = "", minutes: int = 240, release: bool = False,
              note: str = "") -> dict:
        return self._write("lease", author, node=node, task=task, minutes=minutes, release=release, note=note)

    def issue_work(self, *, author: str, **kw: Any) -> dict:
        return self._write("issue_work", author, **kw)

    def return_work(self, *, author: str, order: str, note: str = "") -> dict:
        return self._write("return_work", author, order=order, note=note)

    def accept_work(self, *, author: str, order: str, reason: str = "", text: str = "") -> dict:
        return self._write("accept_work", author, order=order, reason=reason, text=text)

    def rework(self, *, author: str, order: str, missing: str, done_when: list | None = None) -> dict:
        return self._write("rework", author, order=order, missing=missing, done_when=done_when)

    def cancel_work(self, *, author: str, order: str, why: str) -> dict:
        return self._write("cancel_work", author, order=order, why=why)

    def cancel_moot(self, *, author: str, inv: str) -> list[dict]:
        return self._write("cancel_moot", author, inv=inv)

    def settle(self, *, author: str, inv: str) -> list[dict]:
        return self._write("settle", author, inv=inv)


def read_token(token: str = "", token_file: str = "") -> str:
    """The agent token: given, from a file, or $HYPMESH_TOKEN. Never printed."""
    if token:
        return token.strip()
    if token_file:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    env = os.environ.get("HYPMESH_TOKEN", "").strip()
    if env:
        return env
    raise SystemExit("hypmesh seat needs an agent token: --token-file, --token or $HYPMESH_TOKEN (mint one on /me, "
                     "or with `hypmesh identity mint-agent ORCID NAME --to FILE`)")


def build_server(mesh: str, question: str, author: str, token: str, files_root: str | None = None):
    """The MCP server for one seat at one question (hypgraph.mcp.build over a RemoteGraph)."""
    from hypgraph.mcp import build
    g = RemoteGraph(f"{mesh.rstrip('/')}/q/{question}", token, seat=author)
    try:                                              # check the token and the question before an agent relies on it
        g._req("GET", "/investigations")
        who = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{mesh.rstrip('/')}/api/me", headers={"Authorization": f"Bearer {token}"}), timeout=g.timeout).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"the mesh refused the token ({e.code}): mint a new one on /me")
    except (GraphError, KeyError) as e:
        raise SystemExit(f"cannot open question {question!r} on {mesh}: {e}")
    server = build(g, author, files_root=files_root)
    server.instructions = (server.instructions or "") + (
        f"\nYou are seated at HypMesh question {question!r} ({mesh.rstrip('/')}/q/{question}) as {author!r}, an agent of "
        f"{who.get('name')} ({who.get('orcid')}). What you write is public, permanent and attributed to them. "
        "Start with overview(); start your own investigation unless an order names you.")
    return server


def run(mesh: str, question: str, author: str, token: str, transport: str = "stdio", port: int = 8766,
        files_root: str | None = None) -> None:
    server = build_server(mesh, question, author, token, files_root)
    if transport == "http":
        server.run(transport="http", host="127.0.0.1", port=port)
    else:
        server.run(show_banner=False)


__all__ = ["RemoteGraph", "build_server", "run", "read_token"]
