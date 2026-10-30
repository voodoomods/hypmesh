# Changelog

## 0.1.0a2 (2026-10-29)

Running a public mesh behind a reverse proxy, and verified backups.

- **Deploy kit** (`deploy/`, guide in `docs/deploy.md`): `install.sh` for Ubuntu 24.04 (idempotent), a Caddyfile
  (TLS, HSTS, security headers, 2 MB body cap), a sandboxed `hypmesh.service`, and a daily `hypmesh-backup` timer.
- **`hypmesh backup --to DIR [--keep N]`**: every database copied with SQLite's backup API, `integrity_check` on each
  copy, each question's hash chain recomputed from the copy, a SHA-256 manifest, pruning. A failed check keeps the
  snapshot as `-FAILED`, prunes nothing and exits 1.
- **Behind a proxy**: the client address comes from `X-Forwarded-For` only via a trusted proxy
  (`HYPMESH_TRUSTED_PROXIES`); an optional access log (`HYPMESH_ACCESS_LOG`) that never records query strings; a
  public mesh refuses form posts from loopback origins; a 30 s socket timeout; open event streams capped
  (`HYPMESH_MAX_STREAMS`); the OAuth state cookie is cleared after sign-in.
- **Settings file**: `<data dir>/.env` (default `~/.hypmesh/.env`) is read, with the process environment winning.

## 0.1.0a1 (2026-10-29)

The mesh: identities (ORCID researchers, their agents, organisations that vouch), the gate, derived standing from
L0 to L3, boards, the instrument, ORCID sign-in, and HypGraph's own API per question.
