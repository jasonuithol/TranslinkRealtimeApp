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
#   SKIP_BASEMAP=yes   don't export/copy the Melbourne basemap
#   INGEST_MEL=no      passed through: skip the Melbourne timetable ingest
set -euo pipefail

VPS="${1:-${VPS_HOST:-}}"
if [[ -z "$VPS" ]]; then
  echo "Usage: $0 root@<vps-host>   (or set VPS_HOST)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_BASEMAP="${SKIP_BASEMAP:-no}"

if [[ "$SKIP_BASEMAP" != "yes" ]]; then
  # The basemaps live inside the local rootless volume; cat each out via a
  # throwaway container rather than poking at the storage path directly.
  for region in mel syd; do
    if podman run --rm -v translink-data:/data alpine test -f "/data/basemap/${region}.pmtiles" 2>/dev/null; then
      TMP_MAP="$(mktemp "/tmp/${region}.pmtiles.XXXXXX")"
      trap 'rm -f /tmp/mel.pmtiles.?????? /tmp/syd.pmtiles.??????' EXIT
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
ssh -t "$VPS" "INGEST_MEL='${INGEST_MEL:-yes}' INGEST_SYD='${INGEST_SYD:-auto}' bash /tmp/update-vps.sh"
