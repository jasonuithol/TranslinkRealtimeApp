#!/usr/bin/env bash
#
# Build the Transport Honker debug APK in a throwaway Android SDK container
# (no toolchain on the host). From the repo root:
#
#   ./mobile/build-apk.sh
#
# Mounts, and why each matters:
#   /app    the Capacitor project
#   /root/.gradle   named volume — keeps the ~2 min dependency download
#                   down to a ~6 s rebuild
#   /keys   ~/.config/translink — holds honker-debug.keystore so every
#           build is signed with the SAME key. Without it Android rejects
#           each new APK as "conflicts with an existing package", because
#           a fresh throwaway key means a fresh identity.
# --user root: rootless podman maps container root to you, and the SDK
# image's own user cannot write to the mounted project.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="${HOME}/.config/translink"
IMAGE="docker.io/mobiledevops/android-sdk-image:34.0.0"

if [[ ! -f "${KEYS}/honker-debug.keystore" ]]; then
  echo "WARNING: no ${KEYS}/honker-debug.keystore — this build gets a" >&2
  echo "throwaway signature and will NOT install over an existing copy." >&2
fi

podman run --rm --user root \
  -v "${HERE}:/app:Z" \
  -v honker-gradle:/root/.gradle \
  -v "${KEYS}:/keys:ro" \
  -w /app/android "$IMAGE" \
  ./gradlew --no-daemon assembleDebug

APK="${HERE}/android/app/build/outputs/apk/debug/app-debug.apk"
echo "==> ${APK}"
ls -lh "$APK"
