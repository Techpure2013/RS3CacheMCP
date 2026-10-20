"""
Which id is the real one, and what else is the same thing wearing another face.

Locs and npcs morph. A parent carries a list of appearances and a var that picks between them, and
the world entity carries the parent id. Match a child and you match nothing, with no error.

Items do not morph, they link. Noted, lent, bought and shard copies are each their own id.

Both failures read the same from outside: it matched nothing and said nothing.

Tools:
  rs3_family  either end of a family, and which id to match on
"""

import json
import os
import sqlite3

from .cache import KINDS, MORPHS, decode_morphs, name_of, parents_of
from .common import connect

# link fields, tribyte on modern RS3 only
ITEM_LINKS = {
    "noteLink": "the other half of the note pair",
    "noteTemplate": "the note template this item uses",
    "lentLink": "the lent copy",
    "lentTemplate": "the template this item is a lend of",
    "boughtLink": "the bought copy",
    "boughtTemplate": "the template this item is a bought copy of",
    "shardLink": "the shard copy",
    "shardTemplate": "the template this item is a shard of",
    "recipeTemplate": "the construction recipe template this plan is drawn with",
}


def _resolve(c, t, who):
    who = str(who).strip()
    if who.lstrip("-").isdigit():
        r = c.execute("select id,name,display,data from config where type=? and id=?",
                      (t, int(who))).fetchone()
        return [r] if r else []
    rows = c.execute("select id,name,display,data from config where type=? and "
                     "(name=? or display=?) collate nocase", (t, who, who)).fetchall()
    if rows:
        return rows
    return c.execute("select id,name,display,data from config where type=? and "
                     "(name like ? or display like ?) collate nocase order by id limit 12",
                     (t, "%" + who + "%", "%" + who + "%")).fetchall()


def _morph_family(c, t, row):
    d = json.loads(row["data"]) if row["data"] else {}
    out = {"role": "standalone", "children": [], "parents": [], "var": None}

    for key in MORPHS:
        if key in d:
            dec = decode_morphs(c, t, d[key])
            out["role"] = "parent"
            out["var"] = dec.get("var")
            out["children"] = dec.get("states") or []
            out["otherwise"] = dec.get("otherwise")
            break

    parents = parents_of(c, t, row["id"])
    if parents:
        out["parents"] = parents
        out["role"] = "child" if out["role"] == "standalone" else "parent and child"

    return out



_IDX = {}


def _index():
    """The shared sidecar, probed on item_link. See common.sidecar."""
    from .common import sidecar
    return sidecar("item_link")


def _item_family(c, row):
    d = json.loads(row["data"]) if row["data"] else {}
    links, reverse = [], []

    for field, means in ITEM_LINKS.items():
        v = d.get(field)
        if isinstance(v, int) and v >= 0:
            got = name_of(c, "items", v) or {"id": v, "name": None}
            links.append({"field": field, "means": means, **got})

    # who links to this item, from the sidecar
    want = row["id"]
    idx = _index()
    if idx is not None:
        for f, oid, oname, odisp in idx.execute(
                "select field, owner_id, owner_name, owner_display from item_link "
                "where target_id=? order by field, owner_id limit 60", (want,)):
            if oid == want:
                continue
            reverse.append({"field": f, "id": oid, "name": oname, "display": odisp,
                            "means": "that item names this one as its %s" % f})
    else:
        for field in ITEM_LINKS:
            for suffix in (",", "}"):
                pattern = '%%"%s":%d%s%%' % (field, want, suffix)
                for oid, oname, odisp, data in c.execute(
                        "select id,name,display,data from config where type='items' "
                        "and data like ? limit 40", (pattern,)):
                    if oid == want or not data:
                        continue
                    try:
                        od = json.loads(data)
                    except Exception:
                        continue
                    if od.get(field) == want:
                        reverse.append({"field": field, "id": oid, "name": oname,
                                        "display": odisp,
                                        "means": "that item names this one as its %s" % field})

    return {"links": links, "linkedFrom": reverse}


def tool_family(args):
    kind = str(args.get("kind") or "").strip().lower()
    who = args.get("id")
    if who is None:
        who = args.get("name")
    if not kind or who is None:
        raise ValueError("give a kind (loc, npc or obj) and an id or name")
    t = KINDS.get(kind, kind)
    if t not in ("objects", "npcs", "items"):
        raise ValueError("kind must be loc, npc or obj")

    c = connect()
    rows = _resolve(c, t, who)
    if not rows:
        return {"found": 0, "note": "nothing by that id or name"}
    if len(rows) > 1:
        return {"found": len(rows), "ambiguous":
                [{"id": r["id"], "name": r["name"], "display": r["display"]} for r in rows],
                "note": "several match, ask again by id"}

    row = rows[0]
    rec = {"kind": kind, "id": row["id"], "name": row["name"], "display": row["display"]}

    if t == "items":
        fam = _item_family(c, row)
        rec.update(fam)
        rec["matchOn"] = (
            "Items do not morph, so the id in a container is the id of the exact variant that got "
            "there. A note, a lent copy and a bought copy are all separate ids, so a check against "
            "one of them never fires for the others. Match the whole set, not one member."
            if fam["links"] or fam["linkedFrom"] else
            "No linked variants, so this id is the only one."
        )
        return rec

    fam = _morph_family(c, t, row)
    rec.update(fam)

    if fam["role"] in ("child", "parent and child"):
        names = ", ".join("%s (%s)" % (p["id"], p.get("name")) for p in fam["parents"][:4])
        rec["matchOn"] = (
            "This is a child appearance. The thing standing in the world carries the PARENT id, "
            "so match on %s, not on %d. Matching the child finds nothing and reports nothing."
            % (names, row["id"])
        )
    elif fam["role"] == "parent":
        rec["matchOn"] = (
            "This is the parent, so this is the id to match on. Which of its %d appearances is "
            "showing depends on %s." % (len(fam["children"]),
                                        (fam["var"] or {}).get("name") or "its driving var")
        )
    else:
        rec["matchOn"] = "Not part of a family, so this id is the only one."

    return rec


TOOLS = [
    {"name": "rs3_family", "fn": tool_family,
     "description": "The whole config family for a loc, npc or item, from any member, and which "
                    "id to match on. Locs and npcs morph and the world carries the "
                    "parent id, so matching a child finds nothing. Items link instead: notes, "
                    "lent and bought copies are separate ids.",
     "inputSchema": {"type": "object", "required": ["kind"], "properties": {
         "kind": {"type": "string", "description": "loc, npc or obj"},
         "id": {"type": "string", "description": "config id, as rs3_lookup or rs3_query returns"},
         "name": {"type": "string", "description": "config name or display name, if no id"}}}},
]
