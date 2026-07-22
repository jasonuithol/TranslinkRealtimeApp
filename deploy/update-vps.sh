#!/usr/bin/env bash
#
# Bring an already-installed VPS up to date with the current release:
# pull the newest image, ingest the Melbourne timetable, install the Melbourne
# basemap if one has been copied up, restart, and health-check.
#
# Run as root on the VPS:
#   bash update-vps.sh
#
# The Melbourne basemap cannot be built on the VPS (Planetiler wants ~4 GB RAM
# and a 2 GB download), so build it locally and copy it up FIRST if you want
# the Melbourne map (the board works without it):
#
#   local$  podman run --rm -v translink-data:/data alpine \
#             cat /data/basemap/mel.pmtiles > /tmp/mel.pmtiles
#   local$  scp /tmp/mel.pmtiles root@<vps>:/tmp/mel.pmtiles
#
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
IMAGE_REF="${IMAGE_REF:-ghcr.io/jasonuithol/translink-departures:latest}"
APP_PORT="${APP_PORT:-8000}"
MEL_BASEMAP_SRC="${MEL_BASEMAP_SRC:-/tmp/mel.pmtiles}"
SYD_BASEMAP_SRC="${SYD_BASEMAP_SRC:-/tmp/syd.pmtiles}"
INGEST_MEL="${INGEST_MEL:-yes}"
# Sydney's static downloads need the TfNSW key; 'auto' ingests only when the
# quadlet already carries SYD_API_KEY (i.e. enable-syd-vps.sh has run).
INGEST_SYD="${INGEST_SYD:-auto}"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

DEPLOY_UID="$(id -u "${DEPLOY_USER}")"
RUNTIME_DIR="/run/user/${DEPLOY_UID}"

# Same rootless-environment wrapper as install-vps.sh: sudo alone leaves
# XDG_RUNTIME_DIR unset, which breaks rootless Podman. NEVER run podman as
# root here — root has its own separate volume namespace, and a root-created
# `translink-data` is an empty decoy (this has happened).
as_deploy() {
  sudo -u "${DEPLOY_USER}" \
    XDG_RUNTIME_DIR="${RUNTIME_DIR}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME_DIR}/bus" \
    bash -lc "cd \"\$HOME\" 2>/dev/null || cd /; $*"
}

echo "==> Pulling ${IMAGE_REF}…"
as_deploy "podman pull '${IMAGE_REF}'"

if [[ "${INGEST_MEL}" == "yes" ]]; then
  echo "==> Ingesting the Melbourne timetable (PTV zip is ~292 MB; a few minutes)…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region mel"
fi

# The Sydney downloads are authenticated, so the ingest needs the key the
# quadlet holds (written there by enable-syd-vps.sh).
QUADLET="$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)/.config/containers/systemd/translink.container"
SYD_KEY="$(grep -oP '^Environment=SYD_API_KEY=\K.*' "$QUADLET" 2>/dev/null || true)"
if [[ "${INGEST_SYD}" == "yes" || ( "${INGEST_SYD}" == "auto" && -n "$SYD_KEY" ) ]]; then
  if [[ -n "$SYD_KEY" ]]; then
    echo "==> Ingesting the Sydney timetable (per-mode TfNSW zips; a few minutes)…"
    as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${SYD_KEY}' \
      '${IMAGE_REF}' python ingest_gtfs.py --region syd"
  else
    echo "==> INGEST_SYD requested but no SYD_API_KEY in the quadlet — run"
    echo "    deploy/enable-syd-vps.sh first. Skipping the Sydney ingest."
  fi
else
  echo "==> Sydney ingest skipped (no SYD_API_KEY in the quadlet yet)."
fi

install_basemap() {
  local src="$1" name="$2"
  if [[ -f "$src" ]]; then
    echo "==> Installing ${name} basemap from ${src}…"
    # World-readable so the deploy user's container can read it from /tmp.
    chmod 0644 "$src"
    as_deploy "podman run --rm -v translink-data:/data -v /tmp:/in:ro alpine \
      sh -c 'cp /in/$(basename "$src") /data/basemap/${name}.pmtiles.new \
             && mv /data/basemap/${name}.pmtiles.new /data/basemap/${name}.pmtiles \
             && chown 1000:1000 /data/basemap/${name}.pmtiles'"
    rm -f "$src"
  else
    echo "==> No ${src} found — skipping the ${name} basemap."
    echo "    (That region's board still works; only its map stays hidden.)"
  fi
}
install_basemap "${MEL_BASEMAP_SRC}" mel
install_basemap "${SYD_BASEMAP_SRC}" syd

echo "==> Restarting the board (warms the per-region caches)…"
as_deploy "systemctl --user restart translink.service"

# A restart recreates the container from the newly pulled image; uvicorn can
# take a while to come up on a small VPS. Wait for the board rather than
# racing it (a flat sleep raced it, and lost).
echo "==> Waiting for the board to come up (up to 120 s)…"
up=0
for i in $(seq 1 40); do
  if curl -fsS --max-time 3 "http://localhost:${APP_PORT}/api/config" >/dev/null 2>&1; then
    up=1; echo "    up after ~$((i * 3))s"; break
  fi
  sleep 3
done
if [[ $up -ne 1 ]]; then
  echo "Board did not come up within 120 s. Logs:"
  as_deploy "podman logs --tail 30 translink" || true
  exit 1
fi

echo "==> Health checks…"
fail=0
check() {
  local label="$1" url="$2" want="$3"
  local got attempt
  for attempt in 1 2 3; do
    got=$(curl -fsS --max-time 20 "$url" 2>/dev/null) && break
    sleep 3
  done
  if [[ -z "${got:-}" ]]; then echo "  FAIL $label: no response"; fail=1; return; fi
  if grep -q "$want" <<<"$got"; then
    echo "  ok   $label"
  else
    echo "  FAIL $label: wanted '$want' in: ${got:0:120}"; fail=1
  fi
}
check "board up"          "http://localhost:${APP_PORT}/api/config"          '"basemap"'
check "regions list"      "http://localhost:${APP_PORT}/api/regions"         '"seq"'
check "seq departures"    "http://localhost:${APP_PORT}/api/r/seq/departures/place_censta" '"departures"'
if [[ "${INGEST_MEL}" == "yes" ]]; then
  check "mel region"      "http://localhost:${APP_PORT}/api/regions"         '"mel"'
  check "mel departures"  "http://localhost:${APP_PORT}/api/r/mel/departures/2:vic:rail:FSS" '"departures"'
  check "mel config"      "http://localhost:${APP_PORT}/api/r/mel/config"    '"basemap"'
fi
# Only meaningful once the Sydney timetable has been ingested at least once.
if curl -fsS --max-time 10 "http://localhost:${APP_PORT}/api/regions" 2>/dev/null | grep -q '"syd"'; then
  check "syd config"      "http://localhost:${APP_PORT}/api/r/syd/config"    '"basemap"'
  check "syd search"      "http://localhost:${APP_PORT}/api/r/syd/stops/search?q=Circular" '"stop_id"'
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "════════════════════════════════════════════════════════"
  echo "  Updated. Board: http://<this-vps-ip>:${APP_PORT}"
  echo "  Melbourne: same URL — the ⇄ switch appears in the eyebrow."
  echo "  NOTE: the weekly ingest timer refreshes SEQ only; re-run"
  echo "  this script (or the ingest line in it) to refresh Melbourne."
  echo "════════════════════════════════════════════════════════"
else
  echo "One or more health checks FAILED — see above. The previous"
  echo "image keeps serving until a restart succeeds; check:"
  echo "  sudo -iu ${DEPLOY_USER} podman logs translink"
  exit 1
fi
