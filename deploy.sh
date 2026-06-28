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
scp ./*.py ./tech-tree.json charon:"$CHARON_DIR"/
echo "==> restart the autopilot container"
ssh charon "sudo docker restart factorio-autopilot"
echo "==> deployed. status: ssh charon cat $CHARON_DIR/status.json"
