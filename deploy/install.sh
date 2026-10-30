#!/usr/bin/env bash
# Install HypMesh on a fresh Ubuntu 24.04 machine, behind Caddy, as systemd services. Safe to run again (upgrades
# the package and refreshes the units; never overwrites /etc/hypmesh/hypmesh.env or the data).
#
#   sudo ./deploy/install.sh mesh.example.org                 # latest hypmesh from PyPI
#   sudo ./deploy/install.sh mesh.example.org 'hypmesh==0.1.0a2'
#   sudo ./deploy/install.sh mesh.example.org ./dist/hypmesh-0.1.0a2-py3-none-any.whl
#
# Before running: the domain's A/AAAA record points at this machine, and ports 80 and 443 are open (Caddy needs
# both for its certificate). See docs/deploy.md.
set -euo pipefail

DOMAIN="${1:-}"
SPEC="${2:-hypmesh}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "[hypmesh-install] $*" >&2; exit 1; }
say() { echo "[hypmesh-install] $*"; }

[[ -n "$DOMAIN" ]] || die "usage: sudo $0 <domain> [pip spec or wheel]"
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "not a domain name: $DOMAIN"
[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"
command -v apt-get >/dev/null || die "this script is for Ubuntu/Debian; see docs/deploy.md for other systems"

say "packages: python3-venv, caddy"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv caddy >/dev/null

say "user and directories"
if ! id hypmesh >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/hypmesh --no-create-home --shell /usr/sbin/nologin hypmesh
fi
install -d -o hypmesh -g hypmesh -m 0700 /var/lib/hypmesh /var/backups/hypmesh
install -d -o root -g hypmesh -m 0750 /etc/hypmesh
install -d -o root -g root -m 0755 /opt/hypmesh
install -d -o caddy -g caddy -m 0750 /var/log/caddy

say "package: $SPEC"
[[ -x /opt/hypmesh/venv/bin/python ]] || python3 -m venv /opt/hypmesh/venv
/opt/hypmesh/venv/bin/python -m pip install -q --upgrade pip
/opt/hypmesh/venv/bin/python -m pip install -q --upgrade --pre "$SPEC"
/opt/hypmesh/venv/bin/hypmesh --version

if [[ ! -f /etc/hypmesh/hypmesh.env ]]; then
  say "config: /etc/hypmesh/hypmesh.env (new; add the GitHub OAuth app there)"
  sed "s#https://mesh.example.org#https://${DOMAIN}#g" "$HERE/hypmesh.env.example" > /etc/hypmesh/hypmesh.env
else
  say "config: /etc/hypmesh/hypmesh.env kept as it is"
fi
chown root:hypmesh /etc/hypmesh/hypmesh.env
chmod 0640 /etc/hypmesh/hypmesh.env

say "caddy: /etc/caddy/Caddyfile for $DOMAIN"
NEW_CADDY="$(mktemp)"
sed "s#{\$HYPMESH_DOMAIN}#${DOMAIN}#" "$HERE/Caddyfile" > "$NEW_CADDY"
if [[ -f /etc/caddy/Caddyfile ]] && ! cmp -s "$NEW_CADDY" /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.before-hypmesh.$(date +%Y%m%d%H%M%S)"
fi
install -m 0644 "$NEW_CADDY" /etc/caddy/Caddyfile
rm -f "$NEW_CADDY"
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \n  || { caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; die "the Caddyfile does not validate"; }

say "systemd units"
install -m 0644 "$HERE/hypmesh.service" "$HERE/hypmesh-backup.service" "$HERE/hypmesh-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet hypmesh.service hypmesh-backup.timer
systemctl restart hypmesh.service
systemctl start hypmesh-backup.timer
systemctl reload-or-restart caddy.service

say "waiting for the mesh on 127.0.0.1:8800"
for _ in $(seq 1 30); do
  if /opt/hypmesh/venv/bin/python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=2).status == 200 else 1)" 2>/dev/null; then
    say "up: https://${DOMAIN}/healthz (the certificate can take a minute on first start)"
    cat <<EOF

Next:
  1. GitHub sign-in: create an OAuth app with callback URL https://${DOMAIN}/callback, then put its client id
     and secret in /etc/hypmesh/hypmesh.env and run: systemctl restart hypmesh
  2. Open a question as the curator:
     sudo -u hypmesh /opt/hypmesh/venv/bin/hypmesh --data /var/lib/hypmesh question open --hazard-reviewed \\
       --title "..." --objective "..."
  3. Check the first backup: systemctl start hypmesh-backup.service && journalctl -u hypmesh-backup -n 20
EOF
    exit 0
  fi
  sleep 1
done
journalctl -u hypmesh.service -n 40 --no-pager >&2 || true
die "the mesh did not answer on 127.0.0.1:8800"
