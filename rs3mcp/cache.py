"""
The cache database itself: its shape, arbitrary read only sql over it, one config resolved with its
params and morphs already decoded, and sprites.

Everything here is read only by construction. The database is opened mode=ro and tool_query refuses
anything that is not a single SELECT, so this cannot write to the cache even if asked to.
"""

import json

from .common import DB, SPRITES, MAX_ROWS, connect, check_readonly, rows_to_json

import os
import sqlite3

KINDS = {
    "npc": "npcs", "loc": "objects", "obj": "items", "item": "items", "struct": "structs",
    "enum": "enums", "quest": "Quest", "seq": "sequences", "sequence": "sequences",
    "iface": "interfaces", "spotanim": "spotanims", "achievement": "achievements",
    "dbrow": "dbrows", "dbtable": "dbtables",
}

NOTES = """\
Things that are easy to get wrong here:

- config.name is the config's code name (ABYSSAL_WHIP), from the name lists, and null without them.
  config.display is the in-game string. Display names are NOT unique - seven items are called
  "Abyssal whip". config.data is the decoded json, in the field names of rs3mcp/specs/<type>.ops.
- varbit.id is NEGATIVE here. A morph record stores the raw positive id, and the id in this
  database is -(raw + 1). Dropping the +1 lands on a real neighbouring var and reads as plausible,
  so it is silently wrong rather than an error.
- Multilocs are NOT loc-only: npcs do the same thing. data.morphs / morphsWide, where var is
  varbit<<16 | varp with 0xFFFF meaning unused, ids is the value-indexed child list and fallback
  the past-the-end child.
- The empty slot in that child list is a DIFFERENT number for the two types, and each one is a real
  id in the other's space, so reading it as a child is silent rather than an error. npcs use 65535,
  locs use 32767. Over 6000 multilocs of each: npcs never contain 32767, locs never contain 65535,
  and 32767 turned up 30,309 times in loc lists before the sentinel was fixed while also being loc 32767, DI2_TIGHTROPE_ANCHOR2.
  A tightrope anchor is not the appearance of thirty thousand multilocs. The highest real npc id is
  32741, just under the loc sentinel, which is presumably why the two differ at all.
- The entity in the world carries the PARENT id. Anything naming the child matches nothing and
  says nothing, so rs3_lookup reports "parents" on a child and decodes "multiloc"
  on a parent. 3,635 npc ids are children of something.
- A field named unkXX is read correctly and not understood yet. Loc animation is the sequence id
  and loc quests the required quest ids; both are confirmed.
- Items past 65535 use the tribyte families: stack1..9, and noteLink/noteTemplate, lentLink,
  boughtLink, shardLink. The old ushort equivalents have zero uses in modern RS3.
- Shop stock is in inv / inv_stock, decoded from invtype. Nothing anywhere
  in the cache links an npc to its shop, or gives a shop a location - that is server side.
- Sprites carry a name hash, not a name. sprite_name holds the cracked ones, sprite_use holds
  everything that points at a sprite (interface components and params typed SPRITE), sprite_hash
  holds all 36497 hashes.
"""


def tool_schema(_args):
    c = connect()
    tables = []
    for (name,) in c.execute("select name from sqlite_master where type='table' order by name"):
        cols = [(r["name"], r["type"]) for r in c.execute("pragma table_info(%s)" % name)]
        n = c.execute("select count(*) from %s" % name).fetchone()[0]
        tables.append({"table": name, "rows": n,
                       "columns": ["%s %s" % (cn, ct or "") for cn, ct in cols]})
    kinds = [r["type"] for r in c.execute("select distinct type from config order by type")]
    return {"database": DB, "sprites_dir": SPRITES, "tables": tables,
            "config_types": kinds, "kind_aliases": KINDS, "notes": NOTES}


def tool_query(args):
    if not args.get("sql"):
        raise ValueError("give sql= with a single SELECT")
    sql = check_readonly(args["sql"])
    limit = min(int(args.get("limit", 200)), MAX_ROWS)
    c = connect()
    return rows_to_json(c.execute(sql), limit)


NONE16 = 65535

# empty slot, per type
NONE_CHILD = {"npcs": 65535, "objects": 32767}


MORPHS = ("morphs", "morphsWide")


def morph_children(m):
    """The value-indexed list, and the id used when the var reads past the end of it."""
    return list(m.get("ids") or []), m.get("fallback")


def name_of(c, t, cid):
    r = c.execute("select name, display from config where type=? and id=?", (t, cid)).fetchone()
    if not r:
        return None
    return {"id": cid, "name": r["name"], "display": r["display"]}


def decode_morphs(c, t, m):
    """The driving var and which values show which child, with the runs collapsed.

    var packs varp in the low 16 bits and varbit in the high 16, 65535 meaning none. A varbit's
    client id is -(raw + 1), NOT -raw: off by one lands on a real but unrelated var, which reads
    plausible enough to believe. That is the one to get right."""
    u1 = m.get("var", 0)
    lo, hi = u1 & 0xFFFF, (u1 >> 16) & 0xFFFF

    var = None
    if hi != NONE16:
        v = c.execute("select * from varbit where id=?", (-(hi + 1),)).fetchone()
        if v:
            var = {"kind": "varbit", "id": -(hi + 1), "name": v["name"],
                   "base": v["base_name"], "base_id": v["base_id"],
                   "mask": v["mask"], "shift": v["shift"]}
        else:
            var = {"kind": "varbit", "id": -(hi + 1), "name": None}
    elif lo != NONE16:
        v = c.execute("select name from config where type='VarPlayer' and id=?", (lo,)).fetchone()
        var = {"kind": "varp", "id": lo, "name": v["name"] if v else None}

    lst, fallback = morph_children(m)
    none = NONE_CHILD.get(t, NONE16)

    # collapse repeated children into ranges
    states, run = [], None
    for i, ch in enumerate(lst):
        if ch == none:
            run = None
            continue
        if run and run["_id"] == ch and run["_last"] == i - 1:
            run["_last"] = i
            run["values"] = str(run["_first"]) if run["_first"] == i else "%d-%d" % (run["_first"], i)
            continue
        run = {"_id": ch, "_first": i, "_last": i, "values": str(i)}
        states.append(run)
    for r2 in states:
        got = name_of(c, t, r2["_id"]) or {"id": r2["_id"], "name": None}
        r2["id"], r2["name"], r2["display"] = got["id"], got.get("name"), got.get("display")
        for k in ("_id", "_first", "_last"):
            r2.pop(k, None)

    out = {"var": var, "states": states}
    if isinstance(fallback, int) and fallback != none:
        out["otherwise"] = name_of(c, t, fallback)
    return out


_IDX = {}


def _index():
    """The shared sidecar, probed on morph_edge. See common.sidecar."""
    from .common import sidecar
    return sidecar("morph_edge")


def parents_of(c, t, cid):
    """Who wears this config as one of their appearances.

    From the sidecar, which holds the edge already. The fallback below is a LIKE on the json and a
    parse of every hit: 58,124 across four lookups.
    """
    idx = _index()
    if idx is not None:
        ids = [r[0] for r in idx.execute(
            "select distinct parent_id from morph_edge where type=? and child_id=? "
            "order by parent_id", (t, cid))]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        names = {r["id"]: r["name"] for r in c.execute(
            "select id, name from config where type=? and id in (%s)" % marks, [t] + ids)}
        return [{"id": i, "name": names.get(i)} for i in ids]

    out = []
    for pid, pname, data in c.execute(
            "select id, name, data from config where type=? and data like ?",
            (t, "%" + str(cid) + "%")):
        if pid == cid or not data:
            continue
        try:
            d = json.loads(data)
        except Exception:
            continue
        for key in MORPHS:
            m = d.get(key)
            if not isinstance(m, dict):
                continue
            lst, fallback = morph_children(m)
            if cid in lst or cid == fallback:
                out.append({"id": pid, "name": pname})
                break
    return out


def _miss_note(kind, needle):
    """What an add-on knows about a name the cache has never heard of.

    102 items were renamed after the cache's copy: 48 is Longbow (unstrung) here and Shieldbow
    (unstrung) in the game. Looked up by the name on screen they returned found: 0, which reads as
    no such item rather than a name this cache predates.
    """
    from rs3mcp import addons
    for offered in addons.every("lookup_miss"):
        try:
            note = offered(kind, needle)
        except Exception:
            note = None
        if note:
            return note
    return None


def tool_lookup(args):
    # id= is what every other tool here takes
    kind = str(args.get("kind") or "").strip().lower()
    if not kind:
        raise ValueError("give a kind: item, npc, loc, struct, enum, iface")
    t = KINDS.get(kind, kind)
    needle = args.get("name")
    if needle is None:
        needle = args.get("id")
    if needle is None:
        raise ValueError("give a name= or an id=")
    needle = str(needle)
    c = connect()
    if needle.lstrip("-").isdigit():
        rows = c.execute("select * from config where type=? and id=?", (t, int(needle))).fetchall()
    else:
        rows = c.execute("select * from config where type=? and name=? collate nocase", (t, needle)).fetchall()
        if not rows:
            rows = c.execute("select * from config where type=? and display=? collate nocase order by id",
                             (t, needle)).fetchall()
        if not rows:
            like = "%" + needle + "%"
            rows = c.execute("select * from config where type=? and (name like ? or display like ?)"
                             " collate nocase order by id limit 25", (t, like, like)).fetchall()
    if not rows:
        return {"found": 0, "matches": [], "note": _miss_note(t, needle)}

    out = []
    for r in rows[:5]:
        d = json.loads(r["data"]) if r["data"] else {}
        rec = {"type": t, "id": r["id"], "name": r["name"], "display": r["display"], "data": d}
        rec["params"] = [
            {"id": p["param_id"], "name": p["name"], "valuetype": p["valuetype"],
             "value": p["str_value"] if p["str_value"] is not None else p["int_value"],
             "target": p["target_name"]}
            for p in c.execute(
                "select p.param_id,t.name,t.valuetype,p.int_value,p.str_value,p.target_name "
                "from param_use p left join param_type t on t.param_id=p.param_id "
                "where p.owner_type=? and p.owner_id=? order by p.param_id", (t, r["id"]))]
        # multiloc, for npcs as well as locs
        for key in MORPHS:
            if key in d:
                rec["multiloc"] = decode_morphs(c, t, d[key])

        parents = parents_of(c, t, r["id"])
        if parents:
            rec["parents"] = parents
            rec["hint"] = "the world carries the parent's id, not this one"

        if t == "items":
            rec["sold_in"] = [{"inv_id": s["inv_id"], "inv": s["name"], "quantity": s["quantity"]}
                              for s in c.execute(
                                  "select s.inv_id, i.name, s.quantity from inv_stock s "
                                  "join inv i on i.inv_id=s.inv_id where s.item_id=?", (r["id"],))]
        out.append(rec)
    return {"found": len(rows), "matches": out,
            "note": ("%d matched, first 5 returned" % len(rows)) if len(rows) > 5 else None}


def tool_sprite(args):
    if args.get("name") is None and args.get("id") is not None:
        args = dict(args, name=str(args["id"]))
    if args.get("name") is None:
        raise ValueError("give a sprite name= or id=")
    c = connect()
    needle = str(args["name"])
    if needle.lstrip("-").isdigit():
        ids = [int(needle)]
    else:
        like = "%" + needle + "%"
        ids = [r[0] for r in c.execute(
            "select sprite_id from sprite_name where name like ? collate nocase "
            "union select sprite_id from sprite_use where owner_name like ? collate nocase "
            "order by 1 limit 50", (like, like))]
    out = []
    for sid in ids:
        nm = c.execute("select name from sprite_name where sprite_id=?", (sid,)).fetchone()
        png = os.path.join(SPRITES, "sprites-%d.png" % sid)
        out.append({
            "id": sid,
            "name": nm["name"] if nm else None,
            "file": png if os.path.exists(png) else None,
            "pointed_at_by": [{"source": u["source"], "owner_type": u["owner_type"],
                               "owner": u["owner_name"]}
                              for u in c.execute(
                                  "select source,owner_type,owner_name from sprite_use "
                                  "where sprite_id=? limit 10", (sid,))]})
    rec = {"found": len(out), "sprites": out}
    if not out and not c.execute("select 1 from sprite_name limit 1").fetchone():
        rec["note"] = ("no sprite names in this database. Names are cracked from the cache's name "
                       "hashes and carried from older builds, and most sprites have none. A sprite "
                       "id still works")
    return rec



TOOLS = [
    {"name": "rs3_schema", "fn": tool_schema,
     "description": "Tables, columns, row counts and the gotchas needed to write correct SQL "
                    "against the RS3 cache database. Call this first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "rs3_query", "fn": tool_query,
     "description": "Run a read-only SQL SELECT against the RS3 cache database and get rows as JSON.",
     "inputSchema": {"type": "object", "required": ["sql"], "properties": {
         "sql": {"type": "string", "description": "a single SELECT or WITH statement"},
         "limit": {"type": "integer", "description": "max rows, default 200, cap %d" % MAX_ROWS}}}},
    {"name": "rs3_lookup", "fn": tool_lookup,
     "description": "One config by kind and name or id, with params, multiloc varbit and shop "
                    "appearances resolved, and the parents when it is one appearance of a multi. "
                    "rs3_family for the rest of the family, rs3_trace for everything that points "
                    "at it.",
     "inputSchema": {"type": "object", "required": ["kind"], "properties": {
         "kind": {"type": "string", "description": "npc, loc, obj, struct, enum, quest, seq or iface"},
         "name": {"type": "string", "description": "config name or display name"},
         "id": {"type": "string", "description": "config id, same as passing it as name"}}},
     "examples": [{"kind": "npc", "name": "Wolf"}, {"kind": "obj", "id": "4151"}]},
    {"name": "rs3_sprite", "fn": tool_sprite,
     "description": "A sprite by id or by name, with the png path on disk and what points at it.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "part of its name, or its user's"},
         "id": {"type": "string", "description": "sprite id"}}}},
]

