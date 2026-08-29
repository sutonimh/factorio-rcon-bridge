#!/usr/bin/env python3
"""Vanilla-native train dispatch over RCON (MEGABASE-V2-DESIGN section 5 phase 3).

NO LTN: dispatch = base-2.0 train GROUPS (one shared schedule per item) + schedule
INTERRUPTS ("cargo empty -> [item] pickup", "cargo full -> [item] dropoff", low fuel ->
"refuel") + per-stop trains_limit / priority. This module only manages schedules, groups,
and stops of already-built infrastructure — rails and stops come from blueprints.

The group schedule is INTERRUPT-ONLY (no fixed records): a train in the group idles until
an interrupt fires, drives to the named stop, waits out that stop's wait_conditions, and
re-evaluates. Generic item shuttles = name the stops "<item> pickup" / "<item> dropoff"
(stop_names()), cap concurrency per stop with set_stop(limit=...), steer preference with
set_stop(priority=...). Vanilla groups only exist through member trains (there is no
standalone create-group API), so create_group_schedule needs a seed train: pass train_id,
or have a train already assigned to the group.

API verification (Factorio 2.1.17):
  LIVE-verified (read-only RCON against the charon server): game.train_manager exists;
    train_manager.get_train_by_id / get_trains / get_train_stops are functions;
    get_trains{} (empty filter) works; defines.train_state exists;
    surface.find_entities_filtered{type='train-stop'} works.
  DOCS-only (lua-api.factorio.com/latest — the live map had 0 trains and 0 stops, so
    instance members could not be probed):
    - LuaTrain.get_schedule() -> LuaSchedule; LuaTrain.group (r/w string — setting it
      joins/creates the group and applies the group schedule); LuaTrain.station,
      get_contents() (returns [{name,count,quality}] entries, GOTCHAS inventory rule).
    - LuaSchedule: add_interrupt{name,conditions,targets,inside_interrupt} (no-op when
      the name already exists), clear_interrupts(), get_interrupts(), get_records(),
      remove_record{schedule_index=i}, interrupt_count, current, group. LuaSchedule
      REPLACED the 2.0-era table-based LuaTrain.schedule as the mutation surface.
    - ScheduleRecord {station, wait_conditions, temporary, ...}; WaitConditionType
      includes 'empty', 'full', 'fuel_item_count_all', 'fuel_full'.
    - trains_limit / train_stop_priority / trains_count are LuaEntity attributes in 2.x
      (the LuaTrainStop class page 404s; get_train_stops returns LuaEntity).
  UNCERTAIN (verify on the first live run with a real train):
    - the fuel interrupt's condition payload ({comparator,constant,first_signal}) is
      derived from 2.0 interrupt blueprint JSON, not spelled out in the API reference;
    - interrupt-only schedules (zero records) firing from idle is 2.0 release-note
      behavior, not explicit in the LuaSchedule docs.

Reads whose payload can exceed one RCON response (~3-4KB) go through world._chunked (the
architect.py storage-slice pattern). Writes here are schedule/stop mutations only — this
module never creates or destroys entities.
"""
import json
import re

import rcon
import world

REFUEL_STOP = "refuel"             # stop name the low-fuel interrupt targets
FUEL_ITEM = "coal"                 # default fuel the low-fuel condition counts
FUEL_LOW = 300                     # fire refuel when total fuel items across locos < this
_ITEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")   # factorio item names: lowercase-kebab


class TrainsError(Exception):
    """A dispatch op failed (no such train, no matching stop, ...); message = diagnostic."""


def _q(s):
    """Quote a string into a Lua single-quoted literal."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _item(item):
    if not _ITEM_RE.match(item or ""):
        raise ValueError("not a factorio item name: %r" % (item,))
    return item


def stop_names(item):
    """The stable stop-name pair for an item: ('<item> pickup', '<item> dropoff').
    Blueprinted stations must use exactly these names for dispatch to find them."""
    _item(item)
    return ("%s pickup" % item, "%s dropoff" % item)


# --------------------------------------------------------------------------- groups
def _interrupt_lua(name, conditions, station, wait):
    """One sch.add_interrupt{...} statement. conditions/wait are raw Lua table bodies."""
    return ("sch.add_interrupt{name=%s,conditions={%s},"
            "targets={{station=%s,wait_conditions={%s}}}}"
            % (_q(name), conditions, _q(station), wait))


def create_group_schedule(group_name, item, train_id=None, refuel=True,
                          fuel_item=FUEL_ITEM, fuel_low=FUEL_LOW):
    """Build/ensure the interrupt-based shuttle schedule for `group_name` hauling `item`:
    cargo empty -> '<item> pickup' (wait full), cargo full -> '<item> dropoff' (wait
    empty), plus (refuel=True) fuel<fuel_low -> 'refuel' (wait fuel_full,
    inside_interrupt so it can fire mid-interrupt). The schedule is rebuilt
    deterministically (records cleared, interrupts cleared + re-added) and is SHARED by
    every train in the group. Needs a seed train: train_id, or any train already in the
    group. Returns {'group', 'interrupts'}."""
    _item(item)
    _item(fuel_item)
    pick, drop = stop_names(item)
    if train_id is not None:
        find = "local t=tm.get_train_by_id(%d);" % int(train_id)
    else:
        find = ("local t; for _,c in pairs(tm.get_trains{}) do"
                " local ok,g=pcall(function() return c.get_schedule().group end);"
                " if ok and g==%s then t=c; break end end;" % _q(group_name))
    ints = [
        _interrupt_lua(pick, "{type='empty'}", pick, "{type='full'}"),
        _interrupt_lua(drop, "{type='full'}", drop, "{type='empty'}"),
    ]
    if refuel:
        ints.append(
            "sch.add_interrupt{name=%s,inside_interrupt=true,"
            "conditions={{type='fuel_item_count_all',condition="
            "{comparator='<',constant=%d,first_signal={type='item',name=%s}}}},"
            "targets={{station=%s,wait_conditions={{type='fuel_full'}}}}}"
            % (_q(REFUEL_STOP), int(fuel_low), _q(fuel_item), _q(REFUEL_STOP)))
    out = rcon.run(
        "/sc local tm=game.train_manager; " + find +
        "if not (t and t.valid) then rcon.print('NO_TRAIN') else"
        "  t.group=%s;" % _q(group_name) +
        "  local sch=t.get_schedule();"
        "  sch.clear_interrupts();"
        # deterministic rebuild: drop every fixed record -> interrupt-only schedule
        "  while true do local r=sch.get_records();"
        "    if not r or #r==0 then break end;"
        "    sch.remove_record{schedule_index=1} end;"
        "  " + " ".join(ints) +
        "  rcon.print('GROUP '..sch.group..' interrupts='..sch.interrupt_count)"
        " end").strip()
    if out == "NO_TRAIN":
        raise TrainsError("create_group_schedule(%r): no seed train (train_id=%r and no "
                          "train already in the group)" % (group_name, train_id))
    m = re.match(r"GROUP (.*) interrupts=(\d+)$", out)
    if not m:
        raise TrainsError("create_group_schedule(%r) returned %r" % (group_name, out))
    return {"group": m.group(1), "interrupts": int(m.group(2))}


def assign_train(train_id, group_name):
    """Put a train into a group (it adopts the group's shared schedule). Verified after
    write (readback of the schedule's group). Returns the group name."""
    out = rcon.run(
        "/sc local t=game.train_manager.get_train_by_id(%d);"
        "if not (t and t.valid) then rcon.print('NO_TRAIN') else"
        "  t.group=%s;"
        "  rcon.print('OK '..tostring(t.get_schedule().group))"
        " end" % (int(train_id), _q(group_name))).strip()
    if out == "NO_TRAIN":
        raise TrainsError("no train with id %s" % train_id)
    if out != "OK %s" % group_name:
        raise TrainsError("assign_train(%s,%r): group did not stick (readback: %r)"
                          % (train_id, group_name, out))
    return group_name


# --------------------------------------------------------------------------- reads
def list_trains():
    """All trains -> [{id, group, state, station, cargo:{name:count}}]. state/station
    resolved to strings server-side. RCON READ ONLY; chunked (payload scales with fleet)."""
    lua = (
        "local SN={}; for k,v in pairs(defines.train_state) do SN[v]=k end;"
        "local out={};"
        "for _,t in pairs(game.train_manager.get_trains{}) do"
        "  local g=''; local ok,s=pcall(function() return t.get_schedule().group end);"
        "  if ok and s then g=s end;"
        "  local st=''; local oks,stn=pcall(function() return t.station end);"
        "  if oks and stn and stn.valid then st=stn.backer_name or '' end;"
        "  local cargo={};"
        "  for _,c in pairs(t.get_contents()) do cargo[c.name]=(cargo[c.name] or 0)+c.count end;"
        "  out[#out+1]={id=t.id,group=g,state=SN[t.state] or tostring(t.state),"
        "               station=st,cargo=cargo}"
        " end;"
        "if #out==0 then storage._world='[]' else storage._world=helpers.table_to_json(out) end;"
        "rcon.print(#storage._world)"
    )
    trains = json.loads(world._chunked(lua))
    for t in trains:
        if not isinstance(t.get("cargo"), dict):        # empty lua table -> [] in json
            t["cargo"] = {}
    return trains


def train_status(train_id):
    """One train in detail -> {id, group, state, station, cargo, current, records:[stop
    names], interrupts:[names]}. RCON READ ONLY; chunked."""
    lua = (
        "local SN={}; for k,v in pairs(defines.train_state) do SN[v]=k end;"
        "local t=game.train_manager.get_train_by_id(%d);" % int(train_id) +
        "if not (t and t.valid) then storage._world=helpers.table_to_json({err='NO_TRAIN'})"
        " else"
        "  local sch=t.get_schedule();"
        "  local recs={};"
        "  for _,r in pairs(sch.get_records() or {}) do recs[#recs+1]=r.station or 'rail' end;"
        "  local ints={};"
        "  for _,i in pairs(sch.get_interrupts() or {}) do ints[#ints+1]=i.name end;"
        "  local st=''; local oks,stn=pcall(function() return t.station end);"
        "  if oks and stn and stn.valid then st=stn.backer_name or '' end;"
        "  local cargo={};"
        "  for _,c in pairs(t.get_contents()) do cargo[c.name]=(cargo[c.name] or 0)+c.count end;"
        "  storage._world=helpers.table_to_json({id=t.id,group=sch.group or '',"
        "    state=SN[t.state] or tostring(t.state),station=st,cargo=cargo,"
        "    current=sch.current,records=recs,interrupts=ints})"
        " end;"
        "rcon.print(#storage._world)"
    )
    st = json.loads(world._chunked(lua))
    if isinstance(st, list):                            # all-empty lua table -> []
        st = {}
    if st.get("err") == "NO_TRAIN":
        raise TrainsError("no train with id %s" % train_id)
    if not isinstance(st.get("cargo"), dict):
        st["cargo"] = {}
    for k in ("records", "interrupts"):
        if not isinstance(st.get(k), list):
            st[k] = []
    return st


# --------------------------------------------------------------------------- stops
def set_stop(name, limit=None, priority=None):
    """Set trains_limit and/or train_stop_priority on EVERY stop named `name` (stops share
    a name to pool; the limit is per physical stop). Returns how many stops matched;
    raises TrainsError when none did (a silently-missed stop name is a dispatch bug)."""
    if limit is None and priority is None:
        raise ValueError("set_stop(%r): give limit and/or priority" % name)
    sets = []
    if limit is not None:
        if int(limit) < 0:
            raise ValueError("limit must be >= 0, got %r" % (limit,))
        sets.append("e.trains_limit=%d;" % int(limit))
    if priority is not None:
        if not 0 <= int(priority) <= 255:
            raise ValueError("priority must be 0..255, got %r" % (priority,))
        sets.append("e.train_stop_priority=%d;" % int(priority))
    out = rcon.run(
        "/sc local n=0;"
        "for _,e in pairs(game.surfaces[1].find_entities_filtered{type='train-stop'}) do"
        "  if e.valid and e.backer_name==%s then %s n=n+1 end"
        " end;"
        "rcon.print(n)" % (_q(name), " ".join(sets))).strip()
    n = int(out or "0")
    if n == 0:
        raise TrainsError("set_stop(%r): no train stop with that name" % name)
    return n
