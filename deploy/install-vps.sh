#!/usr/bin/env bash
#
# Install the Translink departures board onto a VPS that has ALREADY been
# provisioned by Java2026/inventoryquest/scripts/provision-vps.sh — that script
# creates the `deploy` user, installs rootless Podman, sets subuid/subgid ranges,
# enables linger, and turns on podman-auto-update.timer. This script assumes all
# of that exists and only adds the Translink units alongside iq-pod.
#
# Run as root on the VPS, from the directory containing this script:
#   bash install-vps.sh
#
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
IMAGE_REF="${IMAGE_REF:-ghcr.io/jasonuithol/translink-departures:latest}"
APP_PORT="${APP_PORT:-8000}"
OPEN_FIREWALL="${OPEN_FIREWALL:-yes}"
RUN_INGEST_NOW="${RUN_INGEST_NOW:-yes}"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root." >&2
  exit 1
fi

if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "User '${DEPLOY_USER}' does not exist — run inventoryquest's provision-vps.sh first." >&2
  exit 1
fi

DEPLOY_UID="$(id -u "${DEPLOY_USER}")"
DEPLOY_HOME="$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)"
RUNTIME_DIR="/run/user/${DEPLOY_UID}"

if [[ ! -d "${RUNTIME_DIR}" ]]; then
  echo "==> ${RUNTIME_DIR} missing — starting user@${DEPLOY_UID}.service…"
  systemctl start "user@${DEPLOY_UID}.service" || true
  sleep 2
fi

# Same rootless-environment wrapper as provision-vps.sh: sudo alone leaves
# XDG_RUNTIME_DIR unset and keeps root's CWD, both of which break rootless Podman.
as_deploy() {
  sudo -u "${DEPLOY_USER}" \
    XDG_RUNTIME_DIR="${RUNTIME_DIR}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME_DIR}/bus" \
    bash -lc "cd \"\$HOME\" 2>/dev/null || cd /; $*"
}

echo "==> Pulling ${IMAGE_REF}…"
as_deploy "podman pull '${IMAGE_REF}'"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUADLET_DIR="${DEPLOY_HOME}/.config/containers/systemd"
TIMER_DIR="${DEPLOY_HOME}/.config/systemd/user"

echo "==> Installing Quadlet units to ${QUADLET_DIR}…"
mkdir -p "${QUADLET_DIR}" "${TIMER_DIR}"
install -m644 "${SRC_DIR}/translink-data.volume"      "${QUADLET_DIR}/"
install -m644 "${SRC_DIR}/translink.container"        "${QUADLET_DIR}/"
install -m644 "${SRC_DIR}/translink-ingest.container" "${QUADLET_DIR}/"
# Quadlet does not process .timer files; this one is a plain user unit.
install -m644 "${SRC_DIR}/translink-ingest.timer"     "${TIMER_DIR}/"

# Apply IMAGE_REF / APP_PORT overrides to the installed copies.
sed -i "s|^Image=.*|Image=${IMAGE_REF}|" \
  "${QUADLET_DIR}/translink.container" "${QUADLET_DIR}/translink-ingest.container"
sed -i "s|^PublishPort=.*|PublishPort=${APP_PORT}:8000|" "${QUADLET_DIR}/translink.container"

chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_HOME}/.config"

echo "==> Reloading systemd and enabling the weekly ingest timer…"
as_deploy "
  set -e
  systemctl --user daemon-reload
  systemctl --user enable --now translink-ingest.timer
"

# The board 500s on every request until the timetable exists, and the timer will
# not fire until Sunday, so do the first ingest now unless told otherwise.
if [[ "${RUN_INGEST_NOW}" == "yes" ]]; then
  echo "==> Running the first ingest against the real feed (this may take a while)…"
  as_deploy "systemctl --user start translink-ingest.service"
fi

echo "==> Starting the board…"
as_deploy "systemctl --user start translink.service"

if [[ "${OPEN_FIREWALL}" == "yes" ]] && command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    echo "==> Opening ${APP_PORT}/tcp in ufw…"
    ufw allow "${APP_PORT}/tcp" || true
  fi
fi

echo
echo "════════════════════════════════════════════════════════════════════"
echo "  Departures board is up on  http://<this-vps-ip>:${APP_PORT}"
echo "  (inventoryquest keeps 8080; these run side by side.)"
echo
echo "    ssh ${DEPLOY_USER}@<this-vps-ip>"
echo "    systemctl --user status translink.service"
echo "    systemctl --user list-timers translink-ingest.timer"
echo "    podman logs -f translink"
echo
echo "  Refresh the timetable by hand:"
echo "    systemctl --user start translink-ingest.service"
echo "    journalctl --user -u translink-ingest.service -f"
echo "════════════════════════════════════════════════════════════════════"
