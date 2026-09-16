"""
Everything that points at one config, and everything it points at.

The data was always joinable and nothing joined it. param_use alone is 775,910 typed edges --
owner, param, value, and the config the value refers to when it refers to one -- and beside it sit
enum_entry, sprite_use, model_use, inv_stock and loc_section.

So this walks both directions in one call. Outgoing is what the thing carries. Incoming is every
other config whose param value names it, which is the direction that is invisible otherwise -- an
item does not know which recipe consumes it, but the recipe's param says so.

Tools:
  rs3_trace  one config with its params, its family, and everything elsewhere that refers to it
"""

from . import addons
from .cache import KINDS
from .common import connect

# owner_type in param_use and sprite_use uses the cache's own type names
LINKABLE = ("items", "npcs", "objects", "structs", "sequences", "maplabels", "interfaces")


def _resolve(c, t, who):
    who = str(who).strip()
    if who.lstrip("-").isdigit():
        r = c.execute("select id,name,display from config where type=? and id=?",
                      (t, int(who))).fetchone()
        return [r] if r else []
    rows = c.execute("select id,name,display from config where type=? and "
                     "(name=? or display=?) collate nocase", (t, who, who)).fetchall()
    return rows or c.execute(
        "select id,name,display from config where type=? and (name like ? or display like ?) "
        "collate nocase order by id limit 10", (t, "%" + who + "%", "%" + who + "%")).fetchall()


def _param_names(c, ids):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: r["name"] for r in c.execute(
        "select id,name from config where type='params' and id in (%s)" % marks, list(ids))}


def tool_trace(args):
    kind = str(args.get("kind") or "").strip().lower()
    who = args.get("id")
    if who is None:
        who = args.get("name")
    if not kind or who is None:
        raise ValueError("give a kind and an id or name")
    t = KINDS.get(kind, kind)
    limit = min(int(args.get("limit", 40)), 200)

    c = connect()
    rows = _resolve(c, t, who)
    if not rows:
        return {"found": 0, "note": "nothing by that id or name in " + t}
    if len(rows) > 1:
        return {"found": len(rows),
                "ambiguous": [{"id": r["id"], "name": r["name"], "display": r["display"]}
                              for r in rows],
                "note": "several match, ask again by id"}

    row = rows[0]
    out = {"type": t, "id": row["id"], "name": row["name"], "display": row["display"]}

    fields = addons.first("client_fields")
    got = fields and fields(t, row["id"])
    if got:
        out["fromClient"] = got

    # outgoing: the params this thing carries, with whatever config each value names
    carries = []
    pu = c.execute("select param_id, int_value, str_value, target_type, target_name "
                   "from param_use where owner_type=? and owner_id=? order by param_id limit ?",
                   (t, row["id"], limit)).fetchall()
    pnames = _param_names(c, {r["param_id"] for r in pu})
    for r in pu:
        rec = {"param": r["param_id"], "paramName": pnames.get(r["param_id"]),
               "value": r["int_value"] if r["str_value"] is None else r["str_value"]}
        if r["target_type"]:
            rec["points at"] = {"type": r["target_type"], "name": r["target_name"]}
        carries.append(rec)
    if carries:
        out["carries"] = carries

    # incoming: anything whose param value names this config. The edge stores a name, not an id.
    refs = []
    if row["name"]:
        for r in c.execute("select owner_type, owner_id, param_id from param_use "
                           "where target_type=? and target_name=? "
                           "order by owner_type, owner_id, param_id limit ?",
                           (t, row["name"], limit)).fetchall():
            refs.append({"via": "param %s" % r["param_id"],
                         "type": r["owner_type"], "id": r["owner_id"]})
    if refs:
        out["referencedBy"] = refs

    extra = {}
    if t == "items":
        stock = c.execute("select inv_id, slot, quantity from inv_stock where item_id=? order by inv_id, slot limit ?",
                          (row["id"], limit)).fetchall()
        if stock:
            extra["soldIn"] = [{"inv": r["inv_id"], "slot": r["slot"], "quantity": r["quantity"]}
                               for r in stock]
    if t == "objects":
        sec = c.execute("select section, placed from loc_section where loc_id=? order by section limit ?",
                        (row["id"], limit)).fetchall()
        if sec:
            extra["mapSections"] = [{"section": r["section"], "placed": r["placed"]} for r in sec]
    if t in ("items", "npcs", "objects", "structs", "interfaces"):
        sp = c.execute("select sprite_id, source from sprite_use where owner_type=? and owner_id=? "
                       "order by sprite_id, source limit ?", (t, row["id"], limit)).fetchall()
        if sp:
            extra["sprites"] = [{"sprite": r["sprite_id"], "via": r["source"]} for r in sp]
    # Ordered because it is truncated. Without it the limit takes whatever rows the table happens
    # to hold first, so a rebuild that inserts them in a different order silently returns a
    # different set for the same question.
    inenum = c.execute("select enum_id, key from enum_entry where value=? "
                       "order by enum_id, key limit ?",
                       (str(row["id"]), limit)).fetchall()
    if inenum:
        extra["appearsInEnums"] = [{"enum": r["enum_id"], "key": r["key"]} for r in inenum]
    if extra:
        out.update(extra)

    seen = addons.first("npc_seen") if t == "npcs" else None
    if seen and seen(row["id"]):
        out["seenAt"] = seen(row["id"])

    # the other direction on quests: which walkthroughs name this thing
    if t in ("npcs", "objects"):
        try:
            quests_using = addons.first("quests_using")
            used = quests_using and quests_using("npc" if t == "npcs" else "object", row["id"])
            if used:
                out["usedInQuests"] = used
        except Exception:
            pass

    # the family, so a child id does not look like a dead end
    try:
        from .families import tool_family
        fam = tool_family({"kind": kind, "id": str(row["id"])})
        for k in ("role", "parents", "children", "links", "linkedFrom", "matchOn"):
            if fam.get(k):
                out.setdefault("family", {})[k] = fam[k]
    except Exception:
        pass

    out["note"] = ("carries is what this config holds. referencedBy is every other config whose "
                   "param value names this one, which is the direction nothing else exposes.")
    return out


TOOLS = [
    {"name": "rs3_trace", "fn": tool_trace,
     "description": "Everything connected to one config, both directions: params with resolved targets, configs whose params name it, enums it appears in, shops, sprites, map sections, and its multiloc or note family. Beats guessing which table to join.",
     "inputSchema": {"type": "object", "required": ["kind"], "properties": {
         "kind": {"type": "string", "description": "obj/item, npc, loc, struct, seq, enum, iface"},
         "id": {"type": "string", "description": "config id, as rs3_lookup or rs3_query returns"},
         "name": {"type": "string", "description": "config or display name, if no id"},
         "limit": {"type": "integer", "description": "max rows per section, default 40"}}}},
]

