"""Running behind a reverse proxy, and backups: the client address from X-Forwarded-For only via a trusted proxy,
an access log that never carries a query, the origin check on a public mesh, the event-stream cap, the OAuth state
cookie cleared after sign-in, and verified, pruned snapshots of the data dir."""
from __future__ import annotations

import http.client
import io
import json
import threading
import time
from pathlib import Path

import pytest

from hypmesh import server as srvmod
from hypmesh.backup import snapshot
from hypmesh.server import Mesh, _is_trusted, make_server

ADA = "dev:ada"


def start(m: Mesh):
    srv = make_server(m, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def request(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, r.getheaders(), data)
    c.close()
    return out


@pytest.fixture
def public(tmp_path):
    m = Mesh(tmp_path, base_url="https://mesh.example.org", access_log=True)
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    srv, port = start(m)
    yield m, port
    srv.shutdown()
    m.close()


def test_trusted_proxy_ranges():
    assert _is_trusted("127.0.0.1", ("127.0.0.1", "::1"))
    assert _is_trusted("::1", ("127.0.0.1", "::1"))
    assert _is_trusted("10.1.2.3", ("10.0.0.0/8",))
    assert not _is_trusted("203.0.113.9", ("127.0.0.1", "::1"))
    assert not _is_trusted("not-an-ip", ("127.0.0.1",))


def test_access_log_uses_forwarded_client_and_never_the_query(public, capfd):
    m, port = public
    request(port, "GET", "/healthz", {"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})
    request(port, "GET", "/callback?code=SECRETCODE&state=abc", {"X-Forwarded-For": "203.0.113.9"})
    err = capfd.readouterr().err
    assert '203.0.113.9 "GET /healthz" 200' in err          # the nearest untrusted hop, not a spoofable leftmost one
    assert '"GET /callback" 400' in err
    assert "SECRETCODE" not in err and "state=" not in err


def test_forwarded_for_ignored_from_an_untrusted_peer(tmp_path, capfd):
    m = Mesh(tmp_path, base_url="https://mesh.example.org", access_log=True, trusted_proxies=("10.9.9.9",))
    srv, port = start(m)
    try:
        request(port, "GET", "/healthz", {"X-Forwarded-For": "203.0.113.9"})
        err = capfd.readouterr().err
        assert '127.0.0.1 "GET /healthz" 200' in err and "203.0.113.9" not in err
    finally:
        srv.shutdown()
        m.close()


def test_a_public_mesh_refuses_loopback_origins(public):
    m, port = public
    st, _, _ = request(port, "POST", "/logout", {"Origin": "http://localhost:3000", "Content-Length": "0"})
    assert st == 403
    st, _, _ = request(port, "POST", "/logout", {"Origin": "https://mesh.example.org", "Content-Length": "0"})
    assert st == 303
    st, _, _ = request(port, "POST", "/logout", {"Sec-Fetch-Site": "cross-site", "Content-Length": "0"})
    assert st == 403


def test_event_streams_are_capped(tmp_path):
    m = Mesh(tmp_path, base_url="https://mesh.example.org", max_streams=1)
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    srv, port = start(m)
    try:
        first = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        first.request("GET", "/q/x-vs-y/api/graph/stream?max_seconds=3&poll=0.2")
        r1 = first.getresponse()
        assert r1.status == 200
        st, headers, body = request(port, "GET", "/q/x-vs-y/api/graph/stream?max_seconds=1")
        assert st == 503 and dict(headers).get("Retry-After") == "10" and b"event streams" in body
        r1.read()                                          # the first stream ends after max_seconds ...
        first.close()
        deadline = time.time() + 5
        while time.time() < deadline:                      # ... and its slot is free again
            st, _, _ = request(port, "GET", "/q/x-vs-y/api/graph/stream?max_seconds=0.2")
            if st == 200:
                break
            time.sleep(0.2)
        assert st == 200
    finally:
        srv.shutdown()
        m.close()


def _github(monkeypatch, user: dict):
    """Stand in for GitHub: the token exchange, then the profile. Returns the URLs asked for."""
    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def urlopen(req, timeout=0):
        seen.append(req.full_url)
        if req.full_url.startswith("https://github.com/login/oauth/access_token"):
            return Answer(json.dumps({"access_token": "gho_test", "token_type": "bearer"}).encode())
        assert req.full_url == "https://api.github.com/user" and req.get_header("Authorization") == "Bearer gho_test"
        return Answer(json.dumps(user).encode())
    monkeypatch.setattr(srvmod.urllib.request, "urlopen", urlopen)
    return seen


def test_github_sign_in_makes_a_verified_researcher_and_clears_the_state_cookie(tmp_path, monkeypatch):
    m = Mesh(tmp_path, base_url="https://mesh.example.org", github_client_id="Iv1.test", github_client_secret="x")
    seen = _github(monkeypatch, {"id": 583231, "login": "octocat", "name": "The Octocat", "created_at": "2011-01-25T18:44:36Z"})
    srv, port = start(m)
    try:
        st, headers, _ = request(port, "GET", "/login")
        assert st == 303 and dict(headers)["Location"].startswith("https://github.com/login/oauth/authorize?client_id=Iv1.test")
        st, headers, _ = request(port, "GET", "/callback?code=c&state=s1", {"Cookie": "hm_state=s1"})
        cookies = [v for k, v in headers if k.lower() == "set-cookie"]
        assert st == 303 and dict(headers)["Location"] == "/me"
        assert any(c.startswith("hm_session=") and "Secure" in c for c in cookies)
        assert any(c.startswith("hm_state=;") and "Max-Age=0" in c for c in cookies)
        r = m.ids.researcher("github:583231")
        assert r["verified"] == 1 and r["handle"] == "octocat" and r["name"] == "The Octocat" and r["provider"] == "github"
        assert len(seen) == 2
        sid = [c for c in cookies if c.startswith("hm_session=")][0].split(";")[0].split("=", 1)[1]
        st, _, body = request(port, "GET", "/me", {"Cookie": f"hm_session={sid}"})
        assert st == 200 and b'href="https://github.com/octocat">@octocat' in body and b"signed in with GitHub" in body
    finally:
        srv.shutdown()
        m.close()


def test_a_new_github_account_is_turned_away(tmp_path, monkeypatch):
    import time as _t
    m = Mesh(tmp_path, base_url="https://mesh.example.org", github_client_id="Iv1.test", github_client_secret="x", github_min_age_days=30)
    _github(monkeypatch, {"id": 99, "login": "fresh", "created_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 3 * 86400))})
    srv, port = start(m)
    try:
        st, _, body = request(port, "GET", "/callback?code=c&state=s1", {"Cookie": "hm_state=s1"})
        assert st == 403 and b"at least 30 days old" in body and m.ids.researcher("github:99") is None
    finally:
        srv.shutdown()
        m.close()


def test_an_old_data_dir_is_refused_plainly(tmp_path):
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "mesh.db"))
    con.execute("CREATE TABLE researchers (person TEXT PRIMARY KEY, name TEXT NOT NULL)")     # no researcher ids
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="older than 0.1.0a5"):
        Mesh(tmp_path)

def test_backup_is_consistent_verified_and_pruned(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "backups"
    m = Mesh(data)
    m.ids.add_researcher(ADA, "Ada")
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    m.close()
    r1 = snapshot(data, dest, keep=1)
    assert r1["ok"] and r1["files"] >= 4
    rec = [c for c in r1["checks"] if c["file"] == "questions/x-vs-y.hypgraph"][0]
    assert rec["integrity"] == "ok" and rec["chain"]["ok"] and rec["chain"]["checked"] >= 1
    man = json.loads((Path(r1["snapshot"]) / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["ok"] and "mesh.db" in man["files"] and "tokens/instrument.token" in man["files"]
    r2 = snapshot(data, dest, keep=1)
    assert r2["ok"] and r2["pruned"] == [Path(r1["snapshot"]).name]
    assert [p.name for p in dest.iterdir()] == [Path(r2["snapshot"]).name]
    with pytest.raises(FileNotFoundError):
        snapshot(tmp_path / "nothing-here", dest)


def test_a_tampered_record_fails_the_backup_and_is_not_pruned(tmp_path):
    import sqlite3
    from hypmesh.cli import main
    data, dest = tmp_path / "data", tmp_path / "backups"
    m = Mesh(data)
    m.q.open("Does X beat Y?", "Measure X and Y on the data and say which is larger, with the count.", hazard_reviewed=True, qid="x-vs-y")
    m.close()
    assert main(["--data", str(data), "backup", "--to", str(dest), "--keep", "1"]) == 0
    c = sqlite3.connect(str(data / "questions" / "x-vs-y.hypgraph"))
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        c.execute(f"DROP TRIGGER {t}")
    c.execute("UPDATE versions SET body = replace(body, 'e', 'E') WHERE rowid = (SELECT max(rowid) FROM versions)")
    c.commit()
    c.close()
    assert main(["--data", str(data), "backup", "--to", str(dest), "--keep", "1"]) == 1
    names = sorted(p.name for p in dest.iterdir())
    assert len(names) == 2 and names[1].endswith("-FAILED")           # the good one is kept, the failure beside it
    man = json.loads((dest / names[1] / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["ok"] is False
    rec = [x for x in man["checks"] if x["file"].endswith("x-vs-y.hypgraph")][0]
    assert rec["chain"]["ok"] is False and "differs from its event" in rec["chain"]["problem"]
