"""
Interfaces and their components.

The id is packed: interface << 16 | component. 18612224 is 284 << 16, BUFF_BAR, and every component
shares the interface name, so a raw id from the client means nothing until it is split.

Text components carry their literal string. Search the screen backwards with it: given words you
can see in game, find what draws them.

Tools:
  rs3_iface  an interface, one component, or a search by name or on-screen text
"""

import json
import os

from . import ifacelive
from .common import connect

SHIFT = 16
CHILD = (1 << SHIFT) - 1
ROOT = 65535

# the kind byte, confirmed by counting blocks
KINDS = {0: "container", 3: "figure", 4: "text", 5: "sprite", 6: "model", 9: "line"}

BLOCK = {"sprite": "sprite", "text": "text", "model": "model", "figure": "rectangle", "line": "line"}

# an animation id of 32767 is empty
NO_ANIM = (-1, 32767, None)


# every component carrying drawn text, read once
_CACHE = {}


def _drawn_text(c):
    """(packed id, interface name, the string it draws) for every component that draws one."""
    from .common import DB
    stamp = os.path.getmtime(DB) if os.path.exists(DB) else 0
    if _CACHE.get("stamp") != stamp:
        _CACHE.clear()
        _CACHE["stamp"] = stamp
    if "text" not in _CACHE:
        out = []
        for r in c.execute("select id, name, data from config where type='interfaces' "
                           "and data like '%\"kind\":4,%'"):
            td = (json.loads(r["data"]).get("text") or {})
            if td.get("text"):
                out.append((r["id"], r["name"], td["text"]))
        _CACHE["text"] = out
    return _CACHE["text"]


def _split(n):
    return n >> SHIFT, n & CHILD


def _pack(iface, comp):
    return (iface << SHIFT) | comp


def _kind(d):
    return KINDS.get(d.get("kind"), "type %s" % d.get("kind"))


def _brief(cid, d):
    """One component, small enough that a whole interface can be listed without burying the shape.
    Only the block belonging to its kind is opened, since the other nine are null by construction."""
    iface, comp = _split(cid)
    kind = _kind(d)
    out = {"at": "%d:%d" % (iface, comp), "component": comp, "kind": kind,
           "parent": None if d.get("parent") == ROOT else d.get("parent")}
    if d.get("hidden"):
        out["hidden"] = True
    x, y = d.get("x"), d.get("y")
    w, h = d.get("width"), d.get("height")
    if (x, y) != (0, 0):
        out["pos"] = [x, y]
    if (w, h) != (0, 0):
        out["size"] = [w, h]

    b = d.get(BLOCK.get(kind) or "") or {}
    if kind == "text" and b.get("text"):
        out["text"] = b["text"]
    elif kind == "sprite" and b.get("sprite", -1) not in (-1, None):
        out["sprite"] = b["sprite"]
    elif kind == "model":
        out["model"] = b.get("model")
        if b.get("animation") not in NO_ANIM:
            out["anim"] = b["animation"]
    opts = [o for o in (d.get("options") or []) if o]
    if opts:
        out["options"] = opts
    return out


def _rows(c, iface, limit=None):
    sql = ("select id, data from config where type='interfaces' and id>=? and id<=? order by id")
    args = [_pack(iface, 0), _pack(iface, CHILD)]
    if limit:
        sql += " limit ?"
        args.append(limit)
    return c.execute(sql, args).fetchall()


def _name(c, iface):
    r = c.execute("select name from config where type='interfaces' and id=?",
                  (_pack(iface, 0),)).fetchone()
    return r["name"] if r else None


def _parse(who):
    """Accepts 284, "284:5" and the packed 18612224 alike, because all three turn up: the first as
    an interface id, the second from us, the third out of the clientscripts and the database.

    A bare number under 65536 reads as an interface. Interface 0's components pack to 0 through 20
    and each is also a real interface id, so write "0:20" when the component is what you meant.
    _ambiguous() says when the choice was live.
    """
    s = str(who).strip()
    if ":" in s:
        a, b = s.split(":", 1)
        return int(a), int(b)
    n = int(s)
    if n > CHILD:
        return _split(n)
    return n, None


def _ambiguous(c, who):
    """True when a bare id could equally have meant a component of interface 0."""
    s = str(who).strip()
    if ":" in s or not s.lstrip("-").isdigit():
        return False
    n = int(s)
    if n > CHILD:
        return False
    return _rows(c, 0, 1) and c.execute(
        "select 1 from config where type='interfaces' and id=?", (n,)).fetchone() is not None



def runtime_built():
    """How many interfaces are containers and nothing else, counted rather than remembered.

    They build their children at runtime, so the cache holds the file and none of the contents.
    Written down as a constant it was wrong by four.
    """
    from .common import DB, cached_scan, tree_fingerprint

    def count():
        per = {}
        for r in connect().execute(
                "select id/65536 iface, data from config where type='interfaces'"):
            try:
                d = json.loads(r["data"])
            except ValueError:
                d = {}
            per.setdefault(r["iface"], set()).add(_kind(d))
        return sum(1 for k in per.values() if k == {"container"})

    return cached_scan("runtime-built-interfaces", tree_fingerprint([DB]), count)


def _building_scripts(iface, limit=8):
    """Clientscripts that name this interface, from the reference index.

    A starting point, not an answer. The cache cannot say what a runtime-built interface draws and
    neither can one hop through the scripts; what these give is where the trail begins."""
    try:
        from .scriptrefs import tool_script_refs
        got = tool_script_refs({"kind": "iface", "id": str(iface), "limit": limit})
    except Exception:
        return None
    scripts = [s["script"] for s in (got.get("scripts") or [])]
    if not scripts:
        return None
    return {"scripts": scripts, "found": got.get("found"),
            "how": "rs3_script_read on one of these, and follow the calls; the construction is "
                   "usually several deep. rs3_script_refs kind=iface id=%d lists them all."
                   % iface}


def tool_iface(args):
    c = connect()
    limit = min(int(args.get("limit", 80)), 500)

    text = args.get("text")
    if text:
        hits = []
        needle = str(text).strip().lower()
        for cid, name, drawn in _drawn_text(c):
            if needle in drawn.lower():
                iface, comp = _split(cid)
                hits.append({"at": "%d:%d" % (iface, comp), "interface": iface,
                             "name": name, "text": drawn})
                if len(hits) >= limit:
                    break
        live = ifacelive.find_text(needle, limit)
        out = {"found": len(hits), "components": hits,
               "note": "components whose drawn text contains that. The string is the one baked "
                       "into the interface, so anything the game fills in at runtime is not here."}
        # runtime text is in no cache, only dumps
        if live:
            out["fromLiveDumps"] = live
            out["note"] += (" fromLiveDumps is the other half: text found in interface dumps, "
                            "where the cache has nothing to search.")
        return out

    search = args.get("search") or args.get("name")
    if search and args.get("id") is None:
        s = str(search).strip()
        rows = c.execute(
            "select id/65536 iface, name, count(*) n from config where type='interfaces' "
            "and name like ? collate nocase group by iface, name order by iface limit ?",
            ("%" + s + "%", limit)).fetchall()
        return {"found": len(rows),
                "interfaces": [{"interface": r["iface"], "name": r["name"],
                                "components": r["n"]} for r in rows],
                "note": "ask again with id= for the components of one of them"}

    who = args.get("id")
    if who is None:
        n = c.execute("select count(distinct id/65536) from config where type='interfaces'"
                      ).fetchone()[0]
        out = {"interfaces": n,
               "components": c.execute("select count(*) from config where type='interfaces'"
                                       ).fetchone()[0],
               "note": "ids are packed interface<<16|component. Pass id=284, id=\"284:5\" or a "
                       "raw packed id, or search= by name, or text= by what is drawn on screen."}
        # which ones have been dumped, so gaps show
        seen = ifacelive.dumped()
        if seen:
            out["liveDumps"] = seen
            out["note"] += (" liveDumps are the interfaces there are dumps of, the only way to see "
                            "inside the %d that build themselves at runtime." % runtime_built())
        return out

    iface, comp = _parse(who)
    name = _name(c, iface)
    if name is None and not _rows(c, iface, 1):
        return {"found": 0, "interface": iface,
                "note": "no interface with that id. If it came in packed, %d:%d is how it splits."
                        % (iface, comp or 0)}

    if comp is not None:
        r = c.execute("select id, data from config where type='interfaces' and id=?",
                      (_pack(iface, comp),)).fetchone()
        if r is None:
            return {"found": 0, "at": "%d:%d" % (iface, comp), "interface": iface, "name": name,
                    "note": "that interface exists but has no such component"}
        d = json.loads(r["data"])
        out = {"interface": iface, "name": name, "packedID": r["id"]}
        out.update(_brief(r["id"], d))
        block = BLOCK.get(_kind(d))
        if block and d.get(block):
            out["detail"] = d[block]
        kids = [_split(x["id"])[1] for x in _rows(c, iface)
                if json.loads(x["data"]).get("parent") == comp]
        if kids:
            out["children"] = kids[:limit]
        out["note"] = ("packedID is the packed form the clientscripts use. detail is the "
                       "raw block for this kind, untouched.")
        return out

    rows = _rows(c, iface)
    comps = []
    kinds = {}
    roots = []
    for r in rows:
        d = json.loads(r["data"])
        b = _brief(r["id"], d)
        kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
        if b["parent"] is None:
            roots.append(b["component"])
        comps.append(b)
    # the root is rarely component 0
    out = {"interface": iface, "name": name, "components": len(comps),
           "root": roots[0] if len(roots) == 1 else roots,
           "kinds": kinds, "packedBase": _pack(iface, 0),
           "children": comps[:limit],
           "truncated": ("%d of %d shown, raise limit" % (limit, len(comps))
                         if len(comps) > limit else None),
           "note": "parent is the component this one hangs off, null for the root. Add "
                   "component= or ask by \"%d:n\" for one on its own." % iface}
    # a skeleton of the interface, not the screen
    seen = ifacelive.summary(iface, limit)
    if seen is not None:
        out["live"] = seen

    # which scripts name it, whatever it holds
    refs = _building_scripts(iface)
    if refs:
        out["referencedByScripts"] = refs

    if set(kinds) == {"container"}:
        out["builtAtRuntime"] = True
        out["note"] += (" Every component here is a container, so this interface is only a "
                        "skeleton -- whatever it draws is created by the client at runtime and "
                        "is not in the cache.")
        if seen is not None:
            out["note"] += " live is a dump of it taken while the game ran, which is where the contents are."
    if _ambiguous(c, who):
        out["alsoAValidPackedID"] = "0:%d" % iface
        out["note"] += (" That number is also the packed id of interface 0 component %d. It was "
                        "read as an interface because that is the commoner question; ask for "
                        "\"0:%d\" if the component was what you meant." % (iface, iface))
    return out


TOOLS = [
    {"name": "rs3_iface", "fn": tool_iface,
     "description": "Interfaces and their components, decoded. Splits a packed "
                    "interface<<16|component id, lists a component's kind, parent, position and "
                    "right click options, or searches by name. text= searches what is drawn on "
                    "screen, the fastest route from something visible to its id. "
                    "referencedByScripts names the clientscripts that build it.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "284, \"284:5\", or a raw packed id"},
         "name": {"type": "string", "description": "interface name, exact or partial"},
         "search": {"type": "string", "description": "list interfaces whose name contains this"},
         "text": {"type": "string", "description": "find components drawing this text"},
         "limit": {"type": "integer", "description": "max components or hits, default 80"}}}},
]
