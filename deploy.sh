#!/usr/bin/env bash
# Deploy the autopilot to Charon AFTER a PR is merged to main.
# Workflow: branch -> PR -> merge to main -> ./deploy.sh
# The Mac (which has GitHub access) pulls merged main and scp's the runnable code to Charon,
# then restarts the autopilot container. Charon needs no GitHub auth.
set -euo pipefail
CHARON_DIR=/mnt/user/appdata/factorio-autopilot
echo "==> git: checkout main + pull merged changes"
git checkout main
git pull --ff-only
echo "==> scp code + static tech DB to charon:$CHARON_DIR (NOT runtime json)"
# Only ship code (*.py) + the static tech DB. status.json / state-db.json / base-snapshot.json
# are LIVE runtime state written on Charon: never overwrite them with stale local copies.
scp ./*.py ./tech-tree.json ./dashboard.html charon:"$CHARON_DIR"/
# v2 assets: vendored Lua + the curated blueprint library (static, versioned in git)
ssh charon "mkdir -p $CHARON_DIR/lua $CHARON_DIR/blueprints/library"
scp ./lua/*.lua charon:"$CHARON_DIR"/lua/
scp ./blueprints/library/* charon:"$CHARON_DIR"/blueprints/library/
# GOTCHAS: a process kill mid-walk leaves walking_state=true (character runs forever).
# Best-effort stop before the restart; ignore failures (server may be down).
FACTORIO_RCON_HOST=100.100.199.83 FACTORIO_RCON_PORT=27015 python3 rcon.py "/sc if storage.derpface and storage.derpface.valid then storage.derpface.walking_state={walking=false} end" || true
echo "==> restart the autopilot + dashboard containers"
# factorio-dash is a SEPARATE container: it re-reads dashboard.html per request, but dashboard.py
# is loaded once at start, so a shipped dashboard.py did nothing until this restart existed.
ssh charon "sudo docker restart factorio-autopilot factorio-dash"
echo "==> deployed. status: ssh charon cat $CHARON_DIR/status.json"
