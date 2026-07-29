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
IMAGE_INPUTS=(app.py static ingest_gtfs.py requirements.txt Containerfile)

image_gate() {
  local dirty sha info run_id status conclusion
  dirty="$(git -C "$REPO" status --porcelain -- "${IMAGE_INPUTS[@]}")"
  if [[ -n "$dirty" ]]; then
    echo "!! image inputs have uncommitted changes — no image on GHCR can contain them:"
    sed 's/^/     /' <<<"$dirty"
    echo "   Commit + push, let CI publish, then rerun."
    return 1
  fi
  git -C "$REPO" fetch -q origin main || \
    echo "   (couldn't fetch origin — checking against last-known origin/main)"
  sha="$(git -C "$REPO" rev-parse HEAD)"
  if ! git -C "$REPO" merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
    if [[ "$DRY" == "yes" ]]; then
      echo "  GATE image: HEAD is not on origin/main — a real run would offer to push"
      return 0
    fi
    local a=""
    if [[ -t 0 ]]; then
      read -r -p "   HEAD isn't on origin/main. Push now? [y/N] " a
    fi
    if [[ "$a" == y* || "$a" == Y* ]]; then
      git -C "$REPO" push
      git -C "$REPO" fetch -q origin main
      if ! git -C "$REPO" merge-base --is-ancestor "$sha" origin/main; then
        echo "!! push didn't land HEAD on origin/main — sort the branch out and rerun."
        return 1
      fi
    else
      echo "!! image is pending but HEAD isn't pushed — push (CI publishes) and rerun."
      return 1
    fi
  fi
  if ! command -v gh >/dev/null 2>&1; then
    echo "   (gh not installed — can't verify CI; assuming GHCR :latest is current)"
    return 0
  fi
  # Find the CI run for HEAD; a just-pushed commit can take a moment to appear.
  for _ in 1 2 3 4 5 6; do
    info="$(gh run list --branch main --limit 20 \
             --json databaseId,headSha,status,conclusion \
             --jq "[.[] | select(.headSha==\"${sha}\")][0] | select(.) | \"\(.databaseId) \(.status) \(.conclusion)\"" \
             2>/dev/null || true)"
    if [[ -n "$info" ]]; then break; fi
    sleep 5
  done
  if [[ -z "$info" ]]; then
    echo "!! no CI run found for ${sha:0:7} — did the push trigger CI?"
    return 1
  fi
  read -r run_id status conclusion <<<"$info"
  if [[ "$status" != "completed" ]]; then
    if [[ "$DRY" == "yes" ]]; then
      echo "  GATE image: CI still running for ${sha:0:7} — a real run would wait for it"
      return 0
    fi
    echo "   CI still running for ${sha:0:7} — waiting…"
    gh run watch --exit-status "$run_id" >/dev/null || {
      echo "!! CI failed for ${sha:0:7} — fix it and rerun."; return 1; }
  elif [[ "$conclusion" != "success" ]]; then
    echo "!! CI for ${sha:0:7} concluded '${conclusion}' — fix it and rerun."
    return 1
  fi
  echo "   image gate OK: HEAD ${sha:0:7} pushed, CI green — GHCR :latest is current."
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
  IMAGE_REF="${IMAGE_REF:-ghcr.io/jasonuithol/translink-departures:latest}"
  if has image; then
    image_gate || exit 1
    if [[ "$DRY" == "yes" ]]; then
      echo "  PLAN image: podman pull ${IMAGE_REF}; recreate container 'translink' on :8000"
    else
      echo "==> Pulling ${IMAGE_REF}…"
      podman pull "$IMAGE_REF"
      podman rm -f translink >/dev/null 2>&1 || true
      podman run -d --name translink -p 8000:8000 -v translink-data:/data "$IMAGE_REF" >/dev/null
      for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null http://localhost:8000/ 2>/dev/null; then break; fi
        sleep 1
      done
      curl -fsS -o /dev/null http://localhost:8000/api/regions
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
  if has image || [[ -n "$REGIONS" ]]; then
    # The update pulls :latest even on a basemap-only run, so gate whenever
    # the image is what's being deployed here.
    if has image; then image_gate || exit 1; fi
    if [[ "$DRY" == "yes" ]]; then
      echo "  PLAN release: image pull + update on ${HOST}${REGIONS:+, shipping basemaps: ${REGIONS}}"
    else
      SKIP_BASEMAP=$([[ -n "$REGIONS" ]] && echo no || echo yes) \
      BASEMAP_REGIONS="${REGIONS:-}" TARGET_NAME="$TARGET" \
        "${HERE}/release-vps.sh" "$HOST"   # marks image + shipped basemaps
    fi
    if has image; then DONE+=(image); fi
    for r in $REGIONS; do DONE+=("basemap-${r}"); done
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
