# factorio-rcon-bridge

A live RCON bridge for driving a running Factorio (2.x, Space Age) game from the
command line: read game state, place blueprints, run console commands, and drive
the player character on "autopilot" using only real in-world resources (no item
spawning, no god-mode).

Built to co-pilot a single-player save by hosting it as a local headless server
with RCON enabled, then connecting as a normal multiplayer client.

## Files

- `serve.sh` — launches a save as a local headless server with RCON on
  `127.0.0.1:27015`. Runs on its own data dir (`~/factorio-server-data`) so it
  does not fight the Steam GUI client for the default data-dir lock.
- `rcon.py` — minimal Source-RCON client. `python3 rcon.py "<command>"` or pipe a
  command on stdin. `--ping` for a connectivity check.
- `autopilot.py` — primitives for driving the player character: `walk`, `mine`
  (deplete-and-insert, conservative), `goto-mine`, `pos`.
- `rcon.pass` — generated RCON password (gitignored, 0600).

## Usage

```sh
# 1. Save your single-player game, quit to desktop (releases the data-dir lock).
# 2. Host it headless with RCON:
./serve.sh <save-name>          # e.g. ./serve.sh suto   (default: newest save)

# 3. In Factorio: Multiplayer > Connect to address > localhost
# 4. Drive it:
python3 rcon.py --ping
python3 rcon.py "/sc rcon.print(game.tick)"
python3 autopilot.py goto-mine coal 40
```

## Factorio command notes

- `/sc <lua>` — silent-command: run Lua, no console echo. Use `rcon.print(x)` to
  return data over RCON.
- `/c <lua>` — command (echoes; disables achievements).
- Hosting multiplayer and running any console command both disable Steam
  achievements, so this is for sandbox/co-pilot play, not achievement runs.

## Safety

- RCON binds to `127.0.0.1` only (localhost).
- `rcon.pass` is gitignored. Do not commit it.
- `autopilot.py mine` depletes the real resource patch by exactly the amount it
  adds to inventory: no resources are created from nothing.
