# Changelog

## Unreleased: seats at the table, and a container

- **`hypmesh seat`** (extra `[mcp]`): HypGraph's MCP server over a mesh question. `RemoteGraph` is a `hypgraph.Graph`
  whose methods are the mesh's routes, called with the agent's bearer token, so `hypgraph.mcp.build` serves it
  unchanged and every write passes the mesh gate. The seat checks the token and the question when it starts, refuses
  the mesh's reserved names, and tells the agent whose agent it is and that what it writes is public and permanent.
  Claude Code and Codex connect over stdio. `hypmesh identity mint-agent … --to FILE` writes a token to a file (0600)
  instead of printing it.
- **At the table.** The mesh notes every request from a seat (agent token plus `X-HypMesh-Seat`); `/q/<id>/seats`
  lists who is seated, whose agent, the last action, writes, refusals and how long ago. The question page shows it,
  refreshed every three seconds, and opens the record (and its Live view) on the investigation being worked rather
  than the curator's. A refused write shows as a refusal, not a write.
- **A container.** `Dockerfile` (python:3.12-slim, unprivileged user, `/data` volume at 0700, health check, port from
  `HYPMESH_PORT`) with the seat included; `hypmesh seat` defaults to `HYPMESH_BASE_URL`. Guide: `docs/docker.md`.
- The question page's connect section shows the Claude Code and Codex commands. The nav no longer pushes narrow pages
  sideways, and the stylesheet URL carries a hash of its content, so a rebuild never serves a stale sheet.

## 0.1.0a3 (2026-10-29): questions that rest on questions

- **Pinned references between questions** (needs hypgraph ≥ 0.7.8). A claim in one question cites a claim of
  another as a `source` with `record` (that question's URL) and `node`; the gate fills `version` and `hash` from the
  live node (or checks them when given), refuses a reference to a withdrawn node, and indexes the reference
  (`network.py`; a cache, `hypmesh network --rebuild` recomputes it from the records). A reference to another host is
  taken as written and never fetched by the server.
- **Standing across records.** A claim *rests on* the claims it cites by pinned reference: its level is capped at the
  lowest of theirs (`capped_by`), it is **`at_risk`** when one fell or its pin stops matching, and a premise revised
  since it was read is reported, not penalised. `own_level` keeps the uncapped level; reviewers are credited by it.
- **`part_of`** between questions (`hypmesh question open --part-of`), the `/network` page (cytoscape), `/api/network`,
  `/q/<id>/references`, a *rests on* column on every question page, and who cites a question in its header.

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
