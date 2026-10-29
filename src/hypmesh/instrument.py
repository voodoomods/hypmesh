"""The mesh's instrument: re-runs the code behind every registered result and files what it found, as `instrument`.

This is HypSwarm's reproducer (``hypswarm.reproduce.reproduce_result``) pointed at the mesh: for a result whose run
carries its source, the code is executed again in a sandbox of the host's choosing, each registered value is looked
for in the output, and a lab run that ``replicates`` the author's run is filed with the outcome. A claim reaches
L3 only after this has happened to the computation it rests on.

    hypmesh instrument --mesh http://127.0.0.1:8800 --token-file ~/.hypmesh/tokens/instrument.token
    hypmesh instrument ... --sandbox hypdmz        # run the code inside a HypDMZ swarm container (podman exec)

The instrument never trusts the author's output; it trusts its own run. It needs ``hypmesh[instrument]``."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .questions import INSTRUMENT


def subprocess_sandbox(seconds: int = 60) -> Callable[[str], str]:
    """``python -I`` in a scratch folder, no inherited environment to speak of, a time limit. Not a security boundary;
    use --sandbox hypdmz for one."""
    scratch = Path(tempfile.mkdtemp(prefix="hypmesh-instrument-"))

    def run(code: str) -> str:
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code], cwd=scratch, capture_output=True, text=True,
                               timeout=seconds, env={"PATH": "", "TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)})
        except subprocess.TimeoutExpired:
            return f"error: no output within {seconds} s"
        out = p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr.strip() else "")
        return out[:8000] or "(no output)"
    return run


def hypdmz_sandbox(container: str = "hypdmz-swarm", seconds: int = 60) -> Callable[[str], str]:
    """The code runs inside a HypDMZ swarm container: read-only root, loopback only, capped memory and processes."""
    def run(code: str) -> str:
        try:
            p = subprocess.run(["podman", "exec", "-i", "-u", "swarm", "-w", "/tmp", container, "python", "-I", "-c", code],
                               capture_output=True, text=True, timeout=seconds + 10)
        except subprocess.TimeoutExpired:
            return f"error: no output within {seconds} s"
        out = p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr.strip() else "")
        return out[:8000] or "(no output)"
    return run


def _get(base: str, path: str, token: str, **params) -> dict:
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))) if params else ""
    req = urllib.request.Request(f"{base}{path}{qs}", headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pending(base: str, qid: str, token: str) -> list[tuple[str, str]]:
    """(inv, result id) for every result by an author other than the instrument whose run carries source and has
    not been replicated by the instrument yet."""
    out = []
    for hit in _get(base, f"/q/{qid}/api/graph/search", token, type="result", limit=500).get("results", []):
        res = _get(base, f"/q/{qid}/api/graph/node/{hit['id']}", token)
        if res.get("created_by") == INSTRUMENT:
            continue
        runs = [e["src"] for e in res.get("in", []) if e.get("rel") == "produced"]
        if not runs:
            continue
        run = _get(base, f"/q/{qid}/api/graph/node/{runs[0]}", token)
        if run.get("created_by") == INSTRUMENT:
            continue
        if not str(((run.get("body") or {}).get("config") or {}).get("source") or "").strip():
            continue
        done = False
        for e in run.get("in", []):
            if e.get("rel") == "replicates":
                lab = _get(base, f"/q/{qid}/api/graph/node/{e['src']}", token)
                if lab.get("created_by") == INSTRUMENT:
                    done = True
                    break
        if not done:
            out.append((res.get("inv", ""), hit["id"]))
    return out


def run_once(base: str, token: str, reproducer: Callable[[str], str], *, questions: list[str] | None = None) -> list[dict]:
    """One pass over every open question. Returns what was filed."""
    from hypswarm.http_record import HttpRecord
    from hypswarm.reproduce import reproduce_result

    base = base.rstrip("/")
    qs = questions or [q["id"] for q in _get(base, "/api/questions", token)["questions"] if q.get("state") == "open"]
    filed = []
    for qid in qs:
        todo = pending(base, qid, token)
        if not todo:
            continue
        rec = HttpRecord(f"{base}/q/{qid}", token=token)
        try:
            for inv, rid in todo:
                try:
                    out = asyncio.run(reproduce_result(rec, inv, rid, reproducer))
                except Exception as e:  # noqa: BLE001 — one bad result must not stop the pass
                    out = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                filed.append({"question": qid, "result": rid, **(out or {})})
                print(f"[instrument] {qid} {rid}: {json.dumps(out, default=str)[:160]}")
        finally:
            rec.close()
    return filed


def loop(base: str, token: str, reproducer: Callable[[str], str], every: float = 30.0) -> None:
    while True:
        try:
            run_once(base, token, reproducer)
        except Exception as e:  # noqa: BLE001
            print(f"[instrument] pass failed: {type(e).__name__}: {e}")
        time.sleep(every)


__all__ = ["subprocess_sandbox", "hypdmz_sandbox", "pending", "run_once", "loop"]
