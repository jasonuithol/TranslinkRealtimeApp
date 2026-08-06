#!/usr/bin/env bash
#
# Serve the built region packs on :8010, so the app's download flow can be
# tested against real files without publishing anything.
#
#   ./tools/packserve.sh          start (or restart) it
#   ./tools/packserve.sh stop     stop it
#
# Then open the app with ?packbase=http://localhost:8010 — packs are only
# offered to a browser that is pointed at a host on purpose.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8010}"

if [[ "${1:-}" == "stop" ]]; then
  podman rm -f packserve >/dev/null 2>&1 || true
  echo "stopped"
  exit 0
fi

if [[ ! -f "$ROOT/packs/dist/index.json" ]]; then
  echo "no packs built yet — run build_pack.py with --dist $ROOT/packs/dist" >&2
  exit 1
fi

podman rm -f packserve >/dev/null 2>&1 || true
podman run -d --restart=unless-stopped --name packserve -p "$PORT:80" \
  -v "$ROOT/packs/dist:/usr/share/nginx/html:ro,z" \
  -v "$ROOT/tools/packserve.conf:/etc/nginx/conf.d/default.conf:ro,z" \
  docker.io/library/nginx:alpine >/dev/null
sleep 1
python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
d = json.load(urllib.request.urlopen(f"http://localhost:{port}/index.json"))
print(f"serving on :{port}")
for r in d["regions"]:
    print(f"  {r['id']:4} {r['name'][:40]:42} {r['bytes']/1e6:6.0f} MB")
PY
