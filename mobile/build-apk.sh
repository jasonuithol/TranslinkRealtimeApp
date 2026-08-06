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
# release (default) or debug
VARIANT="${1:-release}"
IMAGE="docker.io/mobiledevops/android-sdk-image:34.0.0"

if [[ ! -f "${KEYS}/honker-debug.keystore" ]]; then
  echo "WARNING: no ${KEYS}/honker-debug.keystore — this build gets a" >&2
  echo "throwaway signature and will NOT install over an existing copy." >&2
fi

# Stage the web app into the Android project. This is what `npx cap copy`
# does, done directly because there is no node toolchain on the host — and
# it is NOT optional: gradle reads assets/public, never mobile/www, so
# skipping it silently rebuilds yesterday's UI and reports success. That
# happened, and it cost an afternoon of debugging a phone running code six
# hours older than the machine it was being compared against.
ASSETS="${HERE}/android/app/src/main/assets/public"
if [[ ! -f "${HERE}/www/index.html" ]]; then
  echo "no ${HERE}/www — run ./mobile/bundle-www.sh first" >&2
  exit 1
fi
rm -rf "$ASSETS"
cp -r "${HERE}/www" "$ASSETS"
echo "staged $(find "$ASSETS" -type f | wc -l) files into assets/public"

podman run --rm --user root \
  -v "${HERE}:/app:Z" \
  -v honker-gradle:/root/.gradle \
  -v "${KEYS}:/keys:ro" \
  -w /app/android "$IMAGE" \
  ./gradlew --no-daemon "assemble${VARIANT^}"

APK="${HERE}/android/app/build/outputs/apk/${VARIANT}/app-${VARIANT}.apk"
echo "==> ${APK}"
ls -lh "$APK"
