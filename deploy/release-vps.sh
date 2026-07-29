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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# VPS_HOST can come from the env or ~/.config/translink/keys.env.
source "${HERE}/load-keys.sh"
VPS="${1:-${VPS_HOST:-}}"
if [[ -z "$VPS" ]]; then
  echo "Usage: $0 root@<vps-host>   (or set VPS_HOST in the env or ~/.config/translink/keys.env)" >&2
  exit 1
fi

SKIP_BASEMAP="${SKIP_BASEMAP:-no}"
BASEMAP_REGIONS="${BASEMAP_REGIONS:-seq mel syd ade per dar qld nsw}"

SHIPPED_BASEMAPS=()
if [[ "$SKIP_BASEMAP" != "yes" ]]; then
  # The basemaps live inside the local rootless volume; cat each out via a
  # throwaway container rather than poking at the storage path directly.
  # Only the named regions ship; the VPS keeps its existing copy of the rest
  # (update-vps.sh skips any /tmp/<region>.pmtiles it doesn't find).
  for region in $BASEMAP_REGIONS; do
    if podman run --rm -v translink-data:/data alpine test -f "/data/basemap/${region}.pmtiles" 2>/dev/null; then
      # Don't re-ship what the VPS already has: compare sizes against a
      # leftover /tmp upload (interrupted run) or the installed volume copy.
      # A failed run AFTER the copy used to mean re-sending 640 MB.
      LOCAL_SIZE="$(podman run --rm -v translink-data:/data alpine stat -c %s "/data/basemap/${region}.pmtiles" 2>/dev/null || echo local-unknown)"
      REMOTE_SIZE="$(ssh "$VPS" "stat -c %s /tmp/${region}.pmtiles 2>/dev/null \
        || { DU=\${DEPLOY_USER:-deploy}; sudo -u \"\$DU\" XDG_RUNTIME_DIR=/run/user/\$(id -u \"\$DU\") \
             podman run --rm -v translink-data:/data alpine stat -c %s /data/basemap/${region}.pmtiles 2>/dev/null; } \
        || echo 0" 2>/dev/null || echo remote-unknown)"
      if [[ "$LOCAL_SIZE" == "$REMOTE_SIZE" ]]; then
        echo "==> ${region} basemap already on ${VPS} (${LOCAL_SIZE} bytes) — skipping the copy."
        SHIPPED_BASEMAPS+=("$region")   # present on the target: mark it deployed
        continue
      fi
      TMP_MAP="$(mktemp "/tmp/${region}.pmtiles.XXXXXX")"
      trap 'rm -f /tmp/seq.pmtiles.?????? /tmp/mel.pmtiles.?????? /tmp/syd.pmtiles.?????? /tmp/ade.pmtiles.?????? /tmp/per.pmtiles.?????? /tmp/dar.pmtiles.?????? /tmp/qld.pmtiles.?????? /tmp/nsw.pmtiles.??????' EXIT
      echo "==> Exporting ${region} basemap from the local volume…"
      podman run --rm -v translink-data:/data alpine cat "/data/basemap/${region}.pmtiles" > "$TMP_MAP"
      echo "==> Copying basemap to ${VPS}:/tmp/${region}.pmtiles ($(du -h "$TMP_MAP" | cut -f1))…"
      scp -q "$TMP_MAP" "${VPS}:/tmp/${region}.pmtiles"
      SHIPPED_BASEMAPS+=("$region")
    else
      echo "==> No ${region}.pmtiles in the local volume — skipping that basemap."
      echo "    (Build it first with: podman run --rm -e REGION=${region} \\"
      echo "       -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap)"
    fi
  done
fi

echo "==> Copying update-vps.sh and running it on ${VPS}…"
scp -q "${HERE}/update-vps.sh" "${VPS}:/tmp/update-vps.sh"
# INGEST_* default to auto (= only if that region's DB is missing): the
# weekly timer owns timetable freshness, a release should be minutes.
ssh -t "$VPS" "INGEST_MEL='${INGEST_MEL:-auto}' INGEST_SYD='${INGEST_SYD:-auto}' INGEST_ADE='${INGEST_ADE:-auto}' INGEST_PER='${INGEST_PER:-auto}' INGEST_DAR='${INGEST_DAR:-auto}' INGEST_QLD='${INGEST_QLD:-auto}' INGEST_NSW='${INGEST_NSW:-auto}' bash /tmp/update-vps.sh"

# The update ran to completion (set -e): record what this release deployed.
# TARGET_NAME picks the state file, for the day there's ever a second VPS.
MARK=(image)
for region in ${SHIPPED_BASEMAPS[@]+"${SHIPPED_BASEMAPS[@]}"}; do
  MARK+=("basemap-${region}")
done
"${HERE}/mark-deployed.sh" "${TARGET_NAME:-vps}" "${MARK[@]}" || true
