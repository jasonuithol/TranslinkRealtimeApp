#!/usr/bin/env bash
#
# Release to the VPS from this machine, in one command:
#
#   ./deploy/release-vps.sh root@<vps-host>
#
# Does the whole dance: exports the locally-built Melbourne basemap out of the
# local podman volume (if present), copies it and update-vps.sh to the VPS,
# then runs the update there as root — image pull, Melbourne ingest, basemap
# install, restart, health checks. Assumes CI has already published the image
# (push to main and wait for green before running this).
#
# Env knobs, all optional:
#   SKIP_BASEMAP=yes            don't export/copy any basemap
#   BASEMAP_REGIONS="nsw qld"   ship only these regions' basemaps (default:
#                               all of them — ~1.5 GB; you usually know which
#                               one you rebuilt, so name it)
#   INGEST_MEL=no      passed through: skip the Melbourne timetable ingest
set -euo pipefail

VPS="${1:-${VPS_HOST:-}}"
if [[ -z "$VPS" ]]; then
  echo "Usage: $0 root@<vps-host>   (or set VPS_HOST)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_BASEMAP="${SKIP_BASEMAP:-no}"
BASEMAP_REGIONS="${BASEMAP_REGIONS:-mel syd ade per dar qld nsw}"

if [[ "$SKIP_BASEMAP" != "yes" ]]; then
  # The basemaps live inside the local rootless volume; cat each out via a
  # throwaway container rather than poking at the storage path directly.
  # Only the named regions ship; the VPS keeps its existing copy of the rest
  # (update-vps.sh skips any /tmp/<region>.pmtiles it doesn't find).
  for region in $BASEMAP_REGIONS; do
    if podman run --rm -v translink-data:/data alpine test -f "/data/basemap/${region}.pmtiles" 2>/dev/null; then
      TMP_MAP="$(mktemp "/tmp/${region}.pmtiles.XXXXXX")"
      trap 'rm -f /tmp/mel.pmtiles.?????? /tmp/syd.pmtiles.?????? /tmp/ade.pmtiles.?????? /tmp/per.pmtiles.?????? /tmp/dar.pmtiles.?????? /tmp/qld.pmtiles.?????? /tmp/nsw.pmtiles.??????' EXIT
      echo "==> Exporting ${region} basemap from the local volume…"
      podman run --rm -v translink-data:/data alpine cat "/data/basemap/${region}.pmtiles" > "$TMP_MAP"
      echo "==> Copying basemap to ${VPS}:/tmp/${region}.pmtiles ($(du -h "$TMP_MAP" | cut -f1))…"
      scp -q "$TMP_MAP" "${VPS}:/tmp/${region}.pmtiles"
    else
      echo "==> No ${region}.pmtiles in the local volume — skipping that basemap."
      echo "    (Build it first with: podman run --rm -e REGION=${region} \\"
      echo "       -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap)"
    fi
  done
fi

echo "==> Copying update-vps.sh and running it on ${VPS}…"
scp -q "${HERE}/update-vps.sh" "${VPS}:/tmp/update-vps.sh"
ssh -t "$VPS" "INGEST_MEL='${INGEST_MEL:-yes}' INGEST_SYD='${INGEST_SYD:-auto}' INGEST_ADE='${INGEST_ADE:-yes}' INGEST_PER='${INGEST_PER:-yes}' INGEST_DAR='${INGEST_DAR:-yes}' INGEST_QLD='${INGEST_QLD:-yes}' INGEST_NSW='${INGEST_NSW:-auto}' bash /tmp/update-vps.sh"
