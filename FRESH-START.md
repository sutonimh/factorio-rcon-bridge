# Fresh-world strategy (what we learned the hard way; do it right next time)

We hand-built a patchwork base over a long session, hit it from every angle, and the
honest conclusion: **plan it around a main bus + blueprints from the very start, and
drive to construction robots so the blueprint book can be stamped and bot-built.** This
file is the strategic playbook; `GOTCHAS.md` has the tactical rules (read both).

## The core realization
- A faithful Nilaus/blueprint base is **meant to be stamped and built by construction
  robots**. Without bots, "following the blueprints" means hand-placing 300+ entities via
  create_entity - which floundered (agents over-analyzed; I made belt/inventory messes).
- So the real objective each playthrough is: **bootstrap cleanly -> reach construction
  robots -> stamp the blueprint book -> let bots build the real base.** Everything before
  bots is just the bootstrap; keep it simple and don't over-engineer it.

## Space Age gotchas that shaped the plan
- `oil-processing` is a **TRIGGER tech**: unlocks by mining crude oil with a pumpjack,
  NOT the science queue. Everything toward robots (plastics -> advanced-circuit ->
  chemical science -> battery -> electric-engine -> robotics -> construction-robotics)
  is gated on it, and the science packs need oil PRODUCTS as ingredients - so you must
  build the oil economy; you can't shortcut it.
- On this map the nearest crude oil was **440 tiles from spawn** (none within 300). On a
  fresh world, **scout for oil early** and factor its distance into base placement.
- `f.research_queue = {names}` silently rejected the whole list; `f.add_research(name)`
  one-at-a-time worked. Trigger techs can't be queued at all.

## Recommended fresh-world sequence
1. **Bootstrap, bus-first.** From the start lay a small **main bus** (start ~4 lanes:
   iron, copper, then green/red circuits). Smelt onto the bus. Pull science assemblers
   and labs OFF the bus - never script-feed labs (that was the patchwork mistake).
2. **Red+green science, automated** (a real assembler line off the bus, not hand-craft).
   Hand-crafting can't sustain >1 lab. Get ~4-8 labs fed off the bus.
3. **Scout + tap oil early.** Find the nearest crude oil; get a pumpjack running there
   (remote power or a short pipe run) to fire the oil-processing trigger ASAP.
4. **Build the oil economy** (refinery + chemical plants -> petroleum/plastic/sulfur ->
   advanced circuits -> chemical/blue science). This is the long pole; plan space for it.
5. **Research to `construction-robotics`** (add_research one tech at a time).
6. **Stamp the blueprint book** (blueprints/nilaus + blueprints/megabase on disk) and let
   bots build the real bus-fed base. THIS is the faithful blueprint base.

## What NOT to repeat (cost us this session)
- Don't hand-build a patchwork and bolt on script-feeding - plan the bus + zones first.
- Don't run a WALKING patrol (multiple sessions yanked the character; looked like
  teleporting). Patrol stands still; maintenance is server-side. ONE controller at a time.
- Don't area-destroy to tear down your own build (it wiped the coal line + iron feeder).
- Don't snake belts around everything - route DIRECT and cross with underground belts.
- Don't over-pull materials (buried the inventory under 5,497 copper, stalled all builds).
  Maintain free inventory space.
- Don't hand a 300-entity blueprint build to a single agent with a vague prompt - it
  over-analyzes and stalls. Either get bots first (then stamp), or build in small
  verified increments from one session.

## Tooling carried over (sutonimh/factorio-rcon-bridge)
autopilot.py (RCON helpers: walk/goto/build_belt/place/snapshot/manage_inventory/maintain),
patrol.py (stationary maintenance loop), tasks.py+tasks.json (live GUI note), rcon.py,
remove_redundant.py + optimize_poles.py (pole layout), blueprints/ (Nilaus + megabase books),
GOTCHAS.md (all tactical lessons), LAB-ARRAY-BUILD.md (bus-fed lab array spec).
