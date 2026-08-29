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
echo "==> restart the autopilot container"
ssh charon "sudo docker restart factorio-autopilot"
echo "==> deployed. status: ssh charon cat $CHARON_DIR/status.json"
