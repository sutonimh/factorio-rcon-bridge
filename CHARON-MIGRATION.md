# Migrate the Factorio server (+ autopilot) to Charon

> STATUS: **COMPLETE** (Phases 1-4). Server `factorio` + autopilot `factorio-autopilot` run on
> charon (Tailscale 100.100.199.83), both `restart: always`. derpface is autonomous 24/7.
> Code lives at `/mnt/user/appdata/factorio-autopilot/` on Charon. Code changes ship via
> **PR -> merge -> `./deploy.sh`** (see WORKFLOW.md); no more ad-hoc `scp`. Live RCON steering
> needs no PR.
> Monitor: `ssh charon cat /mnt/user/appdata/factorio-autopilot/status.json`. Steer: RCON :27015.
> Restart autopilot after a code deploy: `ssh charon sudo docker restart factorio-autopilot`.


Goal: move the headless Factorio server off the Mac onto **charon** (Unraid, Docker 29.5.2,
20 CPUs, 16T free, Tailscale), run it 24/7, drive it with the autopilot from there, and support
TWO characters in the world: the autonomous **derpface** (autopilot-controlled) and Seth's own
player character that he controls when he logs in.

## Current state (source)
- Headless server on the Mac (Steam build), `--config ~/factorio-server-data/config/config.ini`,
  save `~/Library/Application Support/factorio/saves/suto-fresh.zip` (~860 KB, **map v2.1.8**).
- Mods: `base, elevated-rails, quality, recycler, space-age` (the **Space Age DLC** + recycler).
- RCON `127.0.0.1:27015` (pass in `~/code/factorio/rcon.pass`). Game port 34197/udp.
- Autopilot: Python in `~/code/factorio/` (autopilot.py, bootstrap.py, gamedb.py, techdb.py),
  talks RCON to localhost. The GUI client (Seth's Mac) joins `localhost` to watch/play.

## Target on Charon
- A Docker container from **`factoriotools/factorio`** (well-maintained headless image: handles
  RCON, server-settings, mods, save selection, version pinning), pinned to **2.1.8**.
- Persistent data under `/mnt/user/appdata/factorio/` (`/saves`, `/mods`, `/config`,
  `server-settings.json`, `rcon.pwd`). Bind-mounted into the container.
- `restart: always` + Unraid Autostart (Charon convention: every container runs at array start).
- Exposed over **Tailscale only** (no public ports): `34197/udp` (game), `27015/tcp` (RCON).

## Phase 1 — stand up the server on Charon
1. `mkdir -p /mnt/user/appdata/factorio/{saves,mods,config}` on charon.
2. **Space Age is paid DLC** — the headless image ships only the free base. Copy the DLC mod
   files from the Mac's Steam install into the container's `/mods` (so they load):
   - From `.../Steam/steamapps/common/Factorio/factorio.app/Contents/data/` the DLC ships as
     built-in mods (`space-age`, `elevated-rails`, `quality`); `recycler` too. Copy those mod
     folders/zips + `mod-list.json` to `/mnt/user/appdata/factorio/mods/`. (Verify the image
     version's expected mod format; pin image tag to `2.1.8`.)
3. `scp` the save: `suto-fresh.zip` -> `/mnt/user/appdata/factorio/saves/`.
4. `server-settings.json`: name, `visibility.public=false`, `visibility.lan=true`, password
   optional (Tailscale is the perimeter); set the same RCON password as `rcon.pass`.
5. `docker compose up -d` (compose snippet below). Confirm it loads `suto-fresh.zip` at 2.1.8 with
   all 5 mods, RCON answers, and the GUI client can join `charon:34197` over Tailscale.

```yaml
# /mnt/user/appdata/factorio/docker-compose.yml
services:
  factorio:
    image: factoriotools/factorio:2.1.8
    container_name: factorio
    restart: always
    ports: ["34197:34197/udp", "27015:27015/tcp"]   # bind to the Tailscale iface only
    volumes: ["/mnt/user/appdata/factorio:/factorio"]
    environment: { SAVE_NAME: suto-fresh, RCON_PORT: 27015 }
```

## Phase 2 — move the autopilot onto Charon (as a container)
The walk loop polls RCON tightly, so latency matters → run the autopilot **on charon** next to
the server. NOTE: the Unraid HOST has no `python3`, so the autopilot runs as its OWN container
(matches Charon's container convention), on the same docker network as the server.
- Code lives at `/mnt/user/appdata/factorio/autopilot/` (already copied). The Mac Claude session
  edits it there (over Tailscale ssh) and runs direct actions; charon is canonical.
- A `autopilot` service (`python:3.12-slim`, mounts the code, `restart: always`) joins the
  compose; `FACTORIO_RCON_HOST=factorio` (the server container's name) so `rcon.py` reaches RCON
  over the docker network. `depends_on: factorio`.
- Command: `python3 -c "import bootstrap as B; B.scout(); B.maintain()"` once derpface exists.
- (ROADMAP: persistent authenticated RCON socket — cheap + fast now that it's container-local.)
- The Mac is just a viewer (GUI client joins `charon:34197`) + the Claude dev session.

## Phase 3 — TWO characters: derpface (autonomous) + Seth's player
Today the autopilot drives `game.players[1]` — i.e. whoever's connected. For two characters:
- **derpface** = a dedicated character ENTITY the autopilot owns, independent of any connected
  client: `surface.create_entity{name='character'}`, kept in a script global
  (`storage.derpface`), with a render-text label "derpface" following it. The autopilot drives
  THIS entity (its `walking_state`, position, mining, inventory), NOT `game.players[1]`.
- **Seth's player**: when Seth's GUI client connects he's `game.players[1]` with his own
  character; he plays normally, alongside derpface.
- Code change (localized but real): replace `game.players[1]` in `autopilot.py` with a `bot()`
  accessor returning the derpface character (by `unit_number`), and use `char.get_main_inventory()`
  / `char.walking_state=...` / `char.surface` off the entity. Verify server-side `walking_state`
  animates a player-less character (it should; if not, fall back to teleport-step movement for
  derpface only). One-time setup command creates derpface, gives it the starting inventory, and
  parks it at spawn.
- Result: log in any time and your character works the base next to derpface; derpface keeps
  running even with no client connected (true 24/7 autonomy, the win of moving to charon).

## Phase 4 — 24/7 autonomy + status reporting to a Claude session
The point of charon is that **derpface keeps playing when no one is logged in**, and a Claude
session can check on it and steer it at any time.
- **Always-on autonomy**: the autopilot (`bootstrap.maintain()`) runs as its own
  always-restart container/service alongside the server. derpface is a player-less character, so
  it builds/mines/researches with zero clients connected. Server + autopilot both autostart.
- **Status that a Claude session reads** (so a future Claude session can pick up the project and
  know exactly where derpface is, without watching live):
  - `state-db.json` (already exists): all structures + chest inventories, refreshed each loop.
  - **`status.json`** (add): a compact heartbeat the loop writes every few seconds — timestamp,
    current research + %, power OK?, supply gates, labs running, build-queue, last N log lines,
    and any error/stall. This is the "report status" surface.
  - **`autopilot.log`** (add): replace the silent `except: pass` blocks with a rolling log file
    (a ROADMAP item anyway) so stalls are visible.
  - These live on charon under `/mnt/user/appdata/factorio/` (and/or the repo), reachable by a
    Claude session over Tailscale (`ssh charon cat .../status.json`) or a synced path.
- **How a Claude session works with derpface**: read `status.json`/`state-db.json` for state,
  run `python3 techdb.py`/`gamedb` queries, issue live commands or edit the autopilot over RCON
  (`charon:27015`) — exactly like this session does, but against the charon server. The session
  directs derpface (queue builds, fix issues); derpface executes 24/7 between sessions.
- **Optional push**: on milestones (research done) or problems (power death, stuck >N min) send a
  PushNotification / Slack message so Seth/Claude get pinged without polling.
- **Hand-off contract**: a Claude session resuming the project reads `CHARON-MIGRATION.md` +
  `ROADMAP.md` + `GOTCHAS.md` + `status.json`, confirms derpface is alive (RCON ping + recent
  `status.json` ts), then continues down the roadmap.

## Networking / security
- Tailscale only; bind container ports to the tailnet IP. No port-forwarding.
- RCON password = `rcon.pass`; keep it in the container `/config`, not in the repo.
- Achievements already disabled (RCON/console) — no change.

## Resources / UPS
- Factorio is single-thread-per-surface CPU-bound. 20 cores on charon is ample for a bootstrap
  base; watch UPS as the base grows (the megabase phase is the real load). Charon is "I/O-bound,
  not CPU-starved," so headroom is fine. Pin/limit if it competes with CI/dev stacks.

## Rollback
- Keep the Mac server install intact until charon is proven. The save is the source of truth;
  copy back `suto-fresh.zip` and relaunch `serve.sh` on the Mac to revert.

## Open decisions for Seth
1. Run the autopilot as a **2nd container** vs a systemd/script supervisor on charon?
2. derpface movement: confirm player-less `walking_state` works, or accept teleport-step for it.
3. Server password on top of Tailscale, or Tailscale-only?
4. Pin a Factorio image tag (2.1.8) or track latest (and re-verify mods/save each bump)?
