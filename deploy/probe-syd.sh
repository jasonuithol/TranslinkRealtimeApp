#!/usr/bin/env bash
#
# Probe every TfNSW endpoint the Sydney region uses, with your key:
#
#   ./deploy/probe-syd.sh <NSW-API-KEY>
#
# The key is free: register at https://opendata.transport.nsw.gov.au/, create
# an application, copy its API key. Auth is `Authorization: apikey <key>`.
#
# TfNSW split their feeds across /v1 and /v2 and have moved modes between them
# before — this prints an HTTP status (and size / entity count) per endpoint
# so a moved one is obvious. Fix any failures by editing the URL in
# ingest_gtfs.py (static) or the SYD_* env lines (realtime) — nothing else
# hardcodes them.
set -euo pipefail

KEY="${1:-}"
[[ -n "$KEY" ]] || { echo "Usage: $0 <NSW-API-KEY>" >&2; exit 1; }
BASE="https://api.transport.nsw.gov.au"
AUTH="Authorization: apikey $KEY"

# Static schedule zips (HEAD-ish probe: first byte only, we just want the 200).
# Trains: schedule on v1 even though its realtime is v2 (probed 2026-07-23).
STATIC=(
  "t|$BASE/v1/gtfs/schedule/sydneytrains"
  "m|$BASE/v2/gtfs/schedule/metro"
  "b|$BASE/v1/gtfs/schedule/buses"
  "f|$BASE/v1/gtfs/schedule/ferries/sydneyferries"
  "lw|$BASE/v1/gtfs/schedule/lightrail/innerwest"
  "lc|$BASE/v1/gtfs/schedule/lightrail/cbdandsoutheast"
  "lp|$BASE/v1/gtfs/schedule/lightrail/parramatta"
)
# Realtime protobufs (fetched whole; entity count printed via protoc-less grep
# is meaningless, so size stands in — an empty feed is a few hundred bytes).
REALTIME=(
  "TU t|$BASE/v2/gtfs/realtime/sydneytrains"
  "TU m|$BASE/v2/gtfs/realtime/metro"
  "TU b|$BASE/v1/gtfs/realtime/buses"
  "TU f|$BASE/v1/gtfs/realtime/ferries/sydneyferries"
  "TU lw|$BASE/v2/gtfs/realtime/lightrail/innerwest"
  "TU lc|$BASE/v1/gtfs/realtime/lightrail/cbdandsoutheast"
  "TU lp|$BASE/v1/gtfs/realtime/lightrail/parramatta"
  "VP t|$BASE/v2/gtfs/vehiclepos/sydneytrains"
  "VP m|$BASE/v2/gtfs/vehiclepos/metro"
  "VP b|$BASE/v1/gtfs/vehiclepos/buses"
  "VP f|$BASE/v1/gtfs/vehiclepos/ferries/sydneyferries"
  "VP lw|$BASE/v2/gtfs/vehiclepos/lightrail/innerwest"
  "VP lc|$BASE/v1/gtfs/vehiclepos/lightrail/cbdandsoutheast"
  "VP lp|$BASE/v1/gtfs/vehiclepos/lightrail/parramatta"
  "AL t|$BASE/v2/gtfs/alerts/sydneytrains"
  "AL m|$BASE/v2/gtfs/alerts/metro"
  "AL b|$BASE/v2/gtfs/alerts/buses"
  "AL f|$BASE/v2/gtfs/alerts/ferries"
)

echo "== static schedule =="
for entry in "${STATIC[@]}"; do
  label="${entry%%|*}"; url="${entry#*|}"
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
              -H "$AUTH" -r 0-0 "$url" || echo "ERR")
  printf "  %-4s %-3s %s\n" "$label" "$code" "$url"
done

echo "== realtime =="
# Protobuf is binary (bash drops its NUL bytes), so download to a scratch
# file and let curl report the size instead of capturing the body.
SCRATCH="$(mktemp)"
trap 'rm -f "$SCRATCH"' EXIT
for entry in "${REALTIME[@]}"; do
  label="${entry%%|*}"; url="${entry#*|}"
  read -r code size < <(curl -s --max-time 30 -H "$AUTH" -o "$SCRATCH" \
    -w "%{http_code} %{size_download}" "$url" || echo "ERR 0")
  printf "  %-6s %-3s %8sB  %s\n" "$label" "$code" "$size" "$url"
done

echo
echo "All 200s? Then: SYD_API_KEY=$KEY python ingest_gtfs.py --region syd"
echo "and ./deploy/run-local.sh with the key to see it live."
