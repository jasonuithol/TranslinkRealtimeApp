# Sourced (not run) by the scripts that take API keys, so keys needn't be
# pasted on every invocation. Explicit arguments always win; this only fills
# the gaps from a local key store OUTSIDE the repo:
#
#   ~/.config/translink/keys.env        (override path with TRANSLINK_KEYS)
#
# Create it once, in a plain terminal (not in a Claude session — keep keys
# out of transcripts), then chmod 600:
#
#   mkdir -p ~/.config/translink
#   ${EDITOR:-nano} ~/.config/translink/keys.env
#     NSW_KEY=<TfNSW open data key — syd + nsw regions, static + realtime>
#     VIC_KEY=<VIC opendata key — mel realtime>
#     VPS_HOST=root@delphi-bogart.bnr.la   # optional: default deploy target
#   chmod 600 ~/.config/translink/keys.env
#
# NEVER commit keys; this repo's .gitignore has nothing to do with it because
# the file lives in $HOME. The VPS keeps its own copies in the quadlet env.
_KEYFILE="${TRANSLINK_KEYS:-$HOME/.config/translink/keys.env}"
if [[ -f "$_KEYFILE" ]]; then
  # shellcheck source=/dev/null
  source "$_KEYFILE"
fi
