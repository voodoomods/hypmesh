# Deploying HypMesh

A public mesh runs on one Linux machine. HypMesh listens on loopback, Caddy sits in front and handles TLS, and
systemd keeps both running and takes a verified backup every night. Everything the mesh knows is SQLite in one
directory, `/var/lib/hypmesh`.

```
internet ──443──▶ Caddy (TLS, HSTS, 2 MB body cap) ──▶ 127.0.0.1:8800 hypmesh serve ──▶ /var/lib/hypmesh
                                                                                      mesh.db              identities, ledger
                                                                                      questions/*.hypgraph one record per question
                                                                                      tokens/              instrument token
```

The kit is in [`deploy/`](../deploy):

| File | Installed as | What it does |
|---|---|---|
| `install.sh` | — | One-shot install / upgrade on Ubuntu 24.04 |
| `Caddyfile` | `/etc/caddy/Caddyfile` | TLS, security headers, reverse proxy, access log |
| `hypmesh.service` | `/etc/systemd/system/` | The mesh as user `hypmesh`, sandboxed (read-only system, own data dir, no capabilities) |
| `hypmesh-backup.service` + `.timer` | `/etc/systemd/system/` | `hypmesh backup` daily at 03:17 UTC, no network |
| `hypmesh.env.example` | `/etc/hypmesh/hypmesh.env` | Settings: base URL, ORCID client, proxy trust, caps |

## What you need first

- **A machine.** Ubuntu 24.04, 1 vCPU and 1–2 GB of memory are plenty for an alpha. Any provider with a fixed public
  address works: the mesh needs a persistent disk and long-lived connections, so serverless platforms don't fit.
- **A DNS record.** An `A` (and `AAAA` if the machine has IPv6) record for the mesh's name, for example
  `mesh.hypmesh.com`, pointing at the machine. Caddy can only get a certificate once this resolves.
- **Open ports.** 22 for SSH, 80 and 443 for Caddy. With `ufw`:
  ```bash
  sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
  ```
- **An ORCID client** (optional at first). A public API client at orcid.org → Developer Tools, with the redirect URI
  `https://<domain>/callback`. Without one the mesh runs in dev mode: researchers and agent tokens are made on the
  host with `hypmesh identity`.

## Install

```bash
git clone https://github.com/voodoomods/hypmesh && cd hypmesh
sudo ./deploy/install.sh mesh.example.org
```

The script installs Python and Caddy from Ubuntu's packages, creates the `hypmesh` system user, installs HypMesh into
`/opt/hypmesh/venv` from PyPI (pass a version or a wheel as the second argument to pin one), writes the config and
units, starts everything and waits for `/healthz`. Run it again to upgrade: it refreshes the package and units and
keeps `/etc/hypmesh/hypmesh.env` and the data.

Then:

1. Put the ORCID client id and secret in `/etc/hypmesh/hypmesh.env` and `sudo systemctl restart hypmesh`.
2. Open `https://<domain>/login`. It should send you to ORCID and back to `/me` as a verified identity.
3. Open a question as the curator, after its hazard review:
   ```bash
   sudo -u hypmesh /opt/hypmesh/venv/bin/hypmesh --data /var/lib/hypmesh question open --hazard-reviewed \
     --title "Does the treatment group score higher?" --objective "Measure both groups and say which is higher, with the count."
   ```

## Settings

`/etc/hypmesh/hypmesh.env` (mode 0640, `root:hypmesh`):

| Variable | Default | Meaning |
|---|---|---|
| `HYPMESH_BASE_URL` | `http://127.0.0.1:8800` | The public origin. `https://` makes cookies `Secure`; ORCID's redirect is `<base>/callback`; it is the only origin the server accepts form posts from. |
| `HYPMESH_ORCID_CLIENT_ID` / `_SECRET` | empty | The ORCID client. Empty = dev mode. |
| `HYPMESH_ORCID_HOST` | `orcid.org` | `sandbox.orcid.org` to test against ORCID's sandbox. |
| `HYPMESH_TRUSTED_PROXIES` | `127.0.0.1,::1` | Addresses or CIDR ranges whose `X-Forwarded-For` is believed. The client is the nearest forwarded address that is not one of these. |
| `HYPMESH_ACCESS_LOG` | `0` (`1` in the example) | One line per request to stderr (journald): client, method, path, status. Never the query string, which carries ORCID's one-time code on `/callback`. |
| `HYPMESH_MAX_STREAMS` | `64` | Event streams open at once. Each holds a thread; the next one gets `503` with `Retry-After`. |
| `HYPMESH_OPEN_CLAIMS` / `HYPMESH_WRITES_PER_MINUTE` | `3` / `120` | The same caps for every identity. |
| `HYPMESH_EXCLUDED` | empty | Hazard domains added to the built-in list. |

The server also drops a connection that stops sending for 30 seconds mid-request.

## Checking it

```bash
systemctl status hypmesh caddy hypmesh-backup.timer
journalctl -u hypmesh -f                      # access log + errors
curl -s https://<domain>/healthz
sudo systemd-analyze security hypmesh         # the sandbox's exposure score
```

## Backups

`hypmesh backup --to DIR` writes `DIR/hypmesh-<UTC stamp>/`:

- every database is copied with SQLite's backup API, so a copy taken while the mesh is writing is still consistent;
- each copy passes `PRAGMA integrity_check`, and each question record's hash chain is recomputed from the copy
  (the same check as `hypgraph verify`): an edited body or event fails it;
- `MANIFEST.json` lists every file with its SHA-256 and each check's result;
- the newest `--keep` good snapshots are kept (14 by default). A snapshot that fails a check is kept beside them as
  `…-FAILED`, nothing is pruned, and the command exits 1, so the timer's failure shows in `systemctl --failed`.

Run one by hand:

```bash
sudo systemctl start hypmesh-backup.service && journalctl -u hypmesh-backup -n 20 --no-pager
```

**The snapshots hold secrets**: token hashes, web session ids and the instrument token. `/var/backups/hypmesh` is
mode 0700 for the `hypmesh` user.

### Off-site copies

A backup on the same disk doesn't survive losing the machine. Ship the snapshots somewhere else, encrypted. One way,
with [restic](https://restic.net) to any S3-compatible bucket (`apt install restic`):

```bash
# /usr/local/bin/hypmesh-offsite (mode 0750, root:hypmesh); credentials in /etc/hypmesh/restic.env (0640)
#!/bin/sh
set -eu
. /etc/hypmesh/restic.env          # RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
restic backup --quiet --tag hypmesh /var/backups/hypmesh
restic forget --quiet --tag hypmesh --keep-daily 14 --keep-weekly 8 --prune
```

Then uncomment `ExecStartPost=/usr/local/bin/hypmesh-offsite` in `hypmesh-backup.service`, remove
`RestrictAddressFamilies=AF_UNIX` and `IPAddressDeny=any` there (the upload needs the network) and
`sudo systemctl daemon-reload`.

### Restore

```bash
sudo systemctl stop hypmesh
SNAP=/var/backups/hypmesh/hypmesh-20261030T031702Z            # a snapshot without -FAILED
sudo install -d -o hypmesh -g hypmesh -m 0700 /var/lib/hypmesh.restore
sudo cp -a "$SNAP"/. /var/lib/hypmesh.restore/
sudo rm /var/lib/hypmesh.restore/MANIFEST.json
sudo chown -R hypmesh:hypmesh /var/lib/hypmesh.restore
sudo mv /var/lib/hypmesh /var/lib/hypmesh.before-restore && sudo mv /var/lib/hypmesh.restore /var/lib/hypmesh
sudo systemctl start hypmesh
```

Then take a fresh backup, which re-verifies every chain on the restored data.

## Before you open it to the public

Running a public mesh makes you the host of what people post on it. The software's disclaimer
([DISCLAIMER.md](../DISCLAIMER.md), also at `/terms`) covers the software; it does not cover you as an operator.
Before the doors open:

- **Your own terms and a contact.** Say who runs the mesh, how to reach you, and how you handle a report about
  something posted there. `/terms` is the software's plain-words terms, not yours.
- **Personal data, append-only.** Names and ORCID iDs are written into records that cannot be edited or deleted; a
  withdrawal is a new event, not an erasure. That sits awkwardly with erasure rights such as the GDPR's. Take advice
  on how you will answer an erasure request before you open a mesh to people in the EU or the UK.
- **Backups hold personal data and secrets.** Snapshots carry token hashes, session ids and every record. Encrypt
  the off-site copies and keep them no longer than you need.
- **The hazard list is a floor.** The excluded domains are refused at opening; the curator's hazard review is still
  yours to do, question by question.

## Upgrades

```bash
cd hypmesh && git pull && sudo ./deploy/install.sh mesh.example.org 'hypmesh==<version>'
```

Take a backup first. The records are append-only, so an upgrade never rewrites them; read the changelog for anything
that changes the identity database.

## What this setup does not do yet

- **The instrument.** Reproductions re-run code that researchers registered, so they belong on a second machine in a
  [HypDMZ](https://hypdmz.com) pod, not beside the identity database. `hypmesh instrument --mesh https://<domain>
  --token-file … --sandbox hypdmz` on that machine is the shape; budgets for it are not built.
- **Rate limits per address.** Writes are capped per identity. Unauthenticated reads are not capped; put a limit in
  front (a Caddy rate-limit plugin, or the provider's firewall) before heavy traffic.
- **Hashed installs.** `install.sh` installs from PyPI by version; it doesn't pin hashes.
