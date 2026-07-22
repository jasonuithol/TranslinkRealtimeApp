#!/usr/bin/env bash
#
# Build the SEQ vector basemap with Planetiler (OpenMapTiles schema) as pmtiles,
# written to the app's data volume. Run from the builder image:
#
#   podman build -f basemap/Containerfile -t translink-basemap .
#   podman run --rm \
#     -v translink-data:/data \
#     -v translink-basemap-cache:/cache \
#     translink-basemap
#
# The 910 MB Australia OSM extract, Natural Earth and the water polygons are
# downloaded once to the /cache volume and reused on later rebuilds. The basemap
# changes slowly — rebuild occasionally, not weekly like the timetable.
set -euo pipefail

OUT_DIR="${BASEMAP_DIR:-/data/basemap}"
CACHE_DIR="${CACHE_DIR:-/cache}"
# SEQ: Gympie/Noosa in the north to the NSW border, Toowoomba in the west.
BBOX="${BBOX:-151.8,-28.3,153.6,-26.0}"
# OpenMapTiles is tuned for z14 with client-side overzoom; 22-60 MB for SEQ.
MAXZOOM="${MAXZOOM:-14}"
# Geofabrik area. "australia" resolves to the country extract (smaller than the
# whole australia-oceania region).
AREA="${AREA:-australia}"
MEM="${MEM:-4g}"

mkdir -p "$OUT_DIR" "$CACHE_DIR"
TMP="$OUT_DIR/seq.pmtiles.tmp"
rm -f "$TMP"

# Run from the cache dir so Planetiler's default data/sources and data/tmp land
# on the persistent /cache volume rather than the container's ephemeral layer.
cd "$CACHE_DIR"

echo "Building SEQ basemap: area=$AREA bbox=$BBOX maxzoom=$MAXZOOM mem=$MEM"
java -Xmx"$MEM" -jar /planetiler/planetiler.jar \
  --download \
  --area="$AREA" \
  --bounds="$BBOX" \
  --maxzoom="$MAXZOOM" \
  --output="$TMP" \
  --force

# Atomic swap, same as the timetable ingest: a running server never sees a
# partial file.
mv -f "$TMP" "$OUT_DIR/seq.pmtiles"
echo "Done: $(du -h "$OUT_DIR/seq.pmtiles" | cut -f1) at $OUT_DIR/seq.pmtiles"
