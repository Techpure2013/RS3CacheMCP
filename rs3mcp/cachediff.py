"""What a game update changed, comparing the rebuilt database against the one it replaced.

Every rebuild keeps the previous as rs3-cache.db.bak and nothing looked at it. The question is not
how many rows moved, it is whether anything built against these ids went away.

  removed   an id that existed and does not now. A lookup returns empty. Nothing raises
  renamed   the display name moved, so anything matching by name stops matching
  recoded   the config name moved, so anything matching by code name stops matching
  added     new content, and the reason the rebuild was worth doing

Both are opened read only and joined in sqlite: 858,637 rows against 853,963 is not something to
pull into memory.

  rs3_cache_diff  what changed between the current database and the one before it
"""

import os
import sqlite3

from .common import ROOT

CURRENT = os.path.join(ROOT, "rs3-cache.db")
PREVIOUS = os.path.join(ROOT, "rs3-cache.db.bak")

# the types anything outside the cache will name
MATTERS = ("items", "objects", "npcs", "interfaces", "structs", "enums", "sequences", "spotanims")


def _open(a, b):
    c = sqlite3.connect("file:%s?mode=ro" % a.replace("\\", "/"), uri=True)
    c.execute("attach ? as prev", ("file:%s?mode=ro" % b.replace("\\", "/"),))
    return c


def tool_cache_diff(args):
    current = str(args.get("current") or CURRENT)
    previous = str(args.get("previous") or PREVIOUS)
    for p in (current, previous):
        if not os.path.exists(p):
            return {"error": "no database at " + p,
                    "note": "a rebuild keeps the previous one as rs3-cache.db.bak, so there is "
                            "something to compare from the second rebuild on."}

    limit = min(int(args.get("limit", 25)), 200)
    only = str(args.get("type") or "").strip()
    c = _open(current, previous)

    # one grouped count per database, not per type
    now_by = dict(c.execute("select type, count(*) from config group by type"))
    was_by = dict(c.execute("select type, count(*) from prev.config group by type"))
    counts = []
    for ctype in sorted(set(now_by) | set(was_by)):
        if only and ctype != only:
            continue
        now, was = now_by.get(ctype, 0), was_by.get(ctype, 0)
        if now != was:
            counts.append({"type": ctype, "was": was, "now": now, "change": now - was})
    counts.sort(key=lambda r: -abs(r["change"]))

    types = [only] if only else MATTERS
    removed, renamed, recoded, added = [], [], [], {}
    for ctype in types:
        for r in c.execute(
                "select p.id, p.name, p.display from prev.config p "
                "left join config n on n.type=p.type and n.id=p.id "
                "where p.type=? and n.id is null order by p.id limit ?", (ctype, limit)):
            removed.append({"type": ctype, "id": r[0], "name": r[1], "display": r[2]})
        for r in c.execute(
                "select n.id, p.display, n.display from config n join prev.config p "
                "on p.type=n.type and p.id=n.id where n.type=? "
                "and p.display is not null and n.display is not null "
                "and p.display <> n.display order by n.id limit ?", (ctype, limit)):
            renamed.append({"type": ctype, "id": r[0], "was": r[1], "now": r[2]})
        for r in c.execute(
                "select n.id, p.name, n.name from config n join prev.config p "
                "on p.type=n.type and p.id=n.id where n.type=? "
                "and p.name is not null and n.name is not null and p.name <> n.name "
                "order by n.id limit ?", (ctype, limit)):
            recoded.append({"type": ctype, "id": r[0], "was": r[1], "now": r[2]})
        n = c.execute("select count(*) from config n left join prev.config p "
                      "on p.type=n.type and p.id=n.id where n.type=? and p.id is null",
                      (ctype,)).fetchone()[0]
        if n:
            added[ctype] = n
    c.close()

    breaks = len(removed) + len(renamed) + len(recoded)
    return {
        "current": os.path.basename(current), "previous": os.path.basename(previous),
        "rowsByType": counts[:20],
        "added": added,
        "removed": removed[:limit],
        "displayRenamed": renamed[:limit],
        "configNameChanged": recoded[:limit],
        "verdict": ("nothing that could be named went away" if not breaks else
                    "%d thing(s) that could be named elsewhere moved or went away" % breaks),
        "note": "removed and renamed are the ones that break quietly: a lookup that finds nothing "
                "returns empty rather than raising, so whatever asked carries on without it. "
                "Added content cannot break anything. Pass type= to see one kind in full, "
                "and limit= for more than %d of each." % limit,
    }


TOOLS = [
    {"name": "rs3_cache_diff", "fn": tool_cache_diff, "examples": [{"limit": 10}, {"type": "items"}],
     "description": "What a game update changed between the current cache database and the one "
                    "the rebuild replaced: ids that went away, display names that moved, config "
                    "names that moved, and what was added. Run it after a rebuild to find out "
                    "whether an id something relies on has quietly stopped resolving.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string", "description": "one config type, e.g. items or objects"},
         "limit": {"type": "integer", "description": "rows of each kind, 25 by default"},
         "current": {"type": "string", "description": "a database path; defaults to rs3-cache.db"},
         "previous": {"type": "string", "description": "defaults to rs3-cache.db.bak"}}}},
]
