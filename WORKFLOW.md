# Factorio autopilot: contributor workflow

## Code changes ship via PR -> merge -> deploy

The autopilot code is the source of truth in this repo (`github.com/sutonimh/factorio-rcon-bridge`).
The server + autopilot run on **Charon** (Tailscale `100.100.199.83`). The Mac only edits + deploys.

**Never** `scp` ad-hoc edits straight to Charon. Every code change goes:

1. **Branch** off `main`:  `git checkout -b <short-name>`
2. **Edit + commit** locally (`~/code/factorio`).
3. **Push + open a PR** (gh as the `sutonimh` GitHub account):
   `git push -u origin <short-name> && gh pr create --fill`
4. **Merge** the PR (CI/green if configured; this is a personal repo so self-merge is fine).
5. **Deploy**:  `./deploy.sh`
   - checks out `main`, `git pull --ff-only` (the merged change),
   - `scp`s the runnable code (`*.py`, `*.json`) to `charon:/mnt/user/appdata/factorio-autopilot/`,
   - `ssh charon sudo docker restart factorio-autopilot`.
   Charon needs no GitHub auth: the Mac is the deploy source.

Live RCON steering (issuing one-off `/sc` commands to direct derpface, debug, or hand-build) is
NOT a code change and does not need a PR. Only changes to the tracked code do.

## Runtime files are gitignored
`status.json`, `state-db.json`, `autopilot.log`, `base-snapshot.json` are written by the autopilot
on Charon. They are gitignored so `deploy.sh`'s scp + the autopilot's writes never fight git.

## Monitoring
- `ssh charon cat /mnt/user/appdata/factorio-autopilot/status.json`  (heartbeat)
- `ssh charon tail -50 /mnt/user/appdata/factorio-autopilot/autopilot.log`
- RCON: `FACTORIO_RCON_HOST=100.100.199.83 FACTORIO_RCON_PORT=27015 python3 rcon.py "/sc ..."`
