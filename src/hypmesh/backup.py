"""Consistent, verified backups of a mesh's data dir.

A plain file copy of a live SQLite database can catch it mid-write. ``backup`` copies every database with SQLite's
own backup API, then checks each copy (``PRAGMA integrity_check``, and for question records HypGraph's hash chain),
writes a manifest with every file's SHA-256, and prunes old snapshots. A snapshot that fails a check is kept, marked
``FAILED``, and the command exits non-zero, so a timer that runs it shows the failure.

The snapshot holds secrets: token hashes, web session ids and the instrument token. Keep the backup directory
readable by the mesh's user only, and encrypt any copy that leaves the machine."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from hypgraph import Graph

PREFIX = "hypmesh-"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _integrity(path: Path) -> str:
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return str(c.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        c.close()


def snapshot(data: Path, dest: Path, *, keep: int = 14) -> dict:
    """Copy ``data`` into ``dest/hypmesh-<UTC stamp>/``, verify the copy, prune to the newest ``keep``."""
    data, dest = Path(data), Path(dest)
    if not (data / "mesh.db").is_file():
        raise FileNotFoundError(f"{data} is not a mesh data dir (no mesh.db)")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snap = dest / f"{PREFIX}{stamp}"
    n = 1
    while snap.exists():
        n += 1
        snap = dest / f"{PREFIX}{stamp}-{n}"
    work = snap.with_name(snap.name + ".partial")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    checks: list[dict] = []
    _sqlite_copy(data / "mesh.db", work / "mesh.db")
    checks.append({"file": "mesh.db", "integrity": _integrity(work / "mesh.db")})
    for rec in sorted((data / "questions").glob("*.hypgraph")) if (data / "questions").is_dir() else []:
        out = work / "questions" / rec.name
        _sqlite_copy(rec, out)
        row = {"file": f"questions/{rec.name}", "integrity": _integrity(out)}
        g = Graph()                     # in memory: the live record is never opened, the copy is read read-only
        try:
            v = g.verify(path=str(out))
        finally:
            g.close()
        row["chain"] = {"ok": bool(v.get("ok")), "checked": v.get("checked"), "head": v.get("head")}
        if v.get("problem"):
            row["chain"]["problem"] = str(v["problem"])
        checks.append(row)
    for plain in ("questions.json",):
        if (data / plain).is_file():
            shutil.copy2(data / plain, work / plain)
    if (data / "tokens").is_dir():
        shutil.copytree(data / "tokens", work / "tokens")

    files = {str(f.relative_to(work).as_posix()): _sha256(f) for f in sorted(work.rglob("*")) if f.is_file()}
    ok = all(c["integrity"] == "ok" and c.get("chain", {"ok": True})["ok"] for c in checks)
    manifest = {"created": stamp, "source": str(data), "ok": ok, "checks": checks, "files": files}
    (work / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    final = snap if ok else snap.with_name(snap.name + "-FAILED")
    work.rename(final)

    pruned = []
    if ok and keep > 0:
        good = sorted(p for p in dest.iterdir() if p.is_dir() and p.name.startswith(PREFIX)
                      and not p.name.endswith(("-FAILED", ".partial")))
        for old in good[:-keep]:
            shutil.rmtree(old)
            pruned.append(old.name)
    return {"ok": ok, "snapshot": str(final), "checks": checks, "files": len(files), "pruned": pruned}


__all__ = ["snapshot"]
