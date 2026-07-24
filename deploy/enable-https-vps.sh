#!/usr/bin/env bash
#
# Put the board behind HTTPS, from this machine:
#
#   ./deploy/enable-https-vps.sh root@<vps-host> <domain>
#
# e.g.  ./deploy/enable-https-vps.sh root@delphi-bogart.bnr.la board.example.com
#
# PREREQUISITES (manual, one-off):
#   1. A DNS A record:  <domain>  ->  the VPS's public IP.
#      Wait for it to resolve before running this (dig +short <domain>).
#   2. Ports 80 and 443 reachable from the internet. If the provider has a
#      control-panel firewall (BinaryLane does), open them there too — this
#      script can only handle ufw on the host itself.
#
# What it does (idempotent — safe to re-run, e.g. with a new domain):
#   - installs Caddy (distro package, falling back to the official apt repo)
#   - writes a Caddyfile reverse-proxying <domain> -> localhost:8000
#   - opens 80/443 in ufw if ufw is active
#   - reloads Caddy, which obtains + auto-renews a Let's Encrypt certificate
#   - verifies https://<domain>/api/config from THIS machine (real cert check)
#
# Caddy terminates TLS as a host service; the app container is untouched.
# inventoryquest (port 8080) is unaffected — nothing here binds or proxies it.
set -euo pipefail

VPS="${1:-}"; DOMAIN="${2:-}"
APP_PORT="${APP_PORT:-8000}"
if [[ -z "$VPS" || -z "$DOMAIN" ]]; then
  echo "Usage: $0 root@<vps-host> <domain>" >&2
  exit 1
fi

echo "==> Checking DNS for ${DOMAIN} from here…"
RESOLVED="$(dig +short "$DOMAIN" A | tail -1)"
if [[ -z "$RESOLVED" ]]; then
  echo "FATAL: ${DOMAIN} does not resolve yet. Create the A record and wait" >&2
  echo "for propagation (dig +short ${DOMAIN}), then re-run." >&2
  exit 1
fi
echo "    ${DOMAIN} -> ${RESOLVED}"

ssh "$VPS" DOMAIN="$DOMAIN" APP_PORT="$APP_PORT" RESOLVED="$RESOLVED" 'bash -s' <<'REMOTE'
set -euo pipefail

# Sanity: the record should point at this box, or the ACME challenge will
# land somewhere else and issuance will fail.
if ! ip -o addr show | grep -qF "$RESOLVED"; then
  echo "WARNING: ${RESOLVED} is not an address on this host. If the DNS"
  echo "record points elsewhere, certificate issuance WILL fail."
fi

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  if ! apt-get install -y -qq caddy 2>/dev/null; then
    echo "==> Not in the distro repos — adding the official Caddy apt repo…"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
  fi
fi
echo "==> Caddy: $(caddy version)"

CADDYFILE=/etc/caddy/Caddyfile
MARKER="# managed by translink deploy/enable-https-vps.sh"
if [[ -f "$CADDYFILE" ]] && ! grep -qF "$MARKER" "$CADDYFILE"; then
  echo "==> Existing unmanaged Caddyfile — backing up to ${CADDYFILE}.bak"
  cp "$CADDYFILE" "${CADDYFILE}.bak"
fi
cat > "$CADDYFILE" <<EOF
${MARKER}
${DOMAIN} {
	reverse_proxy localhost:${APP_PORT}
}
EOF

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Opening 80/443 in ufw…"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

echo "==> Starting/reloading Caddy…"
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy
REMOTE

# Verify from OUTSIDE the VPS so the certificate chain is actually validated
# the way a browser will validate it. First issuance takes a few seconds.
echo "==> Waiting for a valid certificate + proxied board (up to 120 s)…"
up=0
for i in $(seq 1 40); do
  if curl -fsS --max-time 5 "https://${DOMAIN}/api/config" >/dev/null 2>&1; then
    up=1; echo "    up after ~$((i * 3))s"; break
  fi
  sleep 3
done

if [[ $up -eq 1 ]]; then
  echo "════════════════════════════════════════════════════════"
  echo "  HTTPS is LIVE:  https://${DOMAIN}/"
  echo "  Secure context => the 'near me' geolocation button works."
  echo "  Caddy renews the certificate automatically; nothing to cron."
  echo "════════════════════════════════════════════════════════"
else
  echo "HTTPS did not come up. Common causes:" >&2
  echo "  - port 80/443 blocked at the provider firewall (control panel)" >&2
  echo "  - DNS not yet propagated to Let's Encrypt's resolvers" >&2
  echo "Diagnose on the VPS:  journalctl -u caddy --since '-5 min'" >&2
  exit 1
fi
