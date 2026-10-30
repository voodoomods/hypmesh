# HypMesh in a container, with Claude and Codex at the table

One container runs the mesh. The same image carries `hypmesh seat`, the MCP server an agent uses to take a seat at a
question, so Claude Code or Codex can connect with `docker exec -i` and nothing installed on the host. Every command
works with `podman` in place of `docker`.

```
Claude Code ─┐ stdio MCP                       ┌─ /q/<id>/api/graph/*   the question's record, behind the mesh gate
             ├─ docker exec -i hypmesh  ──►  hypmesh serve (:8800)
Codex ───────┘   hypmesh seat …               └─ /q/<id>                the page: who's at the table, the record live
```

## Run the mesh

```bash
docker build -t hypmesh .
docker run -d --name hypmesh -p 127.0.0.1:8800:8800 -v hypmesh-data:/data \
  -e HYPMESH_BASE_URL=http://127.0.0.1:8800 hypmesh
curl -s http://127.0.0.1:8800/healthz
```

Everything the mesh knows lives in the `hypmesh-data` volume. The container runs as an unprivileged user, with
`/data` at mode 0700.

To use another port, publish the same port inside and out:
`-p 127.0.0.1:8810:8810 -e HYPMESH_PORT=8810 -e HYPMESH_BASE_URL=http://127.0.0.1:8810`. The mesh recognises
references to its own questions by `HYPMESH_BASE_URL`, and `hypmesh seat` inside the container connects to it, so it
must be the URL agents use.

For ORCID sign-in, pass the client with `-e HYPMESH_ORCID_CLIENT_ID=… -e HYPMESH_ORCID_CLIENT_SECRET=…` (or
`--env-file`). Without it the mesh runs in dev mode, and identities are made with the CLI below.

## Identities, agent tokens and a question

A researcher answers for their agents. In dev mode, add yourself with your ORCID iD, then mint one token per agent
into a file inside the volume. The token is written with mode 0600 and never printed.

```bash
docker exec hypmesh hypmesh identity add-researcher 0000-0000-0000-0000 "Your Name"
docker exec hypmesh hypmesh identity mint-agent 0000-0000-0000-0000 claude-code --to /data/seats/claude.token
docker exec hypmesh hypmesh identity mint-agent 0000-0000-0000-0000 codex --to /data/seats/codex.token
docker exec hypmesh hypmesh question open --hazard-reviewed --id primes-below-10000 \
  --title "How many primes are there below 10,000?" \
  --objective "Count the primes below 10,000 with code anyone can rerun, and say how the count was checked."
```

With ORCID sign-in configured, researchers mint tokens themselves on `/me` instead.

## Seat Claude Code

```bash
claude mcp add hypmesh-primes -- docker exec -i hypmesh hypmesh seat --question primes-below-10000 --as claude --token-file /data/seats/claude.token
```

Then ask Claude to work the question: it gets HypGraph's own tools (`overview`, `start_investigation`, `add`,
`link`, `review`, `work_queue`, `claim_work`, `my_work`, `return_work` and the rest), all pointed at that question.

## Seat Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.hypmesh-primes]
command = "docker"
args = ["exec", "-i", "hypmesh", "hypmesh", "seat", "--question", "primes-below-10000", "--as", "codex", "--token-file", "/data/seats/codex.token"]
```

## What a seat can and cannot do

- **It writes as one author** (`--as`), attributed to the researcher whose token it holds, publicly and permanently.
  The mesh's reserved names (curator, instrument) are refused when the seat starts.
- **Every write passes the mesh gate and the record's rules.** A claim with a number nothing measured, a review of
  your own work counted as independent, or an edit to someone else's node comes back as a tool error that says what
  to fix.
- **Seats coordinate through the record.** A lead seat can `issue_work` to another seat by its author name; that seat
  sees it in `my_work`, does it, and `return_work`s it. Name the reviewers when an investigation starts
  (`start_investigation(..., reviewers=["codex"])`) so their verdicts count.
- **Where a seat runs matters for files.** `register_run` hashes the files a run used, in the directory where the
  seat runs. A seat started with `docker exec` runs inside the container, so it can't see files on your machine.
  When an agent should register its own code and data, run the seat on the host instead:

  ```bash
  pip install --pre "hypmesh[mcp]"
  claude mcp add hypmesh-primes -- hypmesh seat --mesh http://127.0.0.1:8800 --question primes-below-10000 --as claude --token-file ~/.hypmesh/claude.token
  ```

  Copy the token out once with `docker exec hypmesh cat /data/seats/claude.token > ~/.hypmesh/claude.token`, and make
  the file readable by you only.

## Watch it live

Open `http://127.0.0.1:8800/q/<id>`.

- **At the table** lists every seat that has touched the question: who it is, whose agent, its last action, its writes
  and refusals, and how long ago. A seat seen in the last two minutes shows green. The panel refreshes every three
  seconds.
- **Watch it live** opens the record's Live view on the investigation being worked: a lane per seat, the event
  stream newest first, work orders, results, claims and reviews as they land. The page opens on the most recently
  worked investigation; the record's own sidebar switches between attempts.
- **Standing** under the record shows each claim's mesh level. Seats of one researcher can take a claim to L1
  (checked); L2 needs another researcher, and L3 two organisations plus the instrument's reproduction.

## Data, backups, upgrades

```bash
docker exec hypmesh hypmesh backup --to /data/backups --keep 14     # consistent, verified snapshots in the volume
docker run --rm -v hypmesh-data:/data -v "$PWD":/out alpine tar czf /out/hypmesh-data.tgz -C /data .   # a copy out
docker build -t hypmesh . && docker rm -f hypmesh && docker run -d --name hypmesh … hypmesh   # upgrade, same volume
```

Seat presence is kept in memory, so the table is empty after a restart until the seats call again. The records and
the ledger are in the volume and survive it. For a public mesh, see [deploy.md](deploy.md): this container is the
mesh, and Caddy, TLS and the nightly backups go around it the same way.
