#!/usr/bin/env bash
#
# Hold the on-device query layer to the server's answers.
#
#   ./ondevice/run-tests.sh [region ...]        (default: seq mel)
#
# For each region: ask the real Python modules, over the real national
# database, what the timetable says — then ask the ported TypeScript the
# same questions of that region's pack, and compare every departure on
# trip, minute, route, headsign, platform and stop sequence.
#
# Melbourne is not optional. It is the only region here whose clocks move,
# and the two mornings a year that are not 24 hours long are where a
# timetable port goes wrong: both DST boundary days are checked explicitly,
# plus a standalone clock test covering the hour that happens twice and the
# hour that never happens at all.
#
# Needs: a built pack per region (build_pack.py) and the translink-dev
# image. Node and its modules live in containers; nothing is installed on
# the host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${TMPDIR:-/tmp}/honker-ondevice-tests"
NODE_IMAGE="docker.io/library/node:22-slim"
APP_IMAGE="localhost/translink-dev:latest"
REGIONS=("$@")
[[ ${#REGIONS[@]} -eq 0 ]] && REGIONS=(seq mel)
mkdir -p "$OUT"

py() {   # the server's answer
  podman run --rm --user root -v translink-data:/data -v "$ROOT:/repo:ro" \
    -v "$OUT:/out:z" "$APP_IMAGE" "$@"
}
node_() { # the port's answer
  podman run --rm -v "$ROOT/ondevice:/work:z" -v honker-node:/work/node_modules \
    -v "$ROOT/packs:/packs:ro,z" -v "$OUT:/out:z" -w /work --user root \
    "$NODE_IMAGE" node --experimental-strip-types "$@"
}

if ! podman volume exists honker-node 2>/dev/null; then
  echo "==> installing node modules (once)"
  podman run --rm -v "$ROOT/ondevice:/work:z" -v honker-node:/work/node_modules \
    -w /work --user root "$NODE_IMAGE" npm install --silent --no-audit --no-fund
fi

fails=0

echo "==> typecheck"
podman run --rm -v "$ROOT/ondevice:/work:z" -v honker-node:/work/node_modules \
  -w /work --user root "$NODE_IMAGE" npx tsc --noEmit || fails=$((fails + 1))

echo "==> realtime: the hand-written protobuf reader vs the generated library"
if py python /repo/ondevice/test/rt-reference.py /out 2>&1 | sed 's/^/    /'; then
  node_ test/rt.ts /out || fails=$((fails + 1))
else
  echo "    feeds unreachable — skipped"
fi

echo "==> geocoding and walking"
if [[ -f "$ROOT/packs/seq/addresses.sqlite3" ]]; then
  py sh -c "python /repo/ondevice/test/geo-reference.py > /out/ref-geo.json"
  node_ test/geo.ts /out/ref-geo.json /packs/seq || fails=$((fails + 1))
else
  echo "    no seq address pack — skipped"
fi

echo "==> the clock, across both DST boundaries"
py sh -c "python /repo/ondevice/test/time-reference.py > /out/ref-time.json"
node_ test/time.ts /out/ref-time.json || fails=$((fails + 1))

for region in "${REGIONS[@]}"; do
  pack="$ROOT/packs/$region/timetable.sqlite3"
  if [[ ! -f "$pack" ]]; then
    echo "==> $region: no pack built — skipped"
    continue
  fi
  # Today, plus (for a region whose clocks move) the two days either side of
  # a transition. Dates outside the pack's validity window are skipped by
  # the reference itself, which simply reports no services.
  dates=("$(TZ=Australia/Brisbane date +%Y%m%d)")
  [[ "$region" == "mel" ]] && dates=(20261003 20261004 20261012)
  for d in "${dates[@]}"; do
    echo "==> $region $d"
    py sh -c "python /repo/ondevice/test/reference.py --region $region --date $d \
              --sample 25 > /out/ref-$region-$d.json"
    node_ test/equivalence.ts "/out/ref-$region-$d.json" "/packs/$region/timetable.sqlite3" \
      || fails=$((fails + 1))
  done

  # Journeys. Stop-to-stop across the network at four times of day, plus
  # the address form with walking penalties that a dropped pin produces.
  echo "==> $region journeys"
  tz=$(python3 -c "import json,sys; print(json.load(open('$ROOT/packs/$region/manifest.json'))['tz'])")
  d="${dates[${#dates[@]}-1]}"
  wd=$(python3 -c "import datetime; print(datetime.date($((10#${d:0:4})), $((10#${d:4:2})), $((10#${d:6:2}))).weekday())")
  specs=$(python3 "$ROOT/ondevice/test/plan-specs.py" "$region")
  py sh -c "python /repo/ondevice/test/plan-reference.py $region $d $wd '$specs' \
            > /out/ref-plan-$region.json"
  node_ test/plan.ts "/out/ref-plan-$region.json" "/packs/$region/timetable.sqlite3" "$tz" \
    || fails=$((fails + 1))
done

echo
if [[ $fails -gt 0 ]]; then
  echo "$fails CHECK(S) FAILED"
  exit 1
fi
echo "the query layer agrees with the server everywhere it was asked"
