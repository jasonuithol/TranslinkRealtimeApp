#!/usr/bin/env bash
#
# Build the map basemap: a Protomaps .pmtiles extract covering South East
# Queensland, written next to the timetable on the data volume.
#
#   podman run --rm -v translink-data:/data translink-departures ./fetch_basemap.sh
#
# Protomaps publishes a daily planet build (~120 GB); `pmtiles extract` pulls
# only the tiles inside the bounding box over HTTP range requests, so this
# transfers ~24 MB rather than the planet. The basemap changes far more slowly
# than the timetable — refresh it occasionally, not weekly.
#
# Protomaps asks that you not hot-link their builds in production; this
# downloads once to your own volume, which is what they recommend.
set -euo pipefail

BASEMAP_DIR="${BASEMAP_DIR:-/data/basemap}"
# SEQ: Gympie/Noosa in the north to the NSW border, Toowoomba in the west.
BBOX="${BBOX:-151.8,-28.3,153.6,-26.0}"
# z13 gives street-level detail at 22 MB. Each extra zoom roughly doubles it.
MAXZOOM="${MAXZOOM:-13}"
BUILD="${BUILD:-}"

# Default to the most recent daily build that exists (today's may not be up).
if [[ -z "$BUILD" ]]; then
  for i in 1 2 3 4 5 6 7; do
    D=$(date -u -d "-${i} day" +%Y%m%d)
    if curl -sSf -o /dev/null -I "https://build.protomaps.com/${D}.pmtiles" 2>/dev/null; then
      BUILD="$D"; break
    fi
  done
fi
if [[ -z "$BUILD" ]]; then
  echo "Could not find a recent Protomaps build; set BUILD=YYYYMMDD." >&2
  exit 1
fi

mkdir -p "$BASEMAP_DIR"
TMP="${BASEMAP_DIR}/seq.pmtiles.tmp"
rm -f "$TMP"

echo "Extracting SEQ from build ${BUILD} (bbox ${BBOX}, maxzoom ${MAXZOOM})…"
pmtiles extract "https://build.protomaps.com/${BUILD}.pmtiles" "$TMP" \
  --bbox="$BBOX" --maxzoom="$MAXZOOM"

# Same atomic swap as the timetable: a running server never sees a partial file.
mv -f "$TMP" "${BASEMAP_DIR}/seq.pmtiles"
echo "Done. $(du -h "${BASEMAP_DIR}/seq.pmtiles" | cut -f1) at ${BASEMAP_DIR}/seq.pmtiles"
