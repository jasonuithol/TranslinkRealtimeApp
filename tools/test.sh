#!/usr/bin/env bash
#
# The test suite. Everything CI used to run, run here instead.
#
#   ./tools/test.sh            build the image from HEAD, then test it
#   ./tools/test.sh --dirty    test the working tree instead of HEAD
#   IMAGE=x ./tools/test.sh    test an image that already exists
#
# There is no unit-test suite in this repo, so this IS the suite: an
# end-to-end run against mock_gtfs.zip — ingest, re-ingest, serve, query —
# plus one regression per bug that actually shipped once.
#
# It builds from `git archive HEAD` by default, not from the working tree.
# That was CI's quietly valuable property: a clean checkout cannot pass
# because of a file you forgot to commit. --dirty exists for the loop where
# you are still editing.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-}"
VOLUME="test-data-$$"
CONTAINER="test-board-$$"
DIRTY=no
[[ "${1:-}" == "--dirty" ]] && DIRTY=yes

cleanup() {
  podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
  podman volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fails=0
ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; fails=$((fails + 1)); }
step() { echo "==> $1"; }

# --- build ------------------------------------------------------------------
if [[ -z "$IMAGE" ]]; then
  IMAGE="translink-test:$$"
  if [[ "$DIRTY" == yes ]]; then
    step "building from the working tree (--dirty)"
    podman build -q -t "$IMAGE" "$ROOT" >/dev/null
  else
    step "building from HEAD ($(git -C "$ROOT" rev-parse --short HEAD))"
    # A clean checkout, so an uncommitted file cannot make this pass.
    tmp="$(mktemp -d)"
    git -C "$ROOT" archive HEAD | tar -x -C "$tmp"
    podman build -q -t "$IMAGE" "$tmp" >/dev/null
    rm -rf "$tmp"
  fi
  trap 'cleanup; podman rmi -f "$IMAGE" >/dev/null 2>&1 || true' EXIT
fi

step "syntax"
# PYTHONPYCACHEPREFIX because the app directory is not writable in the
# image, and compileall wants somewhere to put the bytecode it produces.
podman run --rm -e PYTHONPYCACHEPREFIX=/tmp/pyc "$IMAGE" \
  python -m compileall -q . >/dev/null && ok "every module compiles" \
  || bad "a module does not compile"

# --- ingest -----------------------------------------------------------------
step "ingest the mock feed"
podman volume create "$VOLUME" >/dev/null
podman run --rm -v "$VOLUME:/data" -v "$ROOT/mock_gtfs.zip:/mock.zip:ro,z" "$IMAGE" \
  python ingest_gtfs.py /mock.zip >/dev/null && ok "ingested" || bad "ingest failed"
# Again, over the top of the first: the swap must survive an existing DB.
podman run --rm -v "$VOLUME:/data" -v "$ROOT/mock_gtfs.zip:/mock.zip:ro,z" "$IMAGE" \
  python ingest_gtfs.py /mock.zip >/dev/null && ok "re-ingest swaps atomically" \
  || bad "re-ingest failed over an existing database"

# --- serve ------------------------------------------------------------------
step "serve and query"
podman run -d --name "$CONTAINER" -p 18000:8000 -v "$VOLUME:/data" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null http://localhost:18000/ 2>/dev/null && break
  sleep 1
done
api() { curl -fsS --max-time 20 "http://localhost:18000$1"; }
api /               >/dev/null && ok "the board serves"        || bad "the board does not serve"
api /api/regions    >/dev/null && ok "regions list"            || bad "regions list"
stop=$(api /api/all-stops | python3 -c "
import json, sys
s = json.load(sys.stdin)['stops']
print(s[0]['stop_id'] if s else '')")
[[ -n "$stop" ]] && ok "all-stops returns stops" || bad "all-stops is empty"
[[ -n "$stop" ]] && { api "/api/departures/$stop" >/dev/null \
  && ok "departures for $stop" || bad "departures for $stop"; }

# --- the regressions --------------------------------------------------------
# One per bug that actually shipped. Each names what went wrong, because a
# test whose failure you cannot interpret gets deleted the first time it is
# inconvenient.
step "regressions"

podman run --rm -e TZ=UTC -v "$VOLUME:/data" "$IMAGE" python -c "
import datetime, zoneinfo, gtfsdb
bne = zoneinfo.ZoneInfo('Australia/Brisbane')
day = datetime.datetime(2026, 7, 21, 12, 0, tzinfo=bne)
got = datetime.datetime.fromtimestamp(gtfsdb.gtfs_time_to_epoch('10:16:00', day), bne)
assert (got.hour, got.minute) == (10, 16), got
got = datetime.datetime.fromtimestamp(gtfsdb.gtfs_time_to_epoch('24:28:00', day), bne)
assert (got.day, got.hour, got.minute) == (22, 0, 28), got
" >/dev/null 2>&1 \
  && ok "scheduled times resolve in the network's zone, not the host's" \
  || bad "scheduled times resolve in the network's zone, not the host's"

podman run --rm -v "$VOLUME:/data" "$IMAGE" python -c "
import gtfsdb
nodes = [(1000, -27.0, 153.0), (2000, -27.0, 153.1)]
mid = gtfsdb._interpolate_along(nodes, 1500)
assert abs(mid['lon'] - 153.05) < 1e-9, mid
assert gtfsdb._interpolate_along(nodes, 1000) == {'lat': -27.0, 'lon': 153.0}
assert gtfsdb._interpolate_along(nodes, 500) is None
assert gtfsdb._interpolate_along(nodes, 500, 600) == {'lat': -27.0, 'lon': 153.0}
assert gtfsdb._interpolate_along(nodes, 300, 600) is None
assert gtfsdb._interpolate_along(nodes, 3000) is None
assert gtfsdb._seconds_into_day('24:28:00') == 88080
" >/dev/null 2>&1 \
  && ok "ghost positions interpolate, and do not park phantom buses" \
  || bad "ghost positions interpolate, and do not park phantom buses"

# PTV publishes Google's extended route types; the app speaks basic GTFS.
podman run --rm -v "$VOLUME:/data" "$IMAGE" python -c "
from ingest_gtfs import normalize_route_type as n
assert n('400') == '1' and n('701') == '3' and n('900') == '0'
assert n('101') == '2' and n('1000') == '4'
assert n('2') == '2' and n('0') == '0' and n('') == '' and n(None) is None
" >/dev/null 2>&1 \
  && ok "extended route types normalise to basic GTFS" \
  || bad "extended route types normalise to basic GTFS"

[[ -n "$stop" ]] && {
  dupes=$(api "/api/departures/$stop" | python3 -c "
import json,sys
d = json.load(sys.stdin)['departures']
keys = [(x['trip_id'], x['stop_id']) for x in d]
print(len(keys) - len(set(keys)))")
  [[ "$dupes" == 0 ]] && ok "no duplicated trips on the board" \
    || bad "no duplicated trips on the board ($dupes duplicates)"
}

# The page must not reach for anything it does not host itself.
# `|| true` because a clean result means grep matched nothing and exits 1,
# which under `set -e` would end the run here — silently passing the last
# two checks by never reaching them.
external=$(grep -roE 'https?://[a-z0-9.-]+' "$ROOT/static/index.html" "$ROOT/static/app.css" \
           "$ROOT/static/js" 2>/dev/null \
           | grep -vE 'localhost|127\.0\.0\.1|nextarrival\.app|github\.com|www\.w3\.org|openstreetmap\.org' \
           | sed 's/.*://' | sort -u | head -5 || true)
[[ -z "$external" ]] && ok "frontend assets are self-hosted" \
  || bad "frontend reaches offsite: $(tr '\n' ' ' <<<"$external")"

style_layers=$(python3 -c "
import json
s = json.load(open('$ROOT/static/map-style.json'))
srcs = {l.get('source-layer') for l in s['layers'] if l.get('source-layer')}
print(len(srcs))" 2>/dev/null || echo 0)
[[ "${style_layers:-0}" -gt 0 ]] && ok "map style references real vector layers" \
  || bad "map style references real vector layers"

echo
if [[ $fails -gt 0 ]]; then
  echo "$fails CHECK(S) FAILED"
  exit 1
fi
echo "all checks passed"
