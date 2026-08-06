#!/usr/bin/env bash
#
# THE deploy command — the only two invocations you need:
#
#   ./deploy/deploy.sh local            # deploy everything pending locally
#   ./deploy/deploy.sh vps              # deploy everything pending to the VPS
#   ./deploy/deploy.sh vps --dry-run    # print the plan, touch nothing
#
# deploy/state/<target>.yaml is the single source of truth: every component
# marked `pending` gets deployed, then flipped to `deployed`. No knobs — the
# yaml already knows which basemaps changed, whether units need syncing,
# which regions need enabling. Change something? Its component goes pending
# in the yaml (in the same change). Then run this.
#
# blocked components are reported and skipped. Keys come from
# ~/.config/translink/keys.env (see load-keys.sh); an enable step whose key
# is missing is reported and left pending — nothing fails.
#
# The older scripts (release-vps.sh, enable-*-vps.sh, update-vps.sh) are the
# machinery this calls; they still work standalone for surgical use.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
source "${HERE}/load-keys.sh"

# Files that bake into the container image. The image is the ONE component
# that deploys via GitHub (push → CI builds → GHCR → target pulls); every
# other component ships straight from this working tree. So before pulling
# a pending image anywhere, prove GHCR actually has the local code.
# The image these targets run. Built here now, pushed to GHCR so the VPS
# can pull it exactly as it always has.
IMAGE_NAME="${IMAGE_NAME:-ghcr.io/jasonuithol/translink-departures}"

IMAGE_INPUTS=(app.py regions.py realtime.py gtfsdb.py boards.py geocoding.py
              walking.py planning.py planner.py
              static ingest_gtfs.py requirements.txt Containerfile)

image_gate() {
  # The image is built HERE now, from `git archive HEAD` — so the only
  # thing to check is that HEAD contains what you think it does. CI used
  # to be the gate; waiting on a queue to test a CSS change was a poor
  # trade, and a late run could overwrite :latest with an older build.
  local dirty
  dirty="$(git -C "$REPO" status --porcelain -- "${IMAGE_INPUTS[@]}")"
  if [[ -n "$dirty" ]]; then
    echo "!! image inputs have uncommitted changes, and the image is built from HEAD:"
    sed 's/^/     /' <<<"$dirty"
    echo "   Commit them, or use ./tools/test.sh --dirty to try them first."
    return 1
  fi
  return 0
}

# Build the image from HEAD and tag it. A clean checkout, so a file left
# uncommitted cannot end up in what ships — the one property of a CI
# build worth keeping.
build_image() {
  local sha tmp
  sha="$(git -C "$REPO" rev-parse HEAD)"
  tmp="$(mktemp -d)"
  git -C "$REPO" archive HEAD | tar -x -C "$tmp"
  echo "==> Building ${IMAGE_NAME}:${sha:0:7} from HEAD…"
  podman build -q -t "${IMAGE_NAME}:latest" -t "${IMAGE_NAME}:${sha}" "$tmp" >/dev/null
  rm -rf "$tmp"
  echo "==> Testing it…"
  IMAGE="${IMAGE_NAME}:latest" "${REPO}/tools/test.sh" | sed 's/^/   /'
}

# Send it to GHCR, which the VPS pulls from. Needs a login with
# write:packages once per machine:
#
#   gh auth refresh -h github.com -s write:packages
#   gh auth token | podman login ghcr.io -u jasonuithol --password-stdin
push_image() {
  local sha
  sha="$(git -C "$REPO" rev-parse HEAD)"
  echo "==> Pushing ${IMAGE_NAME}:${sha:0:7} and :latest…"
  if ! podman push "${IMAGE_NAME}:${sha}" 2>&1 | tail -1; then
    echo "!! push failed — is this machine logged in to GHCR?"
    echo "   gh auth refresh -h github.com -s write:packages"
    echo "   gh auth token | podman login ghcr.io -u jasonuithol --password-stdin"
    return 1
  fi
  podman push "${IMAGE_NAME}:latest" 2>&1 | tail -1
}

TARGET="${1:-}"
DRY=no
[[ "${2:-}" == "--dry-run" ]] && DRY=yes || true
FILE="${HERE}/state/${TARGET}.yaml"
if [[ -z "$TARGET" || ! -f "$FILE" ]]; then
  echo "Usage: $0 <local|vps> [--dry-run]" >&2
  exit 1
fi

HOST="$(sed -n 's/^host: //p' "$FILE")"
mapfile -t PENDING < <(sed -nE 's/^  ([a-z0-9-]+): \{ status: pending.*/\1/p' "$FILE")
mapfile -t BLOCKED < <(sed -nE 's/^  ([a-z0-9-]+): \{ status: blocked.*/\1/p' "$FILE")

has() { local c; for c in ${PENDING[@]+"${PENDING[@]}"}; do [[ "$c" == "$1" ]] && return 0; done; return 1; }

if [[ ${#PENDING[@]} -eq 0 ]]; then
  echo "==> ${TARGET}: nothing pending — everything is deployed."
  if [[ ${#BLOCKED[@]} -gt 0 ]]; then
    printf '    (blocked, skipped: %s)\n' "${BLOCKED[*]}"
  fi
  exit 0
fi

echo "==> ${TARGET} (${HOST}): pending components: ${PENDING[*]}"
if [[ ${#BLOCKED[@]} -gt 0 ]]; then echo "    blocked (skipped): ${BLOCKED[*]}"; fi
if [[ "$DRY" == "yes" ]]; then echo "    DRY RUN — printing the plan only."; fi

DONE=()      # handled below (marked by us or by the script we called)
LEFTOVER=()  # pending components nothing here knows how to deploy

# ---------------------------------------------------------------------------
# Sync the systemd quadlet units from the repo to the VPS, PRESERVING the
# Environment= lines the enable scripts injected into the live copies (keys,
# realtime feed URLs) and the live PublishPort. This is the deploy path unit
# changes never had: update-vps.sh must not clobber keys, so nothing synced
# units after install-vps.sh.
sync_quadlet_units() {
  scp -q "${HERE}/translink.container" "${HERE}/translink-ingest.container" \
         "${HERE}/translink-ingest.timer" "${HOST}:/tmp/"
  ssh "$HOST" DEPLOY_USER="${DEPLOY_USER:-deploy}" 'bash -s' <<'REMOTE'
set -euo pipefail
HOME_DIR="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
QUADLET_DIR="${HOME_DIR}/.config/containers/systemd"
TIMER_DIR="${HOME_DIR}/.config/systemd/user"
DEPLOY_UID="$(id -u "$DEPLOY_USER")"
RUNTIME_DIR="/run/user/${DEPLOY_UID}"
as_deploy() {
  sudo -u "$DEPLOY_USER" XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME_DIR}/bus" \
    bash -lc "cd \"\$HOME\" 2>/dev/null || cd /; $*"
}

merge_unit() {  # $1 = repo template, $2 = live unit; merged to stdout
  python3 - "$1" "$2" <<'PY'
import os, sys
new = open(sys.argv[1]).read().splitlines()
live = open(sys.argv[2]).read().splitlines() if os.path.exists(sys.argv[2]) else []

def envname(line):  # "Environment=FOO=bar" -> "FOO"
    return line[len("Environment="):].split("=", 1)[0]

new_envs = {envname(l) for l in new if l.startswith("Environment=")}
# Keys and enable-script wiring live only in the live unit: keep them.
keep = [l for l in live
        if l.startswith("Environment=") and envname(l) not in new_envs]
live_pp = next((l for l in live if l.startswith("PublishPort=")), None)

env_idx = [i for i, l in enumerate(new) if l.startswith("Environment=")]
insert_after = env_idx[-1] if env_idx else next(
    (i for i, l in enumerate(new) if l.strip() == "[Container]"), None)

out = []
for i, l in enumerate(new):
    if l.startswith("PublishPort=") and live_pp:
        l = live_pp                     # the installed port wins
    out.append(l)
    if insert_after is not None and i == insert_after and keep:
        out.extend(keep)
print("\n".join(out))
PY
}

for unit in translink.container translink-ingest.container; do
  merge_unit "/tmp/${unit}" "${QUADLET_DIR}/${unit}" > "/tmp/${unit}.merged"
  install -m600 "/tmp/${unit}.merged" "${QUADLET_DIR}/${unit}"
  chown "${DEPLOY_USER}:" "${QUADLET_DIR}/${unit}"
  rm -f "/tmp/${unit}" "/tmp/${unit}.merged"
done
install -m644 "/tmp/translink-ingest.timer" "${TIMER_DIR}/translink-ingest.timer"
chown "${DEPLOY_USER}:" "${TIMER_DIR}/translink-ingest.timer"
rm -f /tmp/translink-ingest.timer

as_deploy "systemctl --user daemon-reload"
as_deploy "systemctl --user restart translink.service"
as_deploy "systemctl --user restart translink-ingest.timer"
as_deploy "systemctl --user is-active translink.service" >/dev/null \
  && echo "══> quadlet units synced; translink.service is active."
REMOTE
}

if [[ "$TARGET" == "local" ]]; then
  IMAGE_REF="${IMAGE_REF:-${IMAGE_NAME}:latest}"
  if has image; then
    image_gate || exit 1
    if [[ "$DRY" == "yes" ]]; then
      echo "  PLAN image: build from HEAD + ./tools/test.sh; recreate container 'translink' on :8000"
    else
      build_image
      podman rm -f translink >/dev/null 2>&1 || true
      podman run -d --name translink -p 8000:8000 -v translink-data:/data "$IMAGE_REF" >/dev/null
      for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null http://localhost:8000/ 2>/dev/null; then break; fi
        sleep 1
      done
      curl -fsS -o /dev/null http://localhost:8000/api/regions
      # Still verified, cheaply: the running container must be the image
      # built from HEAD a moment ago. Marking something deployed that is
      # not is the one thing this file must never do.
      if [[ "$(podman inspect translink --format '{{.Image}}')" \
         != "$(podman image inspect "${IMAGE_NAME}:$(git rev-parse HEAD)" \
               --format '{{.Id}}')" ]]; then
        echo "!! the running container is not the image built from HEAD."
        exit 1
      fi
      echo "==> local translink is up on :8000."
      "${HERE}/mark-deployed.sh" local image
    fi
    DONE+=(image)
  fi
else  # ------------------------------------------------------------- vps ---
  # 1. Image + changed basemaps, in one release (update-vps.sh pulls, ingests
  #    any missing timetables, installs shipped basemaps, health-checks).
  REGIONS=""
  for c in ${PENDING[@]+"${PENDING[@]}"}; do
    if [[ "$c" == basemap-* ]]; then REGIONS="${REGIONS:+$REGIONS }${c#basemap-}"; fi
  done
  GNAF=no
  if has gnaf-db; then GNAF=yes; fi
  WALKG=no
  if has walkgraph-db; then WALKG=yes; fi
  PLACES=no
  if has places-db; then PLACES=yes; fi
  if has image || [[ -n "$REGIONS" ]] || [[ "$GNAF" == "yes" ]] \
      || [[ "$WALKG" == "yes" ]] || [[ "$PLACES" == "yes" ]]; then
    # The update pulls :latest even on a basemap-only run, so gate whenever
    # the image is what's being deployed here.
    if has image; then image_gate || exit 1; fi
    if [[ "$DRY" == "yes" ]]; then
      echo "  PLAN release: image pull + update on ${HOST}${REGIONS:+, shipping basemaps: ${REGIONS}}$([[ "$GNAF" == yes ]] && echo ", shipping G-NAF DB")$([[ "$WALKG" == yes ]] && echo ", shipping walking graph")$([[ "$PLACES" == yes ]] && echo ", shipping places index")"
    else
      # Build and test here, then push, then let the VPS pull as it always
      # has. GHCR is now a delivery mechanism rather than a build service:
      # nothing waits on a queue, and the sha tag still gives a rollback.
      if has image; then
        build_image
        push_image
      fi
      SKIP_BASEMAP=$([[ -n "$REGIONS" ]] && echo no || echo yes) \
      BASEMAP_REGIONS="${REGIONS:-}" SHIP_GNAF="$GNAF" \
      SHIP_WALKGRAPH="$WALKG" SHIP_PLACES="$PLACES" TARGET_NAME="$TARGET" \
        "${HERE}/release-vps.sh" "$HOST"   # marks image + basemaps + data DBs
    fi
    if has image; then DONE+=(image); fi
    for r in $REGIONS; do DONE+=("basemap-${r}"); done
    if [[ "$GNAF" == "yes" ]]; then DONE+=(gnaf-db); fi
    if [[ "$WALKG" == "yes" ]]; then DONE+=(walkgraph-db); fi
    if [[ "$PLACES" == "yes" ]]; then DONE+=(places-db); fi
  fi

  # 2. Unit files — after the image (a restart rides on new units anyway),
  #    before the enables (they sed their env into whatever units are live).
  if has quadlet-units; then
    if [[ "$DRY" == "yes" ]]; then
      echo "  PLAN quadlet-units: sync repo units to ${HOST}, preserving injected keys/env, daemon-reload + restart"
    else
      sync_quadlet_units
      "${HERE}/mark-deployed.sh" "$TARGET" quadlet-units
    fi
    DONE+=(quadlet-units)
  fi

  # 3. Region enablement (keys from keys.env; missing key = skip, stays pending).
  if has enable-syd-nsw; then
    if [[ -z "${NSW_KEY:-}" ]]; then
      echo "  !! enable-syd-nsw needs NSW_KEY in ~/.config/translink/keys.env — left pending."
    elif [[ "$DRY" == "yes" ]]; then
      echo "  PLAN enable-syd-nsw: run enable-syd-vps.sh (SYD_*+NSW_* env, nsw ingest, ingest-quadlet key)"
    else
      TARGET_NAME="$TARGET" "${HERE}/enable-syd-vps.sh" "$HOST"   # marks itself
    fi
    DONE+=(enable-syd-nsw)
  fi
  if has enable-mel; then
    if [[ -z "${VIC_KEY:-}" ]]; then
      echo "  !! enable-mel needs VIC_KEY in ~/.config/translink/keys.env — left pending."
    elif [[ "$DRY" == "yes" ]]; then
      echo "  PLAN enable-mel: run enable-mel-vps.sh"
    else
      TARGET_NAME="$TARGET" "${HERE}/enable-mel-vps.sh" "$HOST"   # marks itself
    fi
    DONE+=(enable-mel)
  fi
fi

# Anything pending that nothing above knows how to deploy.
for c in ${PENDING[@]+"${PENDING[@]}"}; do
  known=no
  for d in ${DONE[@]+"${DONE[@]}"}; do
    if [[ "$c" == "$d" ]]; then known=yes; fi
  done
  if [[ "$known" == "no" ]]; then LEFTOVER+=("$c"); fi
done
if [[ ${#LEFTOVER[@]} -gt 0 ]]; then
  echo "  !! no automated deploy path for: ${LEFTOVER[*]} — do it by hand, then:"
  echo "     ./deploy/mark-deployed.sh ${TARGET} ${LEFTOVER[*]}"
fi

echo
"${HERE}/status.sh"
