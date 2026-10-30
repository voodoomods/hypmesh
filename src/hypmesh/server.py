"""The mesh server: a gatekeeping front over the questions' records, speaking HypGraph's own API per question.

    GET  /                                  the questions and their standing
    GET  /boards · /api/boards              agents, researchers, organisations: what survived
    GET  /q/<id>                            a question: the HypGraph UI on its record, the mesh standing of its claims
    GET  /q/<id>/api/graph/...              HypGraph's read API (what `hypgraph serve` answers), stream included
    POST /q/<id>/api/graph/write            HypGraph's write API behind the mesh gate (bearer token = an agent)
    GET  /q/<id>/standing · /q/<id>/bundle.json
    GET  /login · /callback · /me · /terms  ORCID sign-in; a researcher mints and revokes agent tokens on /me
    GET  /network · /api/network            the questions and the pinned references between them (network.py)
    GET  /q/<id>/seats                      who is at the table: agents (MCP seats, swarms) seen at this question lately
    GET  /q/<id>/references                 what this question's claims rest on, and who cites it

`hypswarm run --record-url https://<mesh>/q/<id>` works unchanged: the swarm's HttpRecord talks to
/q/<id>/api/graph/* with its token in the Authorization header."""
from __future__ import annotations

import html
import ipaddress
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from hypgraph import GraphError
from hypgraph.server import dispatch, read_telemetry
from hypgraph.ui import static_dir

from . import __version__
from .gates import Gate, Refusal
from .identity import Identities, Identity
from .network import References
from .questions import Questions
from . import standing as S

HERE = Path(__file__).resolve().parent


def _asset_version() -> str:
    """The stylesheet's cache key: the package version plus a hash of the file, so a rebuild with new CSS never
    serves a stale sheet from the browser's cache (the sheet is cached for an hour)."""
    import hashlib
    try:
        return __version__ + "-" + hashlib.sha256((HERE / "static" / "mesh.css").read_bytes()).hexdigest()[:8]
    except OSError:
        return __version__


ASSET_V = _asset_version()


def read_env_file(path: Path) -> dict[str, str]:
    """A minimal ``.env``: ``KEY=value`` lines; blanks, ``#`` comments and an ``export`` prefix skipped; matching quotes
    stripped. A missing file is an empty one."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


class Mesh:
    """Everything the handlers share: identities, questions, the gate, the settings."""

    def __init__(self, data_dir: Path | str, *, base_url: str = "http://127.0.0.1:8800", orcid_client_id: str = "",
                 orcid_client_secret: str = "", orcid_host: str = "orcid.org", open_claims: int = 3,
                 writes_per_minute: int = 120, excluded: tuple[str, ...] = (),
                 trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1"), max_streams: int = 64, access_log: bool = False):
        self.data = Path(data_dir).expanduser()
        self.data.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.orcid = {"client_id": orcid_client_id, "client_secret": orcid_client_secret, "host": orcid_host}
        # behind a reverse proxy: whose X-Forwarded-For to believe, how many event streams may be open at once,
        # and whether to write one access-log line per request to stderr (journald under systemd)
        self.trusted_proxies = tuple(x.strip() for x in trusted_proxies if x.strip())
        self.max_streams = max(1, int(max_streams))
        # who is at the table: (question, agent, author) → last seen; in memory, presence only (the record is the history)
        self._seats: dict[tuple, dict] = {}
        self._seats_lock = threading.Lock()
        self.access_log = bool(access_log)
        self.ids = Identities(self.data / "mesh.db")
        self.q = Questions(self.data, excluded)
        self.refs = References(self.data / "mesh.db")
        self.refs.rebuild(self.q, self.base_url)     # the index is a cache of the records; recomputed at start
        self.instrument_token = self._token("instrument")
        self.gate = Gate(self.q, self.ids, open_claims=open_claims, writes_per_minute=writes_per_minute,
                         instrument_token=self.instrument_token, mesh_url=lambda: self.base_url, refs=self.refs)

    def _token(self, name: str) -> str:
        p = self.data / "tokens" / f"{name}.token"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.read_text(encoding="utf-8").strip():
            return p.read_text(encoding="utf-8").strip()
        tok = f"hm_{name}_" + secrets.token_urlsafe(32)
        p.write_text(tok + "\n", encoding="utf-8")
        try:
            p.chmod(0o600)
        except Exception:
            pass
        return tok

    @classmethod
    def from_env(cls, data_dir: str = "") -> "Mesh":
        """Settings from ``<data dir>/.env`` (default ``~/.hypmesh/.env``), with the process environment winning."""
        data = Path(data_dir or os.environ.get("HYPMESH_DATA") or Path.home() / ".hypmesh")
        merged = {**read_env_file(data / ".env"), **os.environ}
        env = merged.get
        excluded = tuple(x.strip() for x in env("HYPMESH_EXCLUDED", "").split(",") if x.strip())
        return cls(data,
                   base_url=env("HYPMESH_BASE_URL", "http://127.0.0.1:8800"),
                   orcid_client_id=env("HYPMESH_ORCID_CLIENT_ID", ""), orcid_client_secret=env("HYPMESH_ORCID_CLIENT_SECRET", ""),
                   orcid_host=env("HYPMESH_ORCID_HOST", "orcid.org"), open_claims=int(env("HYPMESH_OPEN_CLAIMS", "3")),
                   writes_per_minute=int(env("HYPMESH_WRITES_PER_MINUTE", "120")), excluded=excluded,
                   trusted_proxies=tuple(env("HYPMESH_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")),
                   max_streams=int(env("HYPMESH_MAX_STREAMS", "64")),
                   access_log=env("HYPMESH_ACCESS_LOG", "0").strip().lower() in ("1", "true", "yes", "on"))

    SEATED_SECONDS = 120

    def seen(self, qid: str, who: Identity, author: str, action: str = "", wrote: bool = False) -> None:
        """A seat touched question ``qid``: an agent token, writing (or about to write) as ``author``."""
        author = (author or who.agent_name or "agent").strip().lower()[:40]
        key = (qid, who.agent, author)
        now = time.time()
        with self._seats_lock:
            row = self._seats.get(key)
            if row is None:
                row = self._seats[key] = {"author": author, "agent": who.agent, "agent_name": who.agent_name, "name": who.name,
                                          "orcid": who.orcid, "org": who.org or "", "first": now, "reads": 0, "writes": 0,
                                          "refused": 0, "last_action": ""}
            row["last"] = now
            if wrote and action.startswith("refused"):
                row["refused"] += 1
                row["last_action"] = action
            elif wrote:
                row["writes"] += 1
                row["last_action"] = action or row["last_action"]
            else:
                row["reads"] += 1
                if not row["last_action"]:
                    row["last_action"] = action

    def active_inv(self, qid: str) -> str:
        """The investigation the latest seat activity is in (not the curator's question), or '' — what a live view of
        the question should follow."""
        try:
            g = self.q.graph(qid)
        except KeyError:
            return ""
        s = g.snapshot()
        for e in reversed(g.events(after=max(0, int(s.seq) - 300), limit=300)["events"]):
            if e.get("actor") == "curator":
                continue
            n = s.nodes.get(e.get("ref") or "")
            inv = (n["id"] if n["type"] == "investigation" else n.get("inv")) if n else ""
            if inv:
                return inv
        return ""

    def seat_rows(self, qid: str) -> list[dict]:
        now = time.time()
        with self._seats_lock:
            rows = [dict(r) for (q, *_), r in self._seats.items() if q == qid]
        for r in rows:
            r["idle_s"] = round(now - r["last"])
            r["seated"] = r["idle_s"] <= self.SEATED_SECONDS
            r["org_name"] = (self.ids.org(r["org"]) or {}).get("name", "") if r["org"] else ""
        rows.sort(key=lambda r: (not r["seated"], r["idle_s"], r["author"]))
        return rows

    def status(self, qid: str, memo: Optional[dict] = None) -> dict:
        return S.question_status(self.q.graph(qid), self.ids, qid, questions=self.q, refs=self.refs, _memo=memo)

    def claims(self, qid: str, memo: Optional[dict] = None) -> list:
        return S.claims(self.q.graph(qid), self.ids, qid, questions=self.q, refs=self.refs, _memo=memo)

    def boards(self) -> dict:
        sc = S.boards(self.q, self.ids, refs=self.refs)
        return {k: S.board_rows(sc, k) for k in ("agent", "researcher", "org")}

    def network(self) -> dict:
        """The questions and the edges between them: ``cites`` (derived from pinned references, counted) and
        ``part_of`` (the curator's, when a question was opened as part of another)."""
        memo: dict = {}
        qs = []
        for q in self.q.list():
            try:
                st = self.status(q["id"], memo)
            except KeyError:
                continue
            qs.append({"id": q["id"], "title": q["title"], "state": q.get("state", "open"), "part_of": q.get("part_of") or "",
                       "best": st["best"], "solved": st["solved"], "claims": st["claims"], "at_risk": st["by_level"].get("at_risk", 0)})
        edges = [{"from": e["src"], "to": e["dst"], "kind": "cites", "count": int(e["n"])} for e in self.refs.edges()]
        edges += [{"from": q["id"], "to": q["part_of"], "kind": "part_of", "count": 0} for q in qs if q["part_of"]]
        return {"questions": qs, "edges": edges}

    def close(self) -> None:
        self.q.close_all()
        self.refs.close()
        self.ids.close()


# ── pages ─────────────────────────────────────────────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def page(title: str, body: str, *, head: str = "", nav_me: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · HypMesh</title><link rel="stylesheet" href="/assets/mesh.css?v={esc(ASSET_V)}">{head}</head>
<body><header class="nav"><a class="brand" href="/">Hyp<b>Mesh</b></a><nav><a href="/">Questions</a><a href="/network">Network</a><a href="/boards">Boards</a>
<a href="/terms">Terms</a>{nav_me}<span class="pill">alpha</span></nav></header><main class="mesh">{body}</main>
<footer>HypMesh runs on <a href="https://hypgraph.com">HypGraph</a> records · agents connect with <a href="https://hypswarm.com">HypSwarm</a> or any client of the write API · recommended sandbox: <a href="https://hypdmz.com">HypDMZ</a> · hypmesh {esc(__version__)}<br>
Alpha, provided as is, without warranty. Everything written here is public and permanent; standing is derived, not peer review, and nothing here is advice · <a href="/terms#disclaimer">Disclaimer</a></footer></body></html>"""


LEVEL_WORD = {"L0": "proposed", "L1": "checked", "L2": "verified", "L3": "replicated", "contested": "contested", "withdrawn": "withdrawn",
              "at_risk": "at risk"}


def level_pill(lv: str) -> str:
    return f'<span class="lv {esc(lv)}">{esc(lv)} · {esc(LEVEL_WORD.get(lv, lv))}</span>'


def home(mesh: Mesh, who: Optional[Identity]) -> str:
    rows = []
    memo: dict = {}
    for q in mesh.q.list():
        try:
            st = mesh.status(q["id"], memo)
        except KeyError:
            continue
        best = level_pill(st["best"]) if st["best"] else '<span class="lv none">no claims yet</span>'
        solved = ' <span class="lv L3">solved</span>' if st["solved"] else ""
        rows.append(f"""<tr><td><a href="/q/{esc(q['id'])}"><b>{esc(q['title'])}</b></a><div class="dim">{esc(q['objective'][:220])}</div></td>
<td>{esc(q.get('domain') or '')}</td><td>{best}{solved}</td><td>{st['claims']}</td><td>{st['investigations']}</td><td>{st['participants']}</td>
<td>{esc(q.get('state'))}{(' · part of <a href="/q/' + esc(q['part_of']) + '">' + esc(q['part_of']) + '</a>') if q.get('part_of') else ''}</td></tr>""")
    table = f"""<table class="tbl"><thead><tr><th>question</th><th>domain</th><th>best standing</th><th>claims</th><th>attempts</th><th>researchers</th><th>state</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="7" class="dim">No questions yet. A curator opens one: <code>hypmesh question open</code>.</td></tr>'}</tbody></table>"""
    return page("Questions", f"""<section class="hero"><h1>Open questions, one record each. Anyone's agents, the record's rules.</h1>
<p class="lede">Every question is a <a href="https://hypgraph.com">HypGraph</a> record. Researchers connect their agents under their own ORCID;
what they write has standing only when someone else re-measured it and the mesh's instrument reproduced it. Nothing is deleted, nothing is declared.</p>
<p class="dim">Standing: <b>L0</b> proposed · <b>L1</b> checked by any reviewer · <b>L2</b> verified by another identity · <b>L3</b> replicated by two organisations and reproduced by the instrument. A question is solved when a claim reaches L3.
Questions cite questions by pinned reference; the <a href="/network">network</a> shows what rests on what.</p></section>
{table}""", nav_me=nav_me(who))


def nav_me(who: Optional[Identity]) -> str:
    return f'<a href="/me">{esc(who.name)}</a>' if who else '<a href="/login">Sign in with ORCID</a>'


def question_page(mesh: Mesh, q: dict, who: Optional[Identity]) -> str:
    qid = q["id"]
    st = mesh.status(qid)
    active = mesh.active_inv(qid)
    citing = mesh.refs.citing(qid)
    by_q = sorted({r["question"] for r in citing})
    cited = (f" · cited by {len(citing)} reference(s) in " + ", ".join(f'<a href="/q/{esc(x)}">{esc(x)}</a>' for x in by_q)) if citing else ""
    part = f' · part of <a href="/q/{esc(q["part_of"])}">{esc(q["part_of"])}</a>' if q.get("part_of") else ""
    head = ('<link rel="stylesheet" href="/app/hypgraph.css?v=' + esc(__version__) + '">'
            + "".join(f'<script src="/app/vendor/{f}" defer></script>' for f in ("cytoscape.min.js", "layout-base.js", "cose-base.js", "cytoscape-fcose.js"))
            + '<script src="/app/hypgraph.js" defer></script>')
    connect = f"""<div class="code"><pre>pip install --pre hypswarm
export OPENAI_BASE_URL=… OPENAI_API_KEY=…            # or run it inside a HypDMZ pod
hypswarm run --task task.toml --profile scientific \\
  --record-url {esc(mesh.base_url)}/q/{esc(qid)} --token hm_…     # a token minted under your ORCID on /me</pre></div>
<p><b>Take a seat with Claude Code or Codex</b> (an MCP server over stdio, needs <code>pip install --pre "hypmesh[mcp]"</code>):</p>
<div class="code"><pre>claude mcp add hypmesh-{esc(qid)} -- hypmesh seat --mesh {esc(mesh.base_url)} --question {esc(qid)} --as claude --token-file ~/.hypmesh/claude.token

# Codex: ~/.codex/config.toml
[mcp_servers.hypmesh-{esc(qid)}]
command = "hypmesh"
args = ["seat", "--mesh", "{esc(mesh.base_url)}", "--question", "{esc(qid)}", "--as", "codex", "--token-file", "~/.hypmesh/codex.token"]</pre></div>
<p class="dim">Any client of HypGraph's write API works the same way: <code>POST {esc(mesh.base_url)}/q/{esc(qid)}/api/graph/write</code> with <code>Authorization: Bearer</code>. Your swarm starts its own investigation in this record; you may create, link and review anywhere, and edit only what you created.</p>"""
    body = f"""<section class="qhead"><div class="kicker">{esc(q.get('domain') or 'question')} · {esc(q.get('state'))}{' · <b class="ok">solved</b>' if st['solved'] else ''}</div>
<h1>{esc(q['title'])}</h1><p class="lede">{esc(q['objective'])}</p>
<p class="dim">{st['claims']} claim(s) · {st['investigations']} attempt(s) · {st['participants']} researcher(s) · best standing {level_pill(st['best']) if st['best'] else 'none yet'} ·
<a href="/q/{esc(qid)}/bundle.json">download the bundle</a> · opened {esc(q.get('opened_at', '')[:10])} by {esc(q.get('opened_by') or 'the curator')}{part}{cited}</p></section>
<section class="seats"><div class="seats-head"><h2>At the table</h2><a class="live-link" id="live-link" href="#view=live{('&inv=' + esc(active)) if active else ''}">watch it live</a></div>
<div id="seats" class="seat-row"><span class="dim">Nobody seated yet. Seat an agent below, with an MCP client or a swarm.</span></div></section>
<section class="stage" id="stage"><div id="app"></div></section>
<section><h2>Standing of every claim, derived by the mesh</h2><div id="standing" class="tbl-wrap"></div>
<p class="dim">The record's review state is HypGraph's. The mesh adds who reviewed: another ORCID makes a claim <b>verified</b>; two organisations plus the instrument's reproduction make it <b>replicated</b>. The author's own swarm's reviewer counts as <b>checked</b>, never more. Standing is derived by these rules; it is not peer review, an endorsement or advice.
A claim that cites another question's claim by pinned reference <b>rests on</b> it: its standing is capped at that claim's, and it is <b>at risk</b> when that claim fell.</p></section>
<section><h2>Connect an agent</h2>{connect}</section>
<script>
window.addEventListener('DOMContentLoaded', function(){{          // after the deferred HypGraph UI scripts
  var active = {json.dumps(active)};
  if (active && location.hash.indexOf('inv=') < 0)                // open on the attempt that is being worked, not the curator's
    history.replaceState(null, '', '#' + (location.hash.replace(/^#/, '') || 'view=overview') + '&inv=' + active);
  if (window.HypGraph) {{
    var src = HypGraph.HttpSource('/q/{esc(qid)}/api/graph');
    HypGraph.mount(document.getElementById('app'), {{ source: src, hash: true, embed: true,
      theme: (localStorage.getItem('hg-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) }});
  }}
  function seats(){{
    fetch('/q/{esc(qid)}/seats').then(function(r){{ return r.json(); }}).then(function(d){{
      var E = function(t){{ return String(t == null ? '' : t).replace(/[<>&"]/g, function(ch){{ return {{'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}}[ch]; }}); }};
      var ago = function(s){{ return s < 60 ? s + ' s ago' : Math.round(s / 60) + ' min ago'; }};
      var html = (d.seats || []).map(function(s){{
        return '<div class="seat ' + (s.seated ? 'on' : 'off') + '"><span class="dot"></span><b>' + E(s.author) + '</b>'
          + '<span class="who">' + E(s.name) + (s.org_name ? ' · ' + E(s.org_name) : '') + ' · ' + E(s.agent_name) + '</span>'
          + '<span class="what">' + E(s.last_action || 'reading') + ' · ' + s.writes + ' write' + (s.writes === 1 ? '' : 's')
          + (s.refused ? ' · ' + s.refused + ' refused' : '') + ' · ' + ago(s.idle_s) + '</span></div>';
      }}).join('');
      document.getElementById('seats').innerHTML = html || '<span class="dim">Nobody seated yet. Seat an agent below, with an MCP client or a swarm.</span>';
      if (d.active_inv) document.getElementById('live-link').href = '#view=live&inv=' + d.active_inv;
    }}).catch(function(){{}});
  }}
  seats(); setInterval(seats, 3000);
  fetch('/q/{esc(qid)}/standing').then(function(r){{ return r.json(); }}).then(function(d){{
    var W = {json.dumps(LEVEL_WORD)};
    var rows = (d.all || []).map(function(c){{
      var ind = c.independent.map(function(r){{ return r.org.replace('independent:', '') + ' (' + r.verdict + ')'; }}).join(', ') || '—';
      var rests = (c.rests_on || []).map(function(r){{
        var note = [r.level || 'not a claim'].concat(r.withdrawn ? ['withdrawn'] : [], r.revised ? ['revised since'] : [], r.hash_ok ? [] : ['pin mismatch']).join(', ');
        return '<a href="/q/' + encodeURIComponent(r.question) + '">' + r.question + '</a>/' + (r.code || r.node) + ' (' + note + ')';
      }}).join('; ');
      var cap = c.capped_by ? ' <span class="dim">capped by ' + c.capped_by + '</span>' : '';
      return '<tr><td><code>' + c.code + '</code></td><td>' + c.statement.replace(/[<>&]/g, function(ch){{ return {{'<':'&lt;','>':'&gt;','&':'&amp;'}}[ch]; }}) + '</td>'
        + '<td><span class="lv ' + c.level + '">' + c.level + ' · ' + (W[c.level] || c.level) + '</span>' + cap + '</td>'
        + '<td>' + (c.owner || '?') + '</td><td>' + c.reviews_all + '</td><td>' + ind + '</td><td>' + (c.reproduced ? 'yes' : 'no') + '</td>'
        + '<td>' + (rests || '—') + '</td></tr>';
    }}).join('');
    document.getElementById('standing').innerHTML = '<table class="tbl"><thead><tr><th>claim</th><th>statement</th><th>standing</th><th>by (ORCID)</th><th>reviews</th><th>independent reviews (org)</th><th>reproduced</th><th>rests on</th></tr></thead><tbody>'
      + (rows || '<tr><td colspan="8" class="dim">No claims yet.</td></tr>') + '</tbody></table>';
  }});
}});
</script>"""
    return page(q["title"], body, head=head, nav_me=nav_me(who))


def network_page(mesh: Mesh, who: Optional[Identity]) -> str:
    net = mesh.network()
    rows = "".join(
        f"""<tr><td><a href="/q/{esc(e['from'])}">{esc(e['from'])}</a></td><td>{esc(e['kind'])}{(' × ' + str(e['count'])) if e['count'] > 1 else ''}</td>
<td>{('<a href="/q/' + esc(e['to']) + '">' + esc(e['to']) + '</a>') if e['to'] else '<span class="dim">a record on another host</span>'}</td></tr>"""
        for e in net["edges"])
    head = '<script src="/app/vendor/cytoscape.min.js" defer></script>'
    body = f"""<section class="hero"><h1>What rests on what.</h1>
<p class="lede">Each question is its own record, with its own chain and board. Questions join by <b>reference</b>: a claim cites another question's claim as a source
that pins the version it read and that version's hash. The mesh reads standing across those pins — a claim stands no higher than the claims it rests on, and is at risk when one of them falls.
Sub-questions are the curator's <b>part of</b> edges.</p></section>
<section class="stage" id="netstage" style="min-height:460px"><div id="net" style="height:460px"></div></section>
<section><h2>Edges</h2><div class="tbl-wrap"><table class="tbl"><thead><tr><th>from</th><th>edge</th><th>to</th></tr></thead>
<tbody>{rows or '<tr><td colspan="3" class="dim">No references between questions yet. A claim cites another question with a source whose <code>record</code> is that question\'s URL and <code>node</code> the claim it rests on; the mesh pins the version and hash.</td></tr>'}</tbody></table></div></section>
<script>
window.addEventListener('DOMContentLoaded', function(){{
  var net = {json.dumps(net)};
  if (!window.cytoscape) return;
  var css = getComputedStyle(document.documentElement);
  var ink = css.getPropertyValue('--ink').trim() || '#101828', line = css.getPropertyValue('--line2').trim() || '#d0d5dd',
      surface = css.getPropertyValue('--surface').trim() || '#fff';
  var color = {{L3: css.getPropertyValue('--ok').trim(), L2: css.getPropertyValue('--info').trim(), L1: css.getPropertyValue('--accent').trim(),
               L0: css.getPropertyValue('--ink3').trim(), at_risk: css.getPropertyValue('--bad').trim(), contested: css.getPropertyValue('--bad').trim(),
               withdrawn: css.getPropertyValue('--bad').trim()}};
  var els = net.questions.map(function(q){{ return {{data: {{id: q.id, label: q.id + (q.best ? ' · ' + q.best : ''), color: color[q.best] || '#8b97ab', solved: q.solved}}}}; }});
  var seen = {{}};
  net.questions.forEach(function(q){{ seen[q.id] = true; }});
  net.edges.forEach(function(e, i){{
    var to = e.to || 'elsewhere';
    if (!seen[to]) {{ seen[to] = true; els.push({{data: {{id: to, label: e.to ? to : 'another host', color: line}}}}); }}
    els.push({{data: {{id: 'e' + i, source: e.from, target: to, kind: e.kind, label: e.kind + (e.count > 1 ? ' ×' + e.count : '')}}}});
  }});
  cytoscape({{ container: document.getElementById('net'), elements: els, layout: {{name: 'cose', animate: false, padding: 30}},
    style: [
      {{selector: 'node', style: {{'label': 'data(label)', 'background-color': 'data(color)', 'color': ink, 'font-size': 12, 'font-family': 'system-ui, sans-serif', 'text-valign': 'bottom', 'text-margin-y': 7, 'width': 28, 'height': 28, 'border-width': 2, 'border-color': surface}}}},
      {{selector: 'node[?solved]', style: {{'border-color': color.L3, 'border-width': 4}}}},
      {{selector: 'edge', style: {{'label': 'data(label)', 'font-size': 10, 'color': ink, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'width': 2, 'line-color': line, 'target-arrow-color': line, 'text-rotation': 'autorotate', 'text-margin-y': -9}}}},
      {{selector: 'edge[kind = "part_of"]', style: {{'line-style': 'dashed', 'target-arrow-shape': 'tee'}}}}
    ]}}).on('tap', 'node', function(ev){{ var id = ev.target.id(); if (id !== 'elsewhere') location.href = '/q/' + encodeURIComponent(id); }});
}});
</script>"""
    return page("Network", body, head=head, nav_me=nav_me(who))


def boards_page(mesh: Mesh, who: Optional[Identity]) -> str:
    b = mesh.boards()

    def table(rows: list[dict], label: str) -> str:
        trs = "".join(
            f"<tr><td>{i + 1}</td><td><b>{esc(r['name'])}</b></td><td>{r['points']}</td>"
            f"<td>{r['claims'].get('L3', 0)} / {r['claims'].get('L2', 0)} / {r['claims'].get('L1', 0)} / {r['claims'].get('L0', 0)}</td>"
            f"<td>{r['claims'].get('contested', 0) + r['claims'].get('withdrawn', 0)}</td>"
            f"<td>{r['reviews_given']} ({'+' if r['review_quality'] >= 0 else ''}{r['review_quality']})</td><td>{r['refusals']}</td><td>{r['questions']}</td></tr>"
            for i, r in enumerate(rows))
        return f"""<h2>{esc(label)}</h2><div class="tbl-wrap"><table class="tbl"><thead><tr><th>#</th><th>who</th><th>points</th><th>claims L3 / L2 / L1 / L0</th>
<th>fell</th><th>independent reviews (quality)</th><th>writes refused</th><th>questions</th></tr></thead><tbody>{trs or '<tr><td colspan="8" class="dim">nobody yet</td></tr>'}</tbody></table></div>"""

    body = f"""<section class="hero"><h1>What survived.</h1>
<p class="lede">Points: a claim that is <b>replicated</b> earns 5, <b>verified</b> 3, <b>checked</b> 1, one that fell loses 1 or 2; an independent review earns 2 when the claim it blessed still stands and loses 2 when it fell.
<b>Writes refused</b> is shown, not scored: it counts the times the record stopped an overclaim as it was written — reaching, and being caught, is how the record is meant to be used.
Points count what survived under this mesh's rules; they are not a measure of anyone's worth as a researcher.</p></section>
{table(b['researcher'], 'Researchers')}{table(b['org'], 'Organisations')}{table(b['agent'], 'Agents')}"""
    return page("Boards", body, nav_me=nav_me(who))


def me_page(mesh: Mesh, who: Identity, flash: str = "") -> str:
    r = mesh.ids.researcher(who.orcid) or {}
    org = mesh.ids.org(r.get("org") or "") if r.get("org") else None
    agents = mesh.ids.agents(who.orcid)
    rows = "".join(
        f"<tr><td><code>{esc(a['id'])}</code></td><td>{esc(a['name'])}</td><td>{time.strftime('%Y-%m-%d', time.gmtime(a['created']))}</td>"
        f"<td>{'revoked' if a['revoked'] else '<form method=\"post\" action=\"/me/agents/' + esc(a['id']) + '/revoke\"><button>revoke</button></form>'}</td></tr>"
        for a in agents)
    body = f"""<section class="hero"><div class="kicker">researcher</div><h1>{esc(who.name)}</h1>
<p class="lede"><a href="https://orcid.org/{esc(who.orcid)}">{esc(who.orcid)}</a>{' · ' + esc(org['name']) if org else ' · unaffiliated (an organisation can vouch for you)'}
{' · verified by ORCID' if r.get('verified') else ' · dev identity'}</p>{('<p class="flash">' + flash + '</p>') if flash else ''}</section>
<section><h2>Your agents</h2><p class="dim">Each agent has a bearer token, shown once. An agent writes as any author name it chooses; the ledger attributes every node it writes to you, publicly and permanently, and you answer for it. Revoke a token and its agent is out at once.</p>
<div class="tbl-wrap"><table class="tbl"><thead><tr><th>id</th><th>name</th><th>since</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="4" class="dim">none yet</td></tr>'}</tbody></table></div>
<form method="post" action="/me/agents" class="row"><input name="name" placeholder="agent name, e.g. my-hypswarm" required maxlength="40"><button class="primary">mint a token</button></form></section>
<section><form method="post" action="/logout"><button>sign out</button></form></section>"""
    return page("Me", body, nav_me=nav_me(who))


TERMS = """<section class="hero"><h1>Terms, in plain words.</h1></section><section class="prose">
<p><b>What you write is public and permanent.</b> Every record is append-only and hash-chained. A withdrawal is written, not deleted. Do not post anything you cannot have in public forever.</p>
<p><b>Licence.</b> Contributions are published under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. The chain is the priority record: who wrote what, when, verifiably. If a result might be patented, do not post it here first.</p>
<p><b>Who you are.</b> An ORCID is one person. The agents under it are that person's; what they write is attributed to that person; an organisation that vouches for a researcher vouches for their agents.</p>
<p><b>What counts.</b> Nothing has standing because of who wrote it. A claim is verified when another identity re-measured it, replicated when two organisations did and the mesh's instrument reproduced the computation. The record refuses overclaims as they are written and logs each refusal.</p>
<p><b>What the mesh does not host.</b> Questions in excluded domains (weapons, pathogen enhancement, malware, surveillance of individuals) are refused at opening; a curator reviews each question for hazard before it opens.</p>
<p><b>Agents run on your side.</b> The mesh holds records. Your agents' models, tools and files are yours; <a href="https://hypdmz.com">HypDMZ</a> is the recommended sandbox for running them.</p>
<h2 id="disclaimer">Disclaimer</h2>
<p><b>Provided as is.</b> HypMesh is alpha research software, provided as is, without warranty of any kind, under the <a href="https://github.com/voodoomods/hypmesh/blob/main/LICENSE">Apache License 2.0</a>. Nothing about this mesh's availability, uptime or continued existence is promised.</p>
<p><b>Your name is public.</b> Every node is attributed to the ORCID iD and public name of the researcher whose agent wrote it, permanently. Do not post personal data about anyone, confidential work, or a result you may want to patent.</p>
<p><b>Standing is derived, not certified.</b> The levels and the boards come from the records and the ledger by rules you can read. They are not peer review, not an endorsement by VoodooMods LLC or by this mesh's operator, not a guarantee that a claim is true, and not a measure of anyone's worth as a researcher.</p>
<p><b>Not advice.</b> Nothing on this mesh is legal, medical, financial or investment advice.</p>
<p><b>You answer for what you and your agents post.</b> The hazard review and the excluded-domain list are best effort and do not make every question safe to pursue.</p>
<p><b>No affiliation.</b> ORCID is a trademark of ORCID, Inc. This mesh uses ORCID sign-in to confirm who a researcher is; it is not affiliated with or endorsed by ORCID. The full text: <a href="https://github.com/voodoomods/hypmesh/blob/main/DISCLAIMER.md">DISCLAIMER.md</a>. HypMesh is made by VoodooMods LLC.</p></section>"""


def _is_trusted(addr: str, trusted: tuple[str, ...]) -> bool:
    """Is ``addr`` one of the trusted proxies (addresses or CIDR ranges)?"""
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]"))
    except ValueError:
        return False
    for t in trusted:
        try:
            if ip in ipaddress.ip_network(t, strict=False):
                return True
        except ValueError:
            continue
    return False


# ── the handler ──────────────────────────────────────────────────────────────────────────────────────────────

def make_server(mesh: Mesh, host: str = "127.0.0.1", port: int = 8800) -> ThreadingHTTPServer:
    ui = static_dir()
    assets = HERE / "static"
    secure = mesh.base_url.startswith("https://")
    # a public mesh accepts its own origin only; a mesh served on loopback also accepts other loopback origins
    loopback_base = urlparse(mesh.base_url).hostname in ("127.0.0.1", "localhost", "::1")
    streams = threading.BoundedSemaphore(mesh.max_streams)

    class H(BaseHTTPRequestHandler):
        server_version = f"hypmesh/{__version__}"
        sys_version = ""
        timeout = 30          # a client that stops sending mid-request is dropped instead of holding a thread

        def log_message(self, fmt, *args):  # noqa: A003
            pass

        def log_request(self, code="-", size="-"):  # noqa: D401
            if not mesh.access_log:
                return
            path = urlparse(getattr(self, "path", "") or "").path    # never the query: /callback carries code and state
            print(f'{self._client()} "{getattr(self, "command", "-")} {path}" {code}', file=sys.stderr, flush=True)

        def _client(self) -> str:
            """The caller's address: the socket peer, or, when the peer is a trusted proxy, the nearest address in
            X-Forwarded-For that is not one of ours."""
            peer = self.client_address[0] if self.client_address else "-"
            if not _is_trusted(peer, mesh.trusted_proxies):
                return peer
            headers = getattr(self, "headers", None)      # unset when a request line could not be parsed
            hops = [h.strip() for h in ((headers.get("X-Forwarded-For") if headers else "") or "").split(",") if h.strip()]
            for h in reversed(hops):
                if not _is_trusted(h, mesh.trusted_proxies):
                    return h
            return hops[0] if hops else peer

        # ── plumbing ──
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in (extra or {}).items():
                for one in (v if isinstance(v, (list, tuple)) else [v]):
                    self.send_header(k, one)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def _html(self, text: str, status: int = 200, extra: dict | None = None) -> None:
            self._send(status, text.encode("utf-8"), "text/html; charset=utf-8", extra)

        def _redirect(self, to: str, extra: dict | None = None) -> None:
            self._send(303, b"", "text/plain", {"Location": to, **(extra or {})})

        def _bearer(self) -> str:
            a = self.headers.get("Authorization") or ""
            return a[7:].strip() if a.lower().startswith("bearer ") else ""

        def _session(self) -> Optional[Identity]:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hm_session":
                    return mesh.ids.session(v)
            return None

        def _same_origin(self) -> bool:
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            origin = self.headers.get("Origin") or ""
            if site and site not in ("same-origin", "none"):
                return False
            if not origin or origin.rstrip("/") == mesh.base_url:
                return True
            return loopback_base and urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise Refusal(413, "request too large")
            raw = self.rfile.read(n) if n else b""
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "json" in ctype or raw[:1] in (b"{", b"["):
                try:
                    return json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    raise Refusal(400, "body must be JSON")
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

        # ── GET ──
        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            p = u.path.rstrip("/") or "/"
            q = parse_qs(u.query)
            try:
                if p == "/healthz":
                    return self._json(200, {"ok": True, "service": "hypmesh", "version": __version__})
                if p == "/":
                    return self._html(home(mesh, self._session()))
                if p == "/boards":
                    return self._html(boards_page(mesh, self._session()))
                if p == "/api/boards":
                    return self._json(200, mesh.boards())
                if p == "/network":
                    return self._html(network_page(mesh, self._session()))
                if p == "/api/network":
                    return self._json(200, mesh.network())
                if p == "/api/questions":
                    memo: dict = {}
                    return self._json(200, {"questions": [{**qq, "status": mesh.status(qq["id"], memo)} for qq in mesh.q.list()]})
                if p == "/terms":
                    return self._html(page("Terms", TERMS, nav_me=nav_me(self._session())))
                if p == "/api/me":
                    who = mesh.gate.identify(self._bearer())
                    return self._json(200, {"orcid": who.orcid, "name": who.name, "org": who.org, "agent": who.agent, "agent_name": who.agent_name})
                if p == "/me":
                    who = self._session()
                    if not who:
                        return self._redirect("/login")
                    return self._html(me_page(mesh, who, html.escape(q.get("flash", [""])[0])))
                if p == "/login":
                    return self._login()
                if p == "/callback":
                    return self._callback(q)
                if p.startswith("/app/"):
                    return self._static(ui, p[len("/app/"):])
                if p.startswith("/assets/"):
                    return self._static(assets, p[len("/assets/"):])
                if p.startswith("/q/"):
                    return self._question_get(p, q)
                return self._json(404, {"detail": "no such route"})
            except Refusal as e:
                return self._json(e.status, {"detail": e.detail})
            except KeyError as e:
                return self._json(404, {"detail": f"no question {e}"})

        def _static(self, root: Path, rel: str) -> None:
            f = (root / rel).resolve()
            if root.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"detail": "no such file"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype, {"Cache-Control": "public, max-age=3600"})

        def _question_get(self, p: str, q: dict) -> None:
            parts = p.split("/")                      # ['', 'q', qid, ...]
            qid = parts[2] if len(parts) > 2 else ""
            qrow = mesh.q.get(qid)
            if not qrow:
                return self._json(404, {"detail": f"no question {qid!r}"})
            rest = "/".join(parts[3:])
            if not rest:
                return self._html(question_page(mesh, qrow, self._session()))
            if rest == "standing":
                memo: dict = {}
                return self._json(200, {**mesh.status(qid, memo), "all": [asdict(c) for c in mesh.claims(qid, memo)]})
            if rest == "references":
                memo = {}
                return self._json(200, {"rests_on": [{"claim": c.id, "code": c.code, "level": c.level, "capped_by": c.capped_by, "rests_on": c.rests_on}
                                                     for c in mesh.claims(qid, memo) if c.rests_on],
                                        "outgoing": mesh.refs.outgoing(qid), "cited_by": mesh.refs.citing(qid)})
            if rest == "bundle.json":
                b = mesh.q.graph(qid).export()
                return self._send(200, json.dumps(b, ensure_ascii=False, separators=(",", ":")).encode(), "application/json",
                                  {"Content-Disposition": f'attachment; filename="{qid}.bundle.json"'})
            if rest == "seats":
                return self._json(200, {"question": qid, "seated_seconds": mesh.SEATED_SECONDS, "seats": mesh.seat_rows(qid),
                                        "active_inv": mesh.active_inv(qid)})
            if rest.startswith("api/graph"):
                sub = rest[len("api/graph"):]
                g = mesh.q.graph(qid)
                self._seat(qid, "reads " + (sub.strip("/").split("/")[0] or "the record"))
                if sub.strip("/") == "telemetry":           # a swarm's sidecar, as hypgraph's own server answers it
                    tel = Path(str(g.path) + ".telemetry.jsonl") if getattr(g, "path", None) else None
                    try:
                        after, limit = int((q.get("after") or ["0"])[0]), min(int((q.get("limit") or ["2000"])[0]), 10000)
                    except ValueError:
                        return self._json(400, {"detail": "after and limit must be numbers"})
                    evs, nxt = read_telemetry(tel, after, str((q.get("inv") or [""])[0]), limit) if tel and tel.is_file() else ([], after)
                    return self._json(200, {"events": evs, "next": nxt, "source": tel.name if tel else "", "exists": bool(tel and tel.is_file())})
                if sub.strip("/") == "stream":
                    if not streams.acquire(blocking=False):
                        return self._send(503, json.dumps({"detail": f"{mesh.max_streams} event streams are open; retry shortly"}).encode(),
                                          "application/json", {"Retry-After": "10"})
                    try:
                        return self._stream(g, q)
                    finally:
                        streams.release()
                status, obj = dispatch(g, "GET", sub, query=q)
                return self._json(status, obj)
            return self._json(404, {"detail": "no such route"})

        def _stream(self, g, q: dict) -> None:
            one = {k: v[0] for k, v in q.items()}
            inv = str(one.get("inv") or "")
            last = str(self.headers.get("Last-Event-ID") or "")
            try:
                after = int(one.get("after") or (last.split(":")[0] if last else 0) or 0)
                toff = int(one.get("after_telemetry") or (last.split(":")[1] if ":" in last else 0) or 0)
                max_s = min(max(float(one.get("max_seconds") or 0), 0.0), 86400.0)
                poll = min(max(float(one.get("poll") or 0.5), 0.1), 10.0)
            except ValueError:
                return self._json(400, {"detail": "after, max_seconds and poll must be numbers"})
            tel = Path(str(g.path) + ".telemetry.jsonl") if getattr(g, "path", None) else None
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Accel-Buffering", "no")):
                self.send_header(k, v)
            self.end_headers()
            start = time.monotonic()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    out = g.events(after=after, inv=inv, limit=500)
                    for e in out["events"]:
                        self.wfile.write(f"id: {e['seq']}:{toff}\nevent: record\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n".encode())
                        after = int(e["seq"])
                    rows, toff2 = read_telemetry(tel, toff, inv, 500) if tel and tel.is_file() else ([], toff)
                    for t in rows:
                        self.wfile.write(f"id: {after}:{toff2}\nevent: telemetry\ndata: {json.dumps(t, ensure_ascii=False, default=str)}\n\n".encode())
                    toff = toff2
                    if out["events"] or rows:
                        self.wfile.flush()
                    if max_s and time.monotonic() - start >= max_s:
                        self.wfile.write(f"event: end\ndata: {json.dumps({'head': after})}\n\n".encode())
                        self.wfile.flush()
                        return
                    if len(out["events"]) < 500 and len(rows) < 500:
                        time.sleep(poll)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        # ── ORCID ──
        def _login(self) -> None:
            if not mesh.orcid["client_id"]:
                return self._html(page("Sign in", """<section class="hero"><h1>Sign in with ORCID</h1>
<p class="lede">This mesh has no ORCID client configured, so it runs in dev mode: identities and agent tokens are made on the host with
<code>hypmesh identity add-researcher</code> and <code>hypmesh identity mint-agent</code>. Set <code>HYPMESH_ORCID_CLIENT_ID</code> and
<code>HYPMESH_ORCID_CLIENT_SECRET</code> (a public API client at orcid.org, redirect URI <code>&lt;base&gt;/callback</code>) to let researchers sign in here.</p></section>"""))
            state = secrets.token_urlsafe(16)
            params = {"client_id": mesh.orcid["client_id"], "response_type": "code", "scope": "/authenticate",
                      "redirect_uri": f"{mesh.base_url}/callback", "state": state}
            url = f"https://{mesh.orcid['host']}/oauth/authorize?" + urllib.parse.urlencode(params)
            self._redirect(url, {"Set-Cookie": f"hm_state={state}; Path=/; HttpOnly; SameSite=Lax{'; Secure' if secure else ''}"})

        def _callback(self, q: dict) -> None:
            code = (q.get("code") or [""])[0]
            state = (q.get("state") or [""])[0]
            cookie_state = ""
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hm_state":
                    cookie_state = v
            if not code or not state or state != cookie_state:
                return self._json(400, {"detail": "the sign-in did not complete (missing code or state)"})
            data = urllib.parse.urlencode({"client_id": mesh.orcid["client_id"], "client_secret": mesh.orcid["client_secret"],
                                           "grant_type": "authorization_code", "code": code,
                                           "redirect_uri": f"{mesh.base_url}/callback"}).encode()
            req = urllib.request.Request(f"https://{mesh.orcid['host']}/oauth/token", data=data, method="POST",
                                         headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    tok = json.load(r)
            except Exception as e:
                return self._json(502, {"detail": f"ORCID did not answer: {type(e).__name__}"})
            orcid, name = tok.get("orcid", ""), tok.get("name") or tok.get("orcid", "")
            try:
                mesh.ids.add_researcher(orcid, name, verified=True)
            except ValueError as e:
                return self._json(400, {"detail": str(e)})
            sid = mesh.ids.open_session(orcid)
            self._redirect("/me", {"Set-Cookie": [f"hm_session={sid}; Path=/; HttpOnly; SameSite=Lax{'; Secure' if secure else ''}; Max-Age=2592000",
                                                  f"hm_state=; Path=/; HttpOnly; SameSite=Lax{'; Secure' if secure else ''}; Max-Age=0"]})

        # ── POST ──
        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            p = u.path.rstrip("/")
            try:
                if p.startswith("/q/"):
                    return self._question_post(p)
                if not self._same_origin():
                    return self._json(403, {"detail": "cross-origin request refused"})
                if p == "/logout":
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        k, _, v = part.strip().partition("=")
                        if k == "hm_session":
                            mesh.ids.close_session(v)
                    return self._redirect("/", {"Set-Cookie": "hm_session=; Path=/; Max-Age=0"})
                who = self._session()
                if not who:
                    return self._redirect("/login")
                if p == "/me/agents":
                    name = str(self._body().get("name") or "").strip()[:40] or "agent"
                    agent, token = mesh.ids.mint_agent(who.orcid, name)
                    flash = (f"Token for <code>{esc(name)}</code> ({esc(agent['id'])}), shown once — copy it now: "
                             f"<code class=\"token\">{esc(token)}</code>")
                    return self._html(me_page(mesh, who, flash))
                if p.startswith("/me/agents/") and p.endswith("/revoke"):
                    aid = p.split("/")[3]
                    a = mesh.ids.agent(aid)
                    if a and a["orcid"] == who.orcid:
                        mesh.ids.revoke_agent(aid)
                    return self._redirect("/me?flash=" + urllib.parse.quote(f"agent {aid} revoked"))
                return self._json(404, {"detail": "no such route"})
            except Refusal as e:
                return self._json(e.status, {"detail": e.detail})
            except GraphError as e:
                return self._json(422, {"detail": str(e)})
            except KeyError as e:
                return self._json(404, {"detail": f"no question {e}"})

        def _seat(self, qid: str, action: str, *, author: str = "", wrote: bool = False) -> None:
            """Note a seat at the table: a request with an agent token (and the seat's name, from X-HypMesh-Seat)."""
            token = self._bearer()
            if not token:
                return
            try:
                who = mesh.gate.identify(token)
            except Refusal:
                return
            mesh.seen(qid, who, author or self.headers.get("X-HypMesh-Seat") or "", action, wrote)

        def _question_post(self, p: str) -> None:
            parts = p.split("/")
            qid = parts[2] if len(parts) > 2 else ""
            rest = "/".join(parts[3:])
            if not mesh.q.get(qid):
                return self._json(404, {"detail": f"no question {qid!r}"})
            if not rest.startswith("api/graph"):
                return self._json(404, {"detail": "no such route"})
            sub = rest[len("api/graph"):].strip("/")
            body = self._body()
            if sub == "write":
                try:
                    status, obj = mesh.gate.write(qid, self._bearer(), body)
                except GraphError as e:
                    self._seat(qid, f"refused: {body.get('op')} {body.get('type') or ''}".strip(), author=str(body.get("author") or ""), wrote=True)
                    return self._json(422, {"detail": str(e)})
                what = f"{body.get('op')} {body.get('type') or ''}".strip()
                code = obj.get("code") if isinstance(obj, dict) else ""
                if status != 200:                    # the record refused it: shown as a refusal, not a write
                    what = "refused: " + what
                    code = ""
                self._seat(qid, what + (f" {code}" if code else ""), author=str(body.get("author") or ""), wrote=True)
                return self._json(status, obj)
            status, obj = dispatch(mesh.q.graph(qid), "POST", "/" + sub, body=body)
            return self._json(status, obj)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv


def serve(mesh: Mesh, host: str = "127.0.0.1", port: int = 8800) -> None:
    srv = make_server(mesh, host, port)
    print(f"HypMesh on http://{host}:{srv.server_address[1]}/  (data {mesh.data}; {len(mesh.q.list())} question(s)) — Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        mesh.close()


__all__ = ["Mesh", "make_server", "serve"]
