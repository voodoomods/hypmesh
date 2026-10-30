from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .server import Mesh, serve


def _mesh(a) -> Mesh:
    return Mesh.from_env(a.data or "")


def cmd_serve(a) -> int:
    serve(_mesh(a), a.host, a.port)
    return 0


def cmd_question(a) -> int:
    m = _mesh(a)
    if a.action == "list":
        for q in m.q.list():
            st = m.status(q["id"])
            print(f"{q['id']:40s} {q.get('state', 'open'):7s} best={st['best'] or '-':4s} claims={st['claims']:<3d} {q['title']}")
        return 0
    if a.action == "open":
        try:
            q = m.q.open(a.title, a.objective, domain=a.domain, opened_by=a.by, hazard_reviewed=a.hazard_reviewed, qid=a.id)
        except ValueError as e:
            print(f"[hypmesh] refused: {e}", file=sys.stderr)
            return 2
        print(f"[hypmesh] opened {q['id']}: {q['title']}\n  {m.base_url}/q/{q['id']}")
        return 0
    if a.action == "close":
        q = m.q.close(a.id, a.why)
        print(f"[hypmesh] closed {q['id']}: {a.why}")
        return 0
    return 2


def cmd_identity(a) -> int:
    m = _mesh(a)
    ids = m.ids
    try:
        if a.action == "add-org":
            print(json.dumps(ids.add_org(a.id, a.name, a.domain)))
        elif a.action == "add-researcher":
            print(json.dumps(ids.add_researcher(a.orcid, a.name, a.org, verified=a.verified)))
        elif a.action == "vouch":
            print(json.dumps(ids.vouch(a.orcid, a.org)))
        elif a.action == "mint-agent":
            agent, token = ids.mint_agent(a.orcid, a.name)
            print(f"agent {agent['id']} ({agent['name']}) under {agent['orcid']}\ntoken (shown once): {token}")
        elif a.action == "revoke":
            print("revoked" if ids.revoke_agent(a.agent) else "no such active agent")
        elif a.action == "list":
            for o in ids.orgs():
                print(f"org {o['id']}: {o['name']}")
            for r in ids.researchers():
                agents = ", ".join(f"{x['name']}({x['id']}{', revoked' if x['revoked'] else ''})" for x in ids.agents(r["orcid"]))
                print(f"  {r['orcid']} {r['name']} org={r['org'] or '-'} verified={bool(r['verified'])} agents: {agents or '-'}")
        elif a.action == "instrument-token":
            print(m.data / "tokens" / "instrument.token")
    except ValueError as e:
        print(f"[hypmesh] {e}", file=sys.stderr)
        return 2
    return 0


def cmd_instrument(a) -> int:
    try:
        from . import instrument
    except ImportError as e:  # pragma: no cover
        print(f"[hypmesh] {e}", file=sys.stderr)
        return 2
    try:
        import hypswarm  # noqa: F401
    except ImportError:
        print("[hypmesh] the instrument needs hypswarm: pip install 'hypmesh[instrument]'", file=sys.stderr)
        return 2
    token = a.token or (Path(a.token_file).read_text(encoding="utf-8").strip() if a.token_file else "")
    if not token:
        token = (Mesh.from_env(a.data or "").data / "tokens" / "instrument.token").read_text(encoding="utf-8").strip()
    reproducer = instrument.hypdmz_sandbox(a.container, a.seconds) if a.sandbox == "hypdmz" else instrument.subprocess_sandbox(a.seconds)
    if a.once:
        filed = instrument.run_once(a.mesh, token, reproducer, questions=a.question or None)
        print(f"[instrument] filed {len(filed)} reproduction(s)")
        return 0
    instrument.loop(a.mesh, token, reproducer, every=a.every)
    return 0


def cmd_demo(a) -> int:
    """Two organisations, three researchers, an agent each, one question — the smallest mesh that can show L3."""
    m = _mesh(a)
    ids = m.ids
    ids.add_org("example-u", "Example University", "example.edu")
    ids.add_org("sample-labs", "Sample Labs", "sample.org")
    out = []
    for orcid, name, org, agent in (("0000-0002-1825-0097", "Ada Example", "example-u", "ada-swarm"),
                                    ("0000-0001-5109-3700", "Blaise Sample", "sample-labs", "blaise-swarm"),
                                    ("0000-0002-9079-593X", "Cyrus Unaffiliated", "", "cyrus-agent")):
        ids.add_researcher(orcid, name, org)
        agent_row, token = ids.mint_agent(orcid, agent)
        out.append((name, orcid, org or "unaffiliated", agent_row["id"], token))
    if not m.q.get("demo-treatment-effect"):
        m.q.open("Does the treatment group score higher?",
                 "Two groups of students took the same test (scores.csv: group, score). Is the treatment group's mean higher, "
                 "and is the difference significant at the 5% level? Register every number as a measurement.",
                 domain="statistics", opened_by="curator", hazard_reviewed=True, qid="demo-treatment-effect")
    print(f"[hypmesh] demo mesh in {m.data}\n")
    for name, orcid, org, aid, token in out:
        print(f"  {name:20s} {orcid}  {org:14s} agent {aid}\n    token: {token}")
    print(f"\n  question: {m.base_url}/q/demo-treatment-effect\n  instrument token: {m.data / 'tokens' / 'instrument.token'}\n"
          f"\n  hypmesh serve --data {m.data}\n  hypswarm run --task task.toml --profile scientific --record-url {m.base_url}/q/demo-treatment-effect --token <one of the above>")
    return 0


def cmd_backup(a) -> int:
    from .backup import snapshot
    data = Path(a.data or __import__("os").environ.get("HYPMESH_DATA") or Path.home() / ".hypmesh").expanduser()
    try:
        r = snapshot(data, Path(a.to).expanduser(), keep=a.keep)
    except (FileNotFoundError, OSError) as e:
        print(f"[hypmesh] backup failed: {e}", file=sys.stderr)
        return 1
    for c in r["checks"]:
        chain = (f" chain={'ok' if c['chain']['ok'] else 'BROKEN'} ({c['chain']['checked']} events)"
                 + (f": {c['chain']['problem']}" if c["chain"].get("problem") else "")) if "chain" in c else ""
        print(f"  {c['file']:50s} integrity={c['integrity']}{chain}")
    print(f"[hypmesh] {'ok' if r['ok'] else 'FAILED'}: {r['snapshot']} ({r['files']} files)"
          + (f"; pruned {len(r['pruned'])}" if r["pruned"] else ""))
    return 0 if r["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="hypmesh", description="A public mesh of HypGraph records, one per open question.")
    p.add_argument("--version", action="version", version=f"hypmesh {__version__}")
    p.add_argument("--data", default="", help="the mesh's data dir (default $HYPMESH_DATA or ~/.hypmesh)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the mesh"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8800)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("question", help="open, list, close questions (the curator)")
    qs = s.add_subparsers(dest="action", required=True)
    o = qs.add_parser("open"); o.add_argument("--title", required=True); o.add_argument("--objective", required=True)
    o.add_argument("--domain", default=""); o.add_argument("--by", default="curator"); o.add_argument("--id", default="")
    o.add_argument("--hazard-reviewed", action="store_true", help="say that the hazard review was done (required)")
    qs.add_parser("list")
    c = qs.add_parser("close"); c.add_argument("id"); c.add_argument("--why", required=True)
    s.set_defaults(fn=cmd_question)

    s = sub.add_parser("identity", help="organisations, researchers, agent tokens (dev mode; ORCID sign-in on the web)")
    ii = s.add_subparsers(dest="action", required=True)
    x = ii.add_parser("add-org"); x.add_argument("id"); x.add_argument("name"); x.add_argument("--domain", default="")
    x = ii.add_parser("add-researcher"); x.add_argument("orcid"); x.add_argument("name"); x.add_argument("--org", default="")
    x.add_argument("--verified", action="store_true")
    x = ii.add_parser("vouch"); x.add_argument("orcid"); x.add_argument("org")
    x = ii.add_parser("mint-agent"); x.add_argument("orcid"); x.add_argument("name")
    x = ii.add_parser("revoke"); x.add_argument("agent")
    ii.add_parser("list"); ii.add_parser("instrument-token")
    s.set_defaults(fn=cmd_identity)

    s = sub.add_parser("instrument", help="the reproduction worker (needs hypmesh[instrument])")
    s.add_argument("--mesh", default="http://127.0.0.1:8800"); s.add_argument("--token", default=""); s.add_argument("--token-file", default="")
    s.add_argument("--sandbox", choices=["subprocess", "hypdmz"], default="subprocess"); s.add_argument("--container", default="hypdmz-swarm")
    s.add_argument("--seconds", type=int, default=60); s.add_argument("--every", type=float, default=30.0)
    s.add_argument("--once", action="store_true"); s.add_argument("--question", action="append", default=[])
    s.set_defaults(fn=cmd_instrument)

    s = sub.add_parser("backup", help="consistent, verified snapshot of the data dir (SQLite backup API + hash-chain check)")
    s.add_argument("--to", required=True, help="the directory that holds snapshots")
    s.add_argument("--keep", type=int, default=14, help="how many good snapshots to keep (0 = keep all)")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("demo", help="two organisations, three researchers with agents, one question"); s.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
