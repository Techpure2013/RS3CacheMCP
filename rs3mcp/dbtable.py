"""The game's data tables, and the rows in them.

391 tables holding 20,366 rows: currencies, cosmetics, farm paddocks, invention materials.
Content that is not an item, npc or loc. Before the opcodes were worked out 13,977 were filed under
the wrong table: the table id is a leb128, see rs3mcp/specs/dbrows.ops.

Two things to know.

A dbcolumn arrives packed, `table << 12 | column << 4 | tuple index`. db_find_with_count(209088)
means DINO_BASE_CAMP_BUILDING column 12. column= splits it.

Column types stay numbers. Three are settled: 1 boolean, 9 packed interface component, 36 string.
The rest are not guessed at. Id spaces here overlap enough that a small column matches items, npcs
and structs alike.

The 26,957 strings in these rows are reachable from nothing else. text= searches them.

  rs3_dbtable   a table's columns and rows, a text search, or a packed dbcolumn split up
"""

import json
import os

from .common import connect

TABLE_SHIFT = 12
COLUMN_SHIFT = 4
COLUMN_MASK = 0xFF
TUPLE_MASK = 0xF

# only what the data settles, nothing else
TYPE_NAMES = {1: "boolean", 9: "interface component, packed", 36: "string"}


# tables hidden from a search, each with reason
#
# read some rows before adding a family
HIDDEN_FAMILIES = (
    (("TRH", "MTX", "WOF_", "PREMCLUB", "RUNEPASS", "BP3", "MARKETPLACE"), (),
     "monetised: Treasure Hunter, microtransactions, the battle passes and the store"),
    ((), (13, 77),
     "rotating events: EVENT_WEEKEND and WEEKEND_EVENTS, the bonus weekend schedule"),
    (("UITUTORIAL",), (),
     "onboarding: tutorial prose, 1,256 rows of it"),
    # found by reading rows: storefront wording throughout
    ((), (102, 122, 134, 385),
     "storefronts under a general name: LOOT_TABLE is all Treasure Hunter, "
     "MOBILE_SHOP_FEATURE_INFORMATION and BONDS_PACKAGE are Bond listings, and 385 is unnamed "
     "Marketplace cosmetics"),
)


def _hidden_reason(tid, name):
    """Why this table is hidden, or None if it is not."""
    for prefixes, ids, why in HIDDEN_FAMILIES:
        if tid in ids or (name and prefixes and name.startswith(prefixes)):
            return why
    return None


def _is_promo(tid, name):
    return _hidden_reason(tid, name) is not None


# read once and kept, keyed on mtime
_CACHE = {}


def _fresh(c):
    from .common import DB
    stamp = os.path.getmtime(DB) if os.path.exists(DB) else 0
    if _CACHE.get("stamp") != stamp:
        _CACHE.clear()
        _CACHE["stamp"] = stamp
    return _CACHE


def _typed_params(c):
    """(param id -> name) for every param declared to hold a dbrow. 53 of them, and they do not
    change between rebuilds."""
    hold = _fresh(c)
    if "params" not in hold:
        hold["params"] = {r["param_id"]: r["name"] for r in
                          c.execute("select param_id, name from param_type "
                                    "where target='dbrows'")}
    return hold["params"]


def _row_index(c):
    """table -> row ids, in id order.

    The table field lives inside a json blob with no index, so one table costs a parse of all
    20,031. The whole map costs the same scan."""
    hold = _fresh(c)
    if "rows" not in hold:
        idx = {}
        for rid, tid in c.execute(
                "select id, json_extract(data, '$.table') from config where type='dbrows' "
                "order by id"):
            idx.setdefault(tid, []).append(rid)
        hold["rows"] = idx
    return hold["rows"]


def _item_ids(c):
    """Every item id. 63,414 of them, and _drop_shaped tested membership against a fresh set on
    every call, which was most of its 176ms."""
    hold = _fresh(c)
    if "items" not in hold:
        hold["items"] = {r[0] for r in c.execute("select id from config where type='items'")}
    return hold["items"]


def _split_column(packed):
    return (packed >> TABLE_SHIFT,
            (packed >> COLUMN_SHIFT) & COLUMN_MASK,
            packed & TUPLE_MASK)


def _pack_column(table, column, tuple_index=0):
    return (table << TABLE_SHIFT) | (column << COLUMN_SHIFT) | tuple_index


def _cells(col):
    """A column's values as one list per type, each running down the rows."""
    rows = col.get("values") or []
    return [[r[i] for r in rows] for i in range(len(col.get("types") or []))]


def _table(c, tid):
    r = c.execute("select id, name, data from config where type='dbtables' and id=?",
                  (tid,)).fetchone()
    if r is None:
        return None
    d = json.loads(r["data"]).get("columns") or {}
    cols = []
    for col in d.get("columns") or []:
        types = col.get("types") or []
        entry = {"column": col.get("id"),
                 "types": [{"type": t, "is": TYPE_NAMES[t]} if t in TYPE_NAMES else {"type": t}
                           for t in types],
                 "packedID": _pack_column(tid, col.get("id"))}
        defaults = ((col.get("default") or {}).get("values") or [[]])[0]
        if defaults:
            entry["default"] = defaults if len(defaults) > 1 else defaults[0]
        cols.append(entry)
    return {"table": tid, "name": _name_of(c, tid), "columns": cols,
            "declaredColumns": d.get("count")}


# 132 unnamed tables, identified by reading their rows
IDENTIFIED = {
    35: "invention disassembly material distributions",
    366: "teleport jewellery: name, item id, and the first of its destinations in table 367",
    367: "teleport destinations, run consecutively from the row that table 366 points at",
    369: "construction room shapes, with their size and tile layout",
    370: "construction furniture",
    376: "a location hierarchy, parent in column 4 and children in column 3",
    380: "item name abbreviations, so abs is the Armadyl Battlestaff",
    383: "region descriptions, with settlements and the quests a region completes",
}


def _name_of(c, tid):
    r = c.execute("select name from config where type='dbtables' and id=?", (tid,)).fetchone()
    if r and r["name"]:
        return r["name"]
    ours = IDENTIFIED.get(tid)
    return "(unnamed; looks like %s)" % ours if ours else None


def _referenced_by(c, tid, limit=4):
    """Params typed as dbrow whose value is a row of this table, with what owns them.

    The only handle on 114 of the 374 tables with rows: their rows carry no columns, just three
    bytes naming the table. The meaning is in the param name, so objects.AGILITY_OBSTACLE_DBROW is
    what says table 97 is agility obstacles.

    Declared targets only. Matching any integer in the row id range gives table 1 an alleged
    413,015 references, which is every param holding the number 3."""
    names = _typed_params(c)
    typed = list(names)
    if not typed:
        return None
    # ask param_use directly, since the join cost 7ms
    ids = _row_index(c).get(tid, [])
    if not ids:
        return None
    marks = ",".join("?" * len(typed))
    holes = ",".join("?" * len(ids))
    rows = c.execute(
        "select owner_type, param_id, count(*) n from param_use "
        "where param_id in (%s) and int_value in (%s) "
        "group by owner_type, param_id order by n desc limit ?" % (marks, holes),
        typed + ids + [limit]).fetchall()
    if not rows:
        return None
    return [{"owner": r["owner_type"], "param": names.get(r["param_id"]) or r["param_id"],
             "uses": r["n"]} for r in rows]


def _rows_of(c, tid, limit):
    """Rows belonging to a table.

    No index on the table number. 20,031 short blobs scan fast enough that one would cost more to
    keep current than it saves."""
    ids = _row_index(c).get(tid, [])[:limit]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    got = {r["id"]: r["data"] for r in
           c.execute("select id, data from config where type='dbrows' and id in (%s)" % marks, ids)}
    out = []
    for rid in ids:
        data = got.get(rid)
        if data is None:
            continue
        d = json.loads(data)
        body = d.get("columns") or {}
        row = {"row": rid, "values": {}}
        for col in body.get("columns") or []:
            vals = _cells(col)
            row["values"][str(col.get("id"))] = vals[0] if len(vals) == 1 else vals
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _scripts_using(rowids, limit=12):
    """Clientscripts naming any row of this table, from the reference index.

    Scripts name rows, not tables, so a table's usage is the union over its rows. Nothing
    referencing it is common: plenty are read by the server."""
    if not rowids:
        return None
    try:
        from .scriptrefs import _db, _has_table
    except ImportError:
        return None
    c = _db()
    if not _has_table(c):
        return None
    marks = ",".join("?" * len(rowids))
    rows = c.execute("select file, count(*) n from script_ref where kind='dbrow' "
                     "and id in (%s) group by file order by n desc, file limit ?" % marks,
                     list(rowids) + [limit]).fetchall()
    if not rows:
        return None
    return {"scripts": [r[0] for r in rows],
            "how": "these name one of this table's rows directly. rs3_script_read on one to see "
                   "what it does with it."}


# totals a weighted table adds up to
WEIGHT_TOTALS = (100, 128, 256, 1000, 1024, 10000, 65536)

# exact, which under-counts rows on purpose


def _drop_shaped(c, limit, show_all=False):
    # same answer until a rebuild, so compute once
    hold = _fresh(c)
    if "drops" not in hold:
        hold["drops"] = _drop_scan(c)
    found = dict(hold["drops"])
    return _drop_report(c, found, limit, show_all)


def _drop_report(c, found, limit, show_all):
    promo = [e for e in found.values() if _hidden_reason(e["table"], e["name"])]
    if not show_all:
        for e in promo:
            found.pop(e["table"], None)
    out = sorted(found.values(), key=lambda x: -x["rows"])[:limit]
    return {"found": len(found), "tables": out,
            "hidden": (None if show_all or not promo else
                       {"tables": len(promo), "rows": sum(e["rows"] for e in promo),
                        "names": sorted(e["name"] or str(e["table"]) for e in promo),
                        "how": "all=true to include them"}),
            "note": "found by shape: a column of real item ids beside a column of weights "
                    "totalling a round number. It is a heuristic, and the entries carrying a "
                    "caution are the ones it is least sure of. It under-counts on purpose, so a "
                    "table's row count here is a floor. No table here itemises a monster drop or a "
                    "reward chest."}


def _drop_scan(c):
    """Rows pairing real item ids with weights totalling a round number.

    By name you get LOOT_TABLE, which is three rows of Treasure Hunter promo. By shape you get the
    gem rocks in MINING_ROCK_DATA and the digs in ARCH_EXCAVATION, which is the real drop data.

    Nothing here itemises a monster drop or a reward chest."""
    items = _item_ids(c)
    found = {}
    for rid, data in c.execute("select id, data from config where type='dbrows'"):
        o = json.loads(data)
        for col in (o.get("columns") or {}).get("columns") or []:
            vals = _cells(col)
            if len(vals) < 2 or len({len(v) for v in vals}) != 1 or len(vals[0]) < 2:
                continue
            # by index: small numbers pass both tests
            ids = [i for i, v in enumerate(vals)
                   if all(isinstance(x, int) and x in items for x in v)]
            wts = [i for i, v in enumerate(vals)
                   if all(isinstance(x, int) and 0 < x <= 100000 for x in v)
                   and sum(v) in WEIGHT_TOTALS]
            pair = [(a, b) for a in ids for b in wts if a != b]
            if pair:
                ai, bi = pair[0]
                ids, wts = [vals[ai]], [vals[bi]]
                t = o.get("table")
                e = found.setdefault(t, {"table": t, "name": _name_of(c, t), "rows": 0,
                                         "example": None})
                e["rows"] += 1
                if e["example"] is None:
                    # ids 1 to 20 exist, so say which
                    small = max(ids[0]) < 256
                    e["example"] = {"row": rid, "column": col.get("id"),
                                    "items": ids[0][:6], "weights": wts[-1][:6],
                                    "totals": sum(wts[-1])}
                    if small and t in IDENTIFIED:
                        e["caution"] = ("the ids here are not items: this table is %s"
                                        % IDENTIFIED[t])
                    elif small:
                        e["caution"] = ("every id here is under 256, so these are as likely to be "
                                        "category indexes as items")
                break
    return found


def tool_dbtable(args):
    c = connect()
    limit = min(int(args.get("limit", 40)), 500)

    packed = args.get("column")
    if packed is not None:
        n = int(packed)
        tid, col, tup = _split_column(n)
        t = _table(c, tid)
        out = {"packedID": n, "table": tid, "column": col, "tupleIndex": tup,
               "name": t["name"] if t else None,
               "note": "dbcolumn ids pack as table << 12 | column << 4 | tuple index. This is the "
                       "form a clientscript passes to db_find and friends."}
        if t is None:
            out["found"] = 0
            out["note"] += " No table with that id, so the number is probably not a dbcolumn."
            return out
        match = [x for x in t["columns"] if x["column"] == col]
        if not match:
            out["found"] = 0
            out["note"] += " That table has no such column."
        else:
            out["columnDefinition"] = match[0]
        return out

    if str(args.get("drops", "")).lower() in ("1", "true", "yes"):
        return _drop_shaped(
            c, limit, str(args.get("all", "")).lower() in ("1", "true", "yes"))

    text = args.get("text")
    if text:
        needle = str(text).strip()
        low = needle.lower()
        hits = []
        # LIKE narrows before any json is parsed
        show_all = str(args.get("all", "")).lower() in ("1", "true", "yes")
        skipped = {}
        for rid, data in c.execute("select id, data from config where type='dbrows' "
                                   "and data like ?", ("%" + needle + "%",)):
            d = json.loads(data)
            tid = d.get("table")
            tname = _name_of(c, tid)
            body = d.get("columns") or {}
            for col in body.get("columns") or []:
                for vals in _cells(col):
                    for v in vals:
                        if isinstance(v, str) and low in v.lower():
                            why = None if show_all else _hidden_reason(tid, tname)
                            if why:
                                skipped.setdefault(why, set()).add(tname or tid)
                            else:
                                hits.append({"row": rid, "table": tid, "name": tname,
                                             "column": col.get("id"), "value": v})
                            break
            if len(hits) >= limit:
                break
        # one row can match in several columns
        hits = hits[:limit]
        return {"found": len(hits), "rows": hits,
                "hidden": (None if not skipped else
                           {"reasons": {why: sorted(str(t) for t in tabs)
                                        for why, tabs in skipped.items()},
                            "how": "all=true to search these too"}),
                "note": ("every match was in a hidden table. This text IS in the data; "
                         "pass all=true to see where." if not hits and skipped else
                         "text stored in a data table row. The table and column say what it is; "
                         "rs3_dbtable id=<table> for the rest of that table.")}

    search = args.get("search") or args.get("name")
    if search and args.get("id") is None:
        s = str(search).strip()
        rows = c.execute("select id, name from config where type='dbtables' and name like ? "
                         "collate nocase order by name limit ?",
                         ("%" + s + "%", limit)).fetchall()
        return {"found": len(rows),
                "tables": [{"table": r["id"], "name": r["name"]} for r in rows],
                "note": "ask again with id= for a table's columns, and rows=true for its contents"
                        if rows else "no table name contains %s. text= searches the strings inside "
                                     "the rows instead" % s}

    who = args.get("id")
    if who is None:
        return {"tables": c.execute("select count(*) from config where type='dbtables'"
                                    ).fetchone()[0],
                "rows": c.execute("select count(*) from config where type='dbrows'").fetchone()[0],
                "note": "search= by name, id= for one table's columns, rows=true for its contents, "
                        "or column= to split a packed dbcolumn id out of a clientscript."}

    tid = int(who)
    t = _table(c, tid)
    if t is None:
        return {"found": 0, "table": tid, "note": "no table with that id"}

    ref = _referenced_by(c, tid)
    if ref:
        t["referencedBy"] = ref

    if str(args.get("rows", "")).lower() in ("1", "true", "yes"):
        rows = _rows_of(c, tid, limit)
        t["rows"] = rows
        if rows and not any(r["values"] for r in rows):
            t["keysOnly"] = ("these rows carry no columns; they are ids that something else points "
                             "at. referencedBy says what, and the param's name is usually the best "
                             "account of what the table is for.")
        used = _scripts_using([r["row"] for r in rows])
        if used:
            t["usedByScripts"] = used
        t["note"] = ("values are keyed by column number, matching columns above. A column holding "
                     "several values is a list, in row order.")
        if len(rows) >= limit:
            t["truncated"] = "%d shown, raise limit" % limit
    else:
        t["note"] = ("types are the raw numbers; only boolean, string and packed interface "
                     "component are settled, the rest are left as read. Add rows=true for the "
                     "contents.")
    return t


TOOLS = [
    {"name": "rs3_dbtable", "fn": tool_dbtable, "examples": [{"search": "ACHIEVEMENT"}, {"id": "314", "rows": True}],
     "description": "The game's data tables and their rows: currencies, farm paddocks, invention "
                    "materials and 383 others. Columns with types and defaults, rows, search by "
                    "name, or splits a packed dbcolumn id. text= searches the 26,535 strings no "
                    "other tool reaches.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "table id"},
         "name": {"type": "string", "description": "table name, exact or partial"},
         "search": {"type": "string", "description": "list tables whose name contains this"},
         "rows": {"type": "string", "description": "true to include the table's rows"},
         "column": {"type": "string", "description": "a packed dbcolumn id to split"},
         "text": {"type": "string", "description": "find rows whose stored text contains this"},
         "drops": {"type": "string", "description": "true to list weighted drop tables, found by shape"},
         "all": {"type": "string", "description": "true to include what drops= and text= hide: "
                                                  "monetised, event schedule, tutorial prose"},
         "limit": {"type": "integer", "description": "max tables or rows, default 40"}}}},
]
