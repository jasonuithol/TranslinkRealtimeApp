#!/usr/bin/env bash
#
# Switch ON Melbourne realtime on an already-deployed VPS, from this machine:
#
#   ./deploy/enable-mel-vps.sh root@<vps-host> <VIC-API-KEY>
#
# Rewrites the MEL_* Environment lines in the deploy user's quadlet
# (idempotent — safe to re-run with a new key), daemon-reloads, restarts, and
# verifies the mel feeds are actually flowing.
set -euo pipefail

VPS="${1:-}"; KEY="${2:-}"
if [[ -z "$VPS" || -z "$KEY" ]]; then
  echo "Usage: $0 root@<vps-host> <VIC-API-KEY>" >&2
  exit 1
fi

VIC="https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1"

ssh "$VPS" DEPLOY_USER="${DEPLOY_USER:-deploy}" APP_PORT="${APP_PORT:-8000}" \
    KEY="$KEY" VIC="$VIC" 'bash -s' <<'REMOTE'
set -euo pipefail
DEPLOY_UID="$(id -u "$DEPLOY_USER")"
RUNTIME_DIR="/run/user/${DEPLOY_UID}"
QUADLET="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)/.config/containers/systemd/translink.container"

as_deploy() {
  sudo -u "$DEPLOY_USER" \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME_DIR}/bus" \
    bash -lc "cd \"\$HOME\" 2>/dev/null || cd /; $*"
}

[[ -f "$QUADLET" ]] || { echo "No quadlet at $QUADLET — run install-vps.sh first." >&2; exit 1; }

echo "==> Writing MEL_* environment into $(basename "$QUADLET")…"
# Idempotent: strip any previous MEL_* lines, then insert fresh ones after the
# GTFS_DB Environment line.
sed -i '/^Environment=MEL_/d' "$QUADLET"
sed -i "/^Environment=GTFS_DB=/a\\
Environment=MEL_API_KEY=${KEY}\\
Environment=MEL_API_KEY_HEADER=KeyID\\
Environment=MEL_TRIP_UPDATES=2|${VIC}/metro/trip-updates;3|${VIC}/tram/trip-updates;4|${VIC}/bus/trip-updates\\
Environment=MEL_VEHICLE_POSITIONS=2|${VIC}/metro/vehicle-positions;3|${VIC}/tram/vehicle-positions;4|${VIC}/bus/vehicle-positions\\
Environment=MEL_ALERTS=2|${VIC}/metro/service-alerts;3|${VIC}/tram/service-alerts" "$QUADLET"
chown "$DEPLOY_USER:$DEPLOY_USER" "$QUADLET"
chmod 0600 "$QUADLET"   # it holds the key now

echo "==> Reload + restart…"
as_deploy "systemctl --user daemon-reload && systemctl --user restart translink.service"

echo "==> Waiting for the board (up to 120 s)…"
up=0
for i in $(seq 1 40); do
  curl -fsS --max-time 3 "http://localhost:${APP_PORT}/api/config" >/dev/null 2>&1 && { up=1; break; }
  sleep 3
done
[[ $up -eq 1 ]] || { echo "Board did not come up; logs:"; as_deploy "podman logs --tail 30 translink"; exit 1; }

echo "==> Waiting one poll cycle for the Melbourne feeds…"
sleep 40
FEEDS=$(curl -fsS "http://localhost:${APP_PORT}/api/feeds")
echo "$FEEDS" | python3 -m json.tool 2>/dev/null || echo "$FEEDS"
if grep -q '"mel"' <<<"$FEEDS" && grep -q '"vehicles"' <<<"$FEEDS"; then
  echo "══> Melbourne realtime is LIVE."
else
  echo "══> WARNING: no mel feed stats yet — check: sudo -iu $DEPLOY_USER podman logs translink | grep 'poll:mel'"
  exit 1
fi
REMOTE
