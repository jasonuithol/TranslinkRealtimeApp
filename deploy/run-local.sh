#!/usr/bin/env bash
#
# Run the board locally in a dev container on :8002, against the local data
# volume — with realtime switched on for any region whose key you pass:
#
#   ./deploy/run-local.sh                       # static-only mel/syd
#   ./deploy/run-local.sh <VIC-KEY>             # live Melbourne
#   ./deploy/run-local.sh <VIC-KEY> <NSW-KEY>   # live Melbourne + Sydney
#   ./deploy/run-local.sh "" <NSW-KEY>          # live Sydney only
#
# First time (or to refresh the TfNSW timetable), ingest Sydney on the way up:
#
#   INGEST_SYD=yes ./deploy/run-local.sh "" <NSW-KEY>
#
# (Runs between build and start, so the freshly built image does the ingest
# and the app boots onto the new timetable. A few minutes of downloads.)
#
# VIC key: the "Data Platform API Token" from your profile at
# https://opendata.transport.vic.gov.au/ — sent as the KeyID header (verified;
# ignore the Ocp-Apim-Subscription-Key their OpenAPI specs claim).
# NSW key: an application API key from https://opendata.transport.nsw.gov.au/
# — sent as `Authorization: apikey <key>`.
set -euo pipefail

KEY="${1:-}"
NSW_KEY="${2:-}"
NAME="${NAME:-tdev}"
PORT="${PORT:-8002}"
IMAGE="${IMAGE:-translink-dev}"
VIC="https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1"
NSW="https://api.transport.nsw.gov.au"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Building ${IMAGE} from ${HERE}…"
podman build -q -t "$IMAGE" "$HERE" >/dev/null

if [[ "${INGEST_SYD:-no}" == "yes" ]]; then
  if [[ -z "$NSW_KEY" ]]; then
    echo "INGEST_SYD=yes needs the NSW key as the second argument." >&2
    exit 1
  fi
  echo "==> Ingesting the Sydney timetable (per-mode TfNSW zips; a few minutes)…"
  podman run --rm -v translink-data:/data -e SYD_API_KEY="$NSW_KEY" \
    "$IMAGE" python ingest_gtfs.py --region syd
fi

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

SYD_ENV=()
if [[ -n "$NSW_KEY" ]]; then
  SYD_ENV=(
    -e SYD_API_KEY="$NSW_KEY"
    -e SYD_TRIP_UPDATES="t|${NSW}/v2/gtfs/realtime/sydneytrains;m|${NSW}/v2/gtfs/realtime/metro;b|${NSW}/v1/gtfs/realtime/buses;f|${NSW}/v1/gtfs/realtime/ferries/sydneyferries;lw|${NSW}/v2/gtfs/realtime/lightrail/innerwest;lc|${NSW}/v1/gtfs/realtime/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/realtime/lightrail/parramatta"
    -e SYD_VEHICLE_POSITIONS="t|${NSW}/v2/gtfs/vehiclepos/sydneytrains;m|${NSW}/v2/gtfs/vehiclepos/metro;b|${NSW}/v1/gtfs/vehiclepos/buses;f|${NSW}/v1/gtfs/vehiclepos/ferries/sydneyferries;lw|${NSW}/v2/gtfs/vehiclepos/lightrail/innerwest;lc|${NSW}/v1/gtfs/vehiclepos/lightrail/cbdandsoutheast;lp|${NSW}/v1/gtfs/vehiclepos/lightrail/parramatta"
    -e SYD_ALERTS="t|${NSW}/v2/gtfs/alerts/sydneytrains;m|${NSW}/v2/gtfs/alerts/metro;b|${NSW}/v2/gtfs/alerts/buses;f|${NSW}/v2/gtfs/alerts/ferries"
  )
  echo "==> Sydney realtime: ON"
else
  echo "==> Sydney realtime: off (no NSW key given — static timetable + ghosts)"
fi

podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" -p "${PORT}:8000" -v translink-data:/data \
  "${MEL_ENV[@]}" "${SYD_ENV[@]}" "$IMAGE" >/dev/null
echo "==> Up: http://localhost:${PORT}  (Melbourne: ?region=mel)"

if [[ -n "$KEY" ]]; then
  echo "==> Waiting one poll cycle, then feed health…"
  sleep 40
  curl -s "http://localhost:${PORT}/api/feeds" | python3 -m json.tool || true
  echo
  echo "Healthy = a 'mel' section with trip_updates/vehicle counts. Then check"
  echo "http://localhost:${PORT}/?region=mel — rows should flip 📅→🛜 with live dots."
fi
