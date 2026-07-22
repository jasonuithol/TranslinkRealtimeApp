#!/usr/bin/env bash
#
# Run the board locally in a dev container on :8002, against the local data
# volume — with Melbourne realtime switched on if you pass your VIC key:
#
#   ./deploy/run-local.sh                 # static-only Melbourne
#   ./deploy/run-local.sh <VIC-API-KEY>   # live Melbourne (trains/trams/buses)
#
# The key is the "Data Platform API Token" from your profile at
# https://opendata.transport.vic.gov.au/ — sent as the KeyID header (verified;
# ignore the Ocp-Apim-Subscription-Key their OpenAPI specs claim).
set -euo pipefail

KEY="${1:-}"
NAME="${NAME:-tdev}"
PORT="${PORT:-8002}"
IMAGE="${IMAGE:-translink-dev}"
VIC="https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Building ${IMAGE} from ${HERE}…"
podman build -q -t "$IMAGE" "$HERE" >/dev/null

MEL_ENV=()
if [[ -n "$KEY" ]]; then
  MEL_ENV=(
    -e MEL_API_KEY="$KEY"
    -e MEL_API_KEY_HEADER=KeyID
    -e MEL_TRIP_UPDATES="2|${VIC}/metro/trip-updates;3|${VIC}/tram/trip-updates;4|${VIC}/bus/trip-updates"
    -e MEL_VEHICLE_POSITIONS="2|${VIC}/metro/vehicle-positions;3|${VIC}/tram/vehicle-positions;4|${VIC}/bus/vehicle-positions"
    -e MEL_ALERTS="2|${VIC}/metro/service-alerts;3|${VIC}/tram/service-alerts"
  )
  echo "==> Melbourne realtime: ON"
else
  echo "==> Melbourne realtime: off (no key given — static timetable + ghosts)"
fi

podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" -p "${PORT}:8000" -v translink-data:/data \
  "${MEL_ENV[@]}" "$IMAGE" >/dev/null
echo "==> Up: http://localhost:${PORT}  (Melbourne: ?region=mel)"

if [[ -n "$KEY" ]]; then
  echo "==> Waiting one poll cycle, then feed health…"
  sleep 40
  curl -s "http://localhost:${PORT}/api/feeds" | python3 -m json.tool || true
  echo
  echo "Healthy = a 'mel' section with trip_updates/vehicle counts. Then check"
  echo "http://localhost:${PORT}/?region=mel — rows should flip 📅→🛜 with live dots."
fi
