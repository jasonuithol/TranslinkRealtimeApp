#!/usr/bin/env bash
#
# Flip components to deployed in a target's state file:
#
#   ./deploy/mark-deployed.sh <target> <component> [<component>...]
#   ./deploy/mark-deployed.sh vps image basemap-nsw
#
# The deploy scripts call this on success; call it yourself after a manual
# step (e.g. editing quadlet units on the VPS, or a hand-run podman
# auto-update). Unknown components warn rather than fail, so a deploy never
# dies over bookkeeping.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"; shift || true
FILE="${HERE}/state/${TARGET}.yaml"
if [[ -z "$TARGET" || $# -eq 0 ]]; then
  echo "Usage: $0 <target> <component>...   (targets: $(ls "${HERE}/state" | sed 's/\.yaml//' | tr '\n' ' '))" >&2
  exit 1
fi
[[ -f "$FILE" ]] || { echo "No state file ${FILE}" >&2; exit 1; }

TODAY="$(date +%F)"
for comp in "$@"; do
  if grep -q "^  ${comp}: " "$FILE"; then
    sed -i "s|^  ${comp}: .*|  ${comp}: { status: deployed, at: ${TODAY} }|" "$FILE"
  else
    echo "WARNING: component '${comp}' not tracked in ${FILE} — add it" >&2
  fi
done
sed -i "s|^updated: .*|updated: ${TODAY}|" "$FILE"
echo "==> ${TARGET}: marked deployed: $*"
