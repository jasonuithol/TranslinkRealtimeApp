#!/usr/bin/env bash
#
# Syntax-check the browser JS without installing Node on the host: an
# ephemeral node container mounts the working tree read-only and runs
# `node --check` over every file. The static/js files are classic scripts
# sharing one global scope, so a parse error in ANY of them takes the
# whole page down — check them all, every time.
#
#   ./tools/check-js.sh                    # checks static/js/*.js
#   ./tools/check-js.sh static/js/foo.js   # or just the named files
#
# Exit code: 0 = every file parses, 1 = at least one doesn't.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-docker.io/library/node:lts-alpine}"

FILES=("$@")
if [[ ${#FILES[@]} -eq 0 ]]; then
  mapfile -t FILES < <(cd "$REPO" && ls static/js/*.js)
fi

exec podman run --rm -v "$REPO":/repo:ro -w /repo "$IMAGE" sh -c '
  rc=0
  for f in "$@"; do
    if node --check "$f" 2>/tmp/err; then
      echo "ok   $f"
    else
      rc=1; echo "FAIL $f"; cat /tmp/err
    fi
  done
  exit $rc' sh "${FILES[@]}"
