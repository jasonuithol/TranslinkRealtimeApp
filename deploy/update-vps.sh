#!/usr/bin/env bash
#
# Bring an already-installed VPS up to date with the current release:
# pull the newest image, ingest any MISSING timetables (freshness is the
# weekly timer's job), install whatever basemaps were copied up, restart,
# and health-check.
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
ADE_BASEMAP_SRC="${ADE_BASEMAP_SRC:-/tmp/ade.pmtiles}"
PER_BASEMAP_SRC="${PER_BASEMAP_SRC:-/tmp/per.pmtiles}"
DAR_BASEMAP_SRC="${DAR_BASEMAP_SRC:-/tmp/dar.pmtiles}"
QLD_BASEMAP_SRC="${QLD_BASEMAP_SRC:-/tmp/qld.pmtiles}"
NSW_BASEMAP_SRC="${NSW_BASEMAP_SRC:-/tmp/nsw.pmtiles}"
# Per-region ingest switches: yes = always, no = never, auto (default) =
# only when that region's DB is missing. Timetable FRESHNESS is the weekly
# timer's job (`--region all`) — re-ingesting nine regions on every release
# made deploys ~20 minutes of downloads, and rebooting the app onto nine
# stone-cold SQLite files made the cache warm-up so slow the readiness
# probe below once gave up on a perfectly healthy board.
INGEST_MEL="${INGEST_MEL:-auto}"
INGEST_ADE="${INGEST_ADE:-auto}"
INGEST_PER="${INGEST_PER:-auto}"
INGEST_DAR="${INGEST_DAR:-auto}"
INGEST_QLD="${INGEST_QLD:-auto}"
# Sydney and NSW TrainLink downloads also need the TfNSW key the quadlet
# holds (written by enable-syd-vps.sh) — without it even yes/auto skip.
INGEST_SYD="${INGEST_SYD:-auto}"
INGEST_NSW="${INGEST_NSW:-auto}"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

DEPLOY_UID="$(id -u "${DEPLOY_USER}")"
RUNTIME_DIR="/run/user/${DEPLOY_UID}"

# Health checks probe the host's primary interface address, NOT localhost:
# pasta (rootless podman's port forwarder) stopped answering published ports
# on 127.0.0.1 from the host itself (found 2026-07-29 — every localhost
# check failed for its full window while the public interface served fine,
# which serially killed deploys at the verification step).
PROBE_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
PROBE_HOST="${PROBE_HOST:-localhost}"

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

have_db() {  # e.g. have_db gtfs-mel.sqlite3
  as_deploy "podman run --rm -v translink-data:/data alpine test -f '/data/$1'" 2>/dev/null
}
want_ingest() {  # $1 = yes|no|auto, $2 = db filename
  case "$1" in
    yes) return 0 ;;
    no)  return 1 ;;
    *)   ! have_db "$2" ;;
  esac
}

if want_ingest "${INGEST_MEL}" gtfs-mel.sqlite3; then
  echo "==> Ingesting the Melbourne timetable (PTV zip is ~292 MB; a few minutes)…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region mel"
else
  echo "==> Melbourne timetable present — not re-ingesting (the weekly timer keeps it fresh)."
fi

if want_ingest "${INGEST_ADE}" gtfs-ade.sqlite3; then
  echo "==> Ingesting the Adelaide timetable…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region ade"
fi

if want_ingest "${INGEST_PER}" gtfs-per.sqlite3; then
  echo "==> Ingesting the Perth timetable…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region per"
fi

if want_ingest "${INGEST_DAR}" gtfs-dar.sqlite3; then
  echo "==> Ingesting the Darwin timetable…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region dar"
fi

if want_ingest "${INGEST_QLD}" gtfs-qld.sqlite3; then
  echo "==> Ingesting the regional Queensland timetables (18 small zips)…"
  as_deploy "podman run --rm -v translink-data:/data '${IMAGE_REF}' \
    python ingest_gtfs.py --region qld"
fi

# The Sydney downloads are authenticated, so the ingest needs the key the
# quadlet holds (written there by enable-syd-vps.sh).
QUADLET="$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)/.config/containers/systemd/translink.container"
# Portable extraction: grep -P is not available on every distro's grep, and
# the silent failure here read as "no key" and skipped the Sydney ingest.
SYD_KEY="$(sed -n 's/^Environment=SYD_API_KEY=//p' "$QUADLET" 2>/dev/null | head -1)"
if want_ingest "${INGEST_SYD}" gtfs-syd.sqlite3; then
  if [[ -n "$SYD_KEY" ]]; then
    echo "==> Ingesting the Sydney timetable (per-mode TfNSW zips; a few minutes)…"
    as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${SYD_KEY}' \
      '${IMAGE_REF}' python ingest_gtfs.py --region syd"
  else
    echo "==> Sydney ingest wanted but no SYD_API_KEY in the quadlet — run"
    echo "    deploy/enable-syd-vps.sh first. Skipping the Sydney ingest."
  fi
else
  echo "==> Sydney ingest skipped (DB present, or INGEST_SYD=no)."
fi

if want_ingest "${INGEST_NSW}" gtfs-nsw.sqlite3; then
  if [[ -n "$SYD_KEY" ]]; then
    echo "==> Ingesting the NSW TrainLink timetable (one TfNSW zip)…"
    as_deploy "podman run --rm -v translink-data:/data -e SYD_API_KEY='${SYD_KEY}' \
      '${IMAGE_REF}' python ingest_gtfs.py --region nsw"
  else
    echo "==> TrainLink ingest wanted but no SYD_API_KEY in the quadlet — run"
    echo "    deploy/enable-syd-vps.sh first. Skipping the TrainLink ingest."
  fi
else
  echo "==> NSW TrainLink ingest skipped (DB present, or INGEST_NSW=no)."
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
install_basemap "${ADE_BASEMAP_SRC}" ade
install_basemap "${PER_BASEMAP_SRC}" per
install_basemap "${DAR_BASEMAP_SRC}" dar
install_basemap "${QLD_BASEMAP_SRC}" qld
install_basemap "${NSW_BASEMAP_SRC}" nsw

# The G-NAF address database (house-number geocoding) and the walking
# graph (planner walk-leg routing) ship like basemaps: built locally,
# copied up, installed atomically. Absent is fine — geocoding falls back
# to Nominatim; walk legs fall back to straight dashed lines.
install_data_db() {
  local src="$1" name="$2" label="$3"
  if [[ -f "$src" ]]; then
    echo "==> Installing ${label}…"
    chmod 0644 "$src"
    as_deploy "podman run --rm -v translink-data:/data -v /tmp:/in:ro alpine \
      sh -c 'cp /in/${name} /data/${name}.new \
             && mv /data/${name}.new /data/${name} \
             && chown 1000:1000 /data/${name}'"
    rm -f "$src"
  else
    echo "==> No ${src} — ${label} unchanged."
  fi
}
install_data_db "${GNAF_SRC:-/tmp/gnaf.sqlite3}" gnaf.sqlite3 \
  "the G-NAF address database"
install_data_db "${WALKGRAPH_SRC:-/tmp/walkgraph.sqlite3}" walkgraph.sqlite3 \
  "the walking graph"
install_data_db "${PLACES_SRC:-/tmp/places.sqlite3}" places.sqlite3 \
  "the places index"

echo "==> Restarting the board (warms the per-region caches)…"
as_deploy "systemctl --user restart translink.service"

# A restart recreates the container from the newly pulled image; uvicorn can
# take a while to come up on a small VPS. Wait for the board rather than
# racing it (a flat sleep raced it, and lost).
# NINE ingested regions warm their stop caches at boot — after a fresh
# ingest the SQLite files are stone cold and the warm-up can starve the
# event loop for several minutes (2026-07-29: a 300 s window declared a
# perfectly healthy board dead while it was serving external requests).
echo "==> Waiting for the board to come up (up to 900 s)…"
up=0
for i in $(seq 1 90); do
  if curl -fsS --max-time 10 "http://${PROBE_HOST}:${APP_PORT}/api/config" >/dev/null 2>&1; then
    up=1; echo "    up after ~$((i * 10))s"; break
  fi
  sleep 10
done
if [[ $up -ne 1 ]]; then
  echo "Board did not come up within 900 s. Logs:"
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
check "board up"          "http://${PROBE_HOST}:${APP_PORT}/api/config"          '"basemap"'
check "regions list"      "http://${PROBE_HOST}:${APP_PORT}/api/regions"         '"seq"'
check "seq departures"    "http://${PROBE_HOST}:${APP_PORT}/api/r/seq/departures/place_censta" '"departures"'
if [[ "${INGEST_MEL}" == "yes" ]]; then
  check "mel region"      "http://${PROBE_HOST}:${APP_PORT}/api/regions"         '"mel"'
  check "mel departures"  "http://${PROBE_HOST}:${APP_PORT}/api/r/mel/departures/2:vic:rail:FSS" '"departures"'
  check "mel config"      "http://${PROBE_HOST}:${APP_PORT}/api/r/mel/config"    '"basemap"'
fi
# Only meaningful once the Sydney timetable has been ingested at least once.
if curl -fsS --max-time 10 "http://${PROBE_HOST}:${APP_PORT}/api/regions" 2>/dev/null | grep -q '"syd"'; then
  check "syd config"      "http://${PROBE_HOST}:${APP_PORT}/api/r/syd/config"    '"basemap"'
  check "syd search"      "http://${PROBE_HOST}:${APP_PORT}/api/r/syd/stops/search?q=Circular" '"stop_id"'
fi
# Same deal for NSW TrainLink — only once its timetable exists.
if curl -fsS --max-time 10 "http://${PROBE_HOST}:${APP_PORT}/api/regions" 2>/dev/null | grep -q '"nsw"'; then
  check "nsw config"      "http://${PROBE_HOST}:${APP_PORT}/api/r/nsw/config"    '"basemap"'
  check "nsw search"      "http://${PROBE_HOST}:${APP_PORT}/api/r/nsw/stops/search?q=Central" '"stop_id"'
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "════════════════════════════════════════════════════════"
  echo "  Updated. Board: http://<this-vps-ip>:${APP_PORT}"
  echo "  Melbourne: same URL — the ⇄ switch appears in the eyebrow."
  echo "  NOTE: the weekly ingest timer refreshes every region (keyed ones"
  echo "  only once enable-syd-vps.sh has written the key into the ingest"
  echo "  quadlet). This script also refreshes them all right now."
  echo "════════════════════════════════════════════════════════"
else
  echo "One or more health checks FAILED — see above. The previous"
  echo "image keeps serving until a restart succeeds; check:"
  echo "  sudo -iu ${DEPLOY_USER} podman logs translink"
  exit 1
fi
