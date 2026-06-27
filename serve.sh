#!/usr/bin/env bash
# Launch the current Factorio save as a local dedicated server with RCON enabled.
# Usage: ./serve.sh <save-name-without-.zip>   (default: latest save in the saves dir)
set -euo pipefail

BIN="/Users/sutonimh/Library/Application Support/Steam/steamapps/common/Factorio/factorio.app/Contents/MacOS/factorio"
SAVES="/Users/sutonimh/Library/Application Support/factorio/saves"
HERE="$(cd "$(dirname "$0")" && pwd)"
PASS_FILE="$HERE/rcon.pass"
PORT="${FACTORIO_RCON_PORT:-27015}"
LOG="$HERE/server.log"

if [[ ! -f "$PASS_FILE" ]]; then echo "missing $PASS_FILE" >&2; exit 1; fi

# Resolve save: arg, or newest .zip in saves (excluding autosaves if a real save exists)
if [[ "${1:-}" != "" ]]; then
  SAVE="$SAVES/$1.zip"
else
  SAVE="$(ls -t "$SAVES"/*.zip 2>/dev/null | grep -v '_autosave' | head -1)"
  [[ -z "$SAVE" ]] && SAVE="$(ls -t "$SAVES"/*.zip | head -1)"
fi
if [[ ! -f "$SAVE" ]]; then echo "save not found: $SAVE" >&2; exit 1; fi

echo "Serving: $SAVE"
echo "RCON:    127.0.0.1:$PORT  (password in $PASS_FILE)"
echo "Log:     $LOG"
echo "Join in-game: Multiplayer > Connect to address > localhost"
echo

# Run the server on its OWN data dir so it doesn't fight the Steam GUI client
# for the default data-dir lock (both on this same Mac).
SRV_CONFIG="$HOME/factorio-server-data/config/config.ini"

exec "$BIN" \
  --config "$SRV_CONFIG" \
  --start-server "$SAVE" \
  --rcon-bind "127.0.0.1:$PORT" \
  --rcon-password "$(cat "$PASS_FILE")" \
  >>"$LOG" 2>&1
