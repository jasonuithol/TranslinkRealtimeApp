#!/usr/bin/env bash
#
# Rebuild mobile/www from the web app in static/.
#
#   ./mobile/bundle-www.sh [api-base-url]
#
# The UI ships INSIDE the app (instant, no round trip for assets); the data
# still comes from the API at api-base-url — offline city packs are the next
# milestone, and this is the step before it. A tiny shim rewrites the
# client's absolute /api and /basemap requests onto that host; /static stays
# local, so fonts, glyphs, styles and the honk load from the bundle.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASE="${1:-https://nextarrival.app}"

rm -rf "$HERE/www"
mkdir -p "$HERE/www/static"
cp -r "$ROOT/static/." "$HERE/www/static/"

# The build stamp travels with the bundle: the settings panel shows it, so
# "did the new APK actually install?" is answerable by looking.
BUILD="$(git -C "$ROOT" show -s --format=%cd --date=format:%Y.%m.%d HEAD)-$(git -C "$ROOT" rev-parse --short HEAD)"
python3 - "$ROOT/static/index.html" "$HERE/www/index.html" "$BASE" "$BUILD" <<'PY'
import sys
src, dst, base, build = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
html = open(src).read()
shim = '''<script>
window.__BUILD = "%s";
// Packaged-app shim: the UI is bundled, the data is not (yet). Any
// absolute /api or /basemap request is sent to the API host; everything
// else — /static/... — resolves inside the app bundle.
(function () {
  var BASE = "%s";
  var REMOTE = /^\\/(api|basemap)\\//;
  function remap(u) {
    try {
      var p = new URL(u, location.href);
      if (p.origin === location.origin && REMOTE.test(p.pathname)) {
        return BASE + p.pathname + p.search;
      }
    } catch (e) { /* not a URL we can parse: leave it alone */ }
    return u;
  }
  var orig = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === "string") return orig(remap(input), init);
    if (input && input.url) {
      var m = remap(input.url);
      if (m !== input.url) return orig(new Request(m, input), init);
    }
    return orig(input, init);
  };
})();
</script>
''' % (build, base)
assert "<head>" in html
open(dst, "w").write(html.replace("<head>", "<head>\n" + shim, 1))
print("bundled www/index.html -> API base", base)
PY

du -sh "$HERE/www"
