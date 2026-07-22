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
# Which region's basemap to build. Presets pick the bbox and the output name;
# override BBOX/OUT_NAME directly for anything else.
#   REGION=seq (default)  Gympie/Noosa to the NSW border, Toowoomba in the west
#   REGION=mel            greater Melbourne, Werribee to the Dandenongs
REGION="${REGION:-seq}"
case "$REGION" in
  seq) DEFAULT_BBOX="151.8,-28.3,153.6,-26.0" ;;
  mel) DEFAULT_BBOX="144.4,-38.5,145.8,-37.4" ;;
  *)   echo "Unknown REGION '$REGION' — set BBOX and OUT_NAME yourself." >&2
       DEFAULT_BBOX="" ;;
esac
BBOX="${BBOX:-$DEFAULT_BBOX}"
OUT_NAME="${OUT_NAME:-$REGION.pmtiles}"
[[ -n "$BBOX" ]] || { echo "No BBOX." >&2; exit 1; }
# OpenMapTiles is tuned for z14 with client-side overzoom; ~64 MB for SEQ.
MAXZOOM="${MAXZOOM:-14}"
# Geofabrik area. "australia" resolves to the country extract (smaller than the
# whole australia-oceania region).
AREA="${AREA:-australia}"
MEM="${MEM:-4g}"

mkdir -p "$OUT_DIR" "$CACHE_DIR"
# Planetiler infers the output FORMAT from the extension, so the scratch file
# must still end in .pmtiles — a "….pmtiles.tmp" name fails with
# "Unsupported format tmp". Dot-prefix keeps it hidden and the swap atomic.
TMP="$OUT_DIR/.build-$OUT_NAME"
rm -f "$TMP"

# Run from the cache dir so Planetiler's default data/sources and data/tmp land
# on the persistent /cache volume rather than the container's ephemeral layer.
cd "$CACHE_DIR"

echo "Building $REGION basemap: area=$AREA bbox=$BBOX maxzoom=$MAXZOOM mem=$MEM"
java -Xmx"$MEM" -jar /planetiler/planetiler.jar \
  --download \
  --area="$AREA" \
  --bounds="$BBOX" \
  --maxzoom="$MAXZOOM" \
  --output="$TMP" \
  --force

# Atomic swap, same as the timetable ingest: a running server never sees a
# partial file.
mv -f "$TMP" "$OUT_DIR/$OUT_NAME"
echo "Done: $(du -h "$OUT_DIR/$OUT_NAME" | cut -f1) at $OUT_DIR/$OUT_NAME"
