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
# Rewrites the SYD_* AND NSW_* Environment lines in the deploy user's quadlet
# (idempotent — safe to re-run with a new key; both regions use the same
# TfNSW key), daemon-reloads, restarts, ingests the Sydney and NSW TrainLink
# timetables if they have never been loaded, and verifies the syd feeds are
# actually flowing. Run deploy/probe-syd.sh locally FIRST — if TfNSW has
# moved an endpoint, fix the URL lines below before enabling.
set -euo pipefail

# Args win; gaps fill from ~/.config/translink/keys.env (NSW_KEY, VPS_HOST).
source "$(dirname "${BASH_SOURCE[0]}")/load-keys.sh"
VPS="${1:-${VPS_HOST:-}}"; KEY="${2:-${NSW_KEY:-}}"
if [[ -z "$VPS" || -z "$KEY" ]]; then
  echo "Usage: $0 root@<vps-host> <NSW-API-KEY>" >&2
  echo "  (or set NSW_KEY / VPS_HOST in ~/.config/translink/keys.env)" >&2
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
# Probe the primary interface, not localhost — pasta stopped forwarding
# published ports to 127.0.0.1 on the host (2026-07-29).
PROBE_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
PROBE_HOST="${PROBE_HOST:-localhost}"

as_deploy() {
  sudo -u "$DEPLOY_USER" \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME_DIR}/bus" \
    bash -lc "cd \"\$HOME\" 2>/dev/null || cd /; $*"
}

[[ -f "$QUADLET" ]] || { echo "No quadlet at $QUADLET — run install-vps.sh first." >&2; exit 1; }

echo "==> Writing SYD_* + NSW_* environment into $(basename "$QUADLET")…"
# Idempotent: strip any previous SYD_*/NSW_* lines, then insert fresh ones
# after the GTFS_DB Environment line. NSW_* is the TrainLink interstate
# region (`nsw`) — same key, one nswtrains feed of each kind.
sed -i '/^Environment=SYD_/d; /^Environment=NSW_/d' "$QUADLET"
sed -i "/^Environment=GTFS_DB=/a\\
Environment=SYD_API_KEY=${KEY}\\
Environment=SYD_TRIP_UPDATES=t|${NSW}/v2/gtfs/realtime/sydneytrains;m|${NSW}/v2/gtfs/realtime/metro;b|${NSW}/v1/gtfs/realtime/buses;f|${NSW}/v1/gtfs/realtime/ferries/sydneyferries;lw|${NSW}/v2/gtfs/realtime/lightrail/innerwest;lc|${NSW}/v1/gtfs/realtime/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/realtime/lightrail/parramatta\\
Environment=SYD_VEHICLE_POSITIONS=t|${NSW}/v2/gtfs/vehiclepos/sydneytrains;m|${NSW}/v2/gtfs/vehiclepos/metro;b|${NSW}/v1/gtfs/vehiclepos/buses;f|${NSW}/v1/gtfs/vehiclepos/ferries/sydneyferries;lw|${NSW}/v2/gtfs/vehiclepos/lightrail/innerwest;lc|${NSW}/v1/gtfs/vehiclepos/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/vehiclepos/lightrail/parramatta\\
Environment=SYD_ALERTS=t|${NSW}/v2/gtfs/alerts/sydneytrains;m|${NSW}/v2/gtfs/alerts/metro;b|${NSW}/v2/gtfs/alerts/buses;f|${NSW}/v2/gtfs/alerts/ferries\\
Environment=NSW_TRIP_UPDATES=nt|${NSW}/v1/gtfs/realtime/nswtrains\\
Environment=NSW_VEHICLE_POSITIONS=nt|${NSW}/v1/gtfs/vehiclepos/nswtrains\\
Environment=NSW_ALERTS=nt|${NSW}/v2/gtfs/alerts/nswtrains" "$QUADLET"
chown "$DEPLOY_USER:$DEPLOY_USER" "$QUADLET"
chmod 0600 "$QUADLET"   # it holds keys now

# The weekly refresh (translink-ingest.container, `--region all`) needs the
# same key or it will skip syd and nsw every Sunday.
INGEST_QUADLET="$(dirname "$QUADLET")/translink-ingest.container"
if [[ -f "$INGEST_QUADLET" ]]; then
  echo "==> Writing SYD_API_KEY into $(basename "$INGEST_QUADLET")…"
  sed -i '/^Environment=SYD_API_KEY=/d' "$INGEST_QUADLET"
  sed -i "/^Environment=GTFS_DB=/a\\
Environment=SYD_API_KEY=${KEY}" "$INGEST_QUADLET"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$INGEST_QUADLET"
  chmod 0600 "$INGEST_QUADLET"
else
  echo "==> WARNING: no $INGEST_QUADLET — the weekly refresh will skip syd/nsw."
fi

if ! as_deploy "podman run --rm -v translink-data:/data alpine test -f /data/gtfs-syd.sqlite3" 2>/dev/null; then
  echo "==> No Sydney timetable yet — ingesting (per-mode TfNSW zips; a few minutes)…"
  as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${KEY}' \
    '${IMAGE_REF}' python ingest_gtfs.py --region syd"
fi

if ! as_deploy "podman run --rm -v translink-data:/data alpine test -f /data/gtfs-nsw.sqlite3" 2>/dev/null; then
  echo "==> No NSW TrainLink timetable yet — ingesting (one TfNSW zip)…"
  as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${KEY}' \
    '${IMAGE_REF}' python ingest_gtfs.py --region nsw"
fi

echo "==> Reload + restart…"
as_deploy "systemctl --user daemon-reload && systemctl --user restart translink.service"

echo "==> Waiting for the board (up to 600 s — nine cold regions warm at boot)…"
up=0
for i in $(seq 1 200); do
  curl -fsS --max-time 3 "http://${PROBE_HOST}:${APP_PORT}/api/config" >/dev/null 2>&1 && { up=1; break; }
  sleep 3
done
[[ $up -eq 1 ]] || { echo "Board did not come up; logs:"; as_deploy "podman logs --tail 30 translink"; exit 1; }

echo "==> Waiting one poll cycle for the Sydney feeds…"
sleep 40
FEEDS=$(curl -fsS "http://${PROBE_HOST}:${APP_PORT}/api/feeds")
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
  # TrainLink is one small feed — report but don't fail on it (overnight the
  # network can be near-empty).
  if python3 -c '
import json, sys
nsw = json.load(sys.stdin).get("nsw", {})
ok = any(nsw.get(k, {}).get("age_s") is not None
         for k in ("trip_updates", "vehicle_positions"))
sys.exit(0 if ok else 1)' <<<"$FEEDS"; then
    echo "══> NSW TrainLink realtime is LIVE."
  else
    echo "══> NOTE: nsw (TrainLink) polls not confirmed yet — check /api/feeds"
    echo "    and: sudo -iu $DEPLOY_USER podman logs translink | grep 'poll:nsw'"
  fi
else
  echo "══> WARNING: syd polls not succeeding — errors (if any) are in the"
  echo "    feed stats above; also: sudo -iu $DEPLOY_USER podman logs translink | grep 'poll:syd'"
  exit 1
fi
REMOTE

# Remote script succeeded (set -e): record it in the deployment state.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${HERE}/mark-deployed.sh" "${TARGET_NAME:-vps}" enable-syd-nsw || true
