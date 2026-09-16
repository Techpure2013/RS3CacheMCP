"""
Which clientscripts touch a given thing, rather than which contain a given string.

`rs3_script` matches text, which answers "where does this word appear". The question actually being
asked is usually "what writes this varbit", "what uses this interface", "what reads this item" --
and text search answers those badly, because 44763 matches a coordinate as readily as a var.

The decompiler has already done the work. It types its constants:

  varbitplayer_44763    a varbit, by id
  varplayer_3079        a base VarPlayer
  varclient_2506        a VarClient
  32036 as seq          a sequence, and the same shape for obj, npc, struct, inv, quest, model,
                        dbrow, cursor, achievement and the rest
  comp(1786, 88)        interface 1786, component 88
  script12735()         a call to another script

So the references are extractable exactly rather than by guessing, and an index over them turns a
scan of 60MB into a keyed lookup. It lives in the same sidecar as the token index, is rebuilt by
buildindex.py, and every reader falls back to nothing rather than to a wrong answer if it is
missing.

Tools:
  rs3_script_refs  every script that touches one var, config, interface or script, by id
"""

import os
import sqlite3

from .common import ROOT
INDEX = os.path.join(ROOT, "rs3-index.db")

# what a caller may ask for, and the column value it maps to
KINDS = {
    "varbit": "varbit", "varp": "varp", "varplayer": "varp", "varclient": "varclient",
    "obj": "obj", "item": "obj", "npc": "npc", "loc": "loc", "seq": "seq",
    "struct": "struct", "inv": "inv", "quest": "quest", "model": "model",
    "dbrow": "dbrow", "cursor": "cursor", "achievement": "achievement",
    "graphic": "graphic", "stat": "stat", "iface": "iface", "script": "script",
}
_C = {}


def _db():
    if "c" not in _C:
        # The shared opener, so this gets the same staleness refusal as the rest. This one had
        # neither a probe nor a stamp check, so it would happily answer from an index built
        # against a different cache.
        from .common import sidecar
        _C["c"] = sidecar("script_ref")
    return _C["c"]


def _has_table(c):
    if c is None:
        return False
    return bool(c.execute("select 1 from sqlite_master where type='table' "
                          "and name='script_ref'").fetchone())


def tool_script_refs(args):
    c = _db()
    if not _has_table(c):
        return {"error": "no reference index",
                "note": "run python rs3mcp/buildindex.py; it is built from the decompiled "
                        "clientscripts and nothing else needs it."}

    kind = str(args.get("kind") or "").strip().lower()
    who = args.get("id")
    limit = min(int(args.get("limit", 40)), 300)

    if not kind:
        rows = c.execute("select kind, count(*), count(distinct id) from script_ref "
                         "group by kind order by 2 desc").fetchall()
        return {"kinds": [{"kind": k, "references": n, "distinctIds": d} for k, n, d in rows],
                "note": "pass kind= and id=, for instance kind=varbit id=44763. The ids are the "
                        "ones the decompiler wrote, so a varbit id here is the positive form "
                        "rather than the negative one the varbit table uses."}

    if kind not in KINDS:
        return {"found": 0, "note": "kind must be one of " + ", ".join(sorted(KINDS))}
    if who is None or str(who).strip() == "":
        rows = c.execute("select id, count(*) n from script_ref where kind=? "
                         "group by id order by n desc limit ?", (KINDS[kind], limit)).fetchall()
        return {"kind": kind, "mostReferenced": [{"id": i, "scripts": n} for i, n in rows],
                "note": "the ids of this kind that the most scripts touch. Add id= for one."}

    ident = str(who).strip()
    # a varbit is negative in the database and positive in the scripts; accept either
    if KINDS[kind] == "varbit" and ident.startswith("-"):
        ident = ident[1:]
    try:
        cid = int(ident)
    except ValueError:
        return {"found": 0, "note": "id must be a number"}

    rows = c.execute("select file, count(*) n from script_ref where kind=? and id=? "
                     "group by file order by n desc, file limit ?",
                     (KINDS[kind], cid, limit)).fetchall()
    return {"kind": kind, "id": cid, "found": len(rows),
            "scripts": [{"script": f, "mentions": n} for f, n in rows],
            "note": "every clientscript that names it. rs3_script_read shows one whole, and "
                    "rs3_script searches their text when what you want really is a string."}


TOOLS = [
    {"name": "rs3_script_refs", "fn": tool_script_refs, "examples": [{"kind": "varbit", "id": "27169"}, {"kind": "iface", "id": "1188"}],
     "description": "Which clientscripts touch a given varbit, var, item, npc, interface, "
                    "sequence or script, by id rather than by text. The decompiler types its "
                    "constants, so this is exact where a text search would match a coordinate "
                    "sharing the number. kind= alone gives the most-referenced ids.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string",
                  "description": "varbit, varp, varclient, obj, npc, seq, struct, inv, quest, "
                                 "iface, script and others"},
         "id": {"type": "string", "description": "the id; a varbit may be given either sign"},
         "limit": {"type": "integer", "description": "scripts to return, 40 by default"}}}},
]
