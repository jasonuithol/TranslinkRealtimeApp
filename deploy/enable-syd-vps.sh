#!/usr/bin/env bash
#
# Switch ON Sydney realtime on an already-deployed VPS, from this machine:
#
#   ./deploy/enable-syd-vps.sh root@<vps-host> <NSW-API-KEY>
#
# The key is a free TfNSW Open Data application key from
# https://opendata.transport.nsw.gov.au/ — sent as `Authorization: apikey
# <key>` (paste just the token; the app adds the scheme).
#
# Rewrites the SYD_* Environment lines in the deploy user's quadlet
# (idempotent — safe to re-run with a new key), daemon-reloads, restarts,
# ingests the Sydney timetable if it has never been loaded, and verifies the
# syd feeds are actually flowing. Run deploy/probe-syd.sh locally FIRST — if
# TfNSW has moved an endpoint, fix the URL lines below before enabling.
set -euo pipefail

VPS="${1:-}"; KEY="${2:-}"
if [[ -z "$VPS" || -z "$KEY" ]]; then
  echo "Usage: $0 root@<vps-host> <NSW-API-KEY>" >&2
  exit 1
fi

NSW="https://api.transport.nsw.gov.au"

ssh "$VPS" DEPLOY_USER="${DEPLOY_USER:-deploy}" APP_PORT="${APP_PORT:-8000}" \
    IMAGE_REF="${IMAGE_REF:-ghcr.io/jasonuithol/translink-departures:latest}" \
    KEY="$KEY" NSW="$NSW" 'bash -s' <<'REMOTE'
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

echo "==> Writing SYD_* environment into $(basename "$QUADLET")…"
# Idempotent: strip any previous SYD_* lines, then insert fresh ones after the
# GTFS_DB Environment line.
sed -i '/^Environment=SYD_/d' "$QUADLET"
sed -i "/^Environment=GTFS_DB=/a\\
Environment=SYD_API_KEY=${KEY}\\
Environment=SYD_TRIP_UPDATES=t|${NSW}/v2/gtfs/realtime/sydneytrains;m|${NSW}/v2/gtfs/realtime/metro;b|${NSW}/v1/gtfs/realtime/buses;f|${NSW}/v1/gtfs/realtime/ferries/sydneyferries;lw|${NSW}/v2/gtfs/realtime/lightrail/innerwest;lc|${NSW}/v1/gtfs/realtime/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/realtime/lightrail/parramatta\\
Environment=SYD_VEHICLE_POSITIONS=t|${NSW}/v2/gtfs/vehiclepos/sydneytrains;m|${NSW}/v2/gtfs/vehiclepos/metro;b|${NSW}/v1/gtfs/vehiclepos/buses;f|${NSW}/v1/gtfs/vehiclepos/ferries/sydneyferries;lw|${NSW}/v2/gtfs/vehiclepos/lightrail/innerwest;lc|${NSW}/v1/gtfs/vehiclepos/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/vehiclepos/lightrail/parramatta\\
Environment=SYD_ALERTS=t|${NSW}/v2/gtfs/alerts/sydneytrains;m|${NSW}/v2/gtfs/alerts/metro;b|${NSW}/v2/gtfs/alerts/buses;f|${NSW}/v2/gtfs/alerts/ferries" "$QUADLET"
chown "$DEPLOY_USER:$DEPLOY_USER" "$QUADLET"
chmod 0600 "$QUADLET"   # it holds keys now

if ! as_deploy "podman run --rm -v translink-data:/data alpine test -f /data/gtfs-syd.sqlite3" 2>/dev/null; then
  echo "==> No Sydney timetable yet — ingesting (per-mode TfNSW zips; a few minutes)…"
  as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${KEY}' \
    '${IMAGE_REF}' python ingest_gtfs.py --region syd"
fi

echo "==> Reload + restart…"
as_deploy "systemctl --user daemon-reload && systemctl --user restart translink.service"

echo "==> Waiting for the board (up to 120 s)…"
up=0
for i in $(seq 1 40); do
  curl -fsS --max-time 3 "http://localhost:${APP_PORT}/api/config" >/dev/null 2>&1 && { up=1; break; }
  sleep 3
done
[[ $up -eq 1 ]] || { echo "Board did not come up; logs:"; as_deploy "podman logs --tail 30 translink"; exit 1; }

echo "==> Waiting one poll cycle for the Sydney feeds…"
sleep 40
FEEDS=$(curl -fsS "http://localhost:${APP_PORT}/api/feeds")
echo "$FEEDS" | python3 -m json.tool 2>/dev/null || echo "$FEEDS"
# Check SYDNEY'S OWN feed ages — a bare grep for "vehicles" matched the SEQ
# section and declared LIVE while every syd poll was failing.
if python3 -c '
import json, sys
syd = json.load(sys.stdin).get("syd", {})
ok = any(syd.get(k, {}).get("age_s") is not None
         for k in ("trip_updates", "vehicle_positions"))
sys.exit(0 if ok else 1)' <<<"$FEEDS"; then
  echo "══> Sydney realtime is LIVE."
else
  echo "══> WARNING: syd polls not succeeding — errors (if any) are in the"
  echo "    feed stats above; also: sudo -iu $DEPLOY_USER podman logs translink | grep 'poll:syd'"
  exit 1
fi
REMOTE
