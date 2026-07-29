#!/usr/bin/env bash
#
# What still needs deploying, per target:  ./deploy/status.sh
# Reads deploy/state/*.yaml and prints every pending/blocked component.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in "${HERE}"/state/*.yaml; do
  target="$(basename "$f" .yaml)"
  host="$(sed -n 's/^host: //p' "$f")"
  echo "== ${target} (${host}) =="
  if grep -E "status: (pending|blocked)" "$f" >/dev/null; then
    grep -E "status: (pending|blocked)" "$f" \
      | sed -E 's/^  ([a-z0-9-]+): \{ status: ([a-z]+)[^}]*reason: "([^"]*)".*/  \2  \1 — \3/;
                s/^  ([a-z0-9-]+): \{ status: ([a-z]+).*/  \2  \1/'
  else
    echo "  everything deployed"
  fi
  echo
done
