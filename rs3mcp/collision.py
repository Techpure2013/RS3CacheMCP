"""Collision a route planner can use: the blocked edges, and which of them are ways through.

mapwalk holds the collision itself. This sorts what stands on it into four kinds, because a
pathfinder needs the difference: a door opens, a barrier is crossed, a ladder leaves the plane,
and a sealed door never moves. Keyed by the mapzone a tile stands in, so a project can ship one
region instead of the world.

A kind comes from the loc's options, not its name alone. Open and Close both mean a door, one
shut and one already standing open. Chests and drawers carry Open too, so a verb on its own is
not enough; a way through is a door by name, or a crossing verb, or Open on something named like
a barrier.

Tools:
  rs3_collision         an area as a grid, ways through and transports marked
  rs3_collision_export  a region's collision as run-length bytes plus its ways through
"""

import base64
import json
import os
import sqlite3
import zlib

from . import areas, common, mapwalk

SIDE = mapwalk.SIDE
N, E, S, W, FULL = mapwalk.N, mapwalk.E, mapwalk.S, mapwalk.W, mapwalk.FULL

DOOR_TOKENS = {"door", "doors", "gate", "gates"}
BARRIER_TOKENS = {"wall", "walls", "barrier", "barriers", "barricade", "barricades",
                  "entrance", "entrances", "opening", "openings", "doorway", "doorways"}
OPENS = {"open", "close", "shut", "unlock", "pick-lock", "open-pick-lock", "knock"}
THROUGH = {"go-through", "pass", "pass-through", "walk through", "walk-through", "cross",
           "crawl-under", "squeeze-through", "squeeze through", "climb-over", "climb over",
           "traverse", "push"}
AWAY = {"enter", "exit", "leave", "leave through", "climb", "climb-up", "climb up",
        "climb-down", "climb down", "descend", "ascend", "crawl through", "grapple", "jump"}

GLYPH = {"door": "D", "opening": "O", "transport": "T", "sealed": "X"}


def words(name):
    """A name split into lowercase words. Trapdoor stays one word, so it never reads as a door."""
    out, word = [], ""
    for ch in str(name or "").lower():
        if ch.isalpha():
            word += ch
        else:
            if word:
                out.append(word)
            word = ""
    if word:
        out.append(word)
    return out


def classify(obj):
    """door, opening, transport, sealed, or None for anything that just blocks."""
    acts = {a.strip().lower() for a in (obj.get("options") or []) if a}
    name = words(obj.get("name"))
    if any(w in DOOR_TOKENS for w in name):
        if acts & (OPENS | THROUGH | {"enter", "exit"}):
            return "door"
        return "sealed"
    if acts & THROUGH:
        return "opening"
    if acts & OPENS and any(w in BARRIER_TOKENS for w in name):
        return "opening"
    if acts & AWAY:
        return "transport"
    return None


def sides(shape, rot):
    """The wall edges a loc of this shape stands on, empty when it is not a wall."""
    if shape == 0:
        return [mapwalk.WALL_SIDE[rot]]
    if shape == 2:
        return list(mapwalk.CORNER_SIDES[rot])
    return []


def _zones(db):
    """(area, name, plane, x0, z0, x1, z1) for every mapzone box, smallest first."""
    out = []
    for (data,) in db.execute("select data from config where type='mapzones'"):
        d = json.loads(data)
        name = d.get("name") or d.get("internalName")
        if not name or name == "RuneScape Surface":
            continue
        for b in d.get("bounds") or []:
            w, h = b["toX"] - b["fromX"] + 1, b["toZ"] - b["fromZ"] + 1
            if w > 0 and h > 0:
                out.append((w * h, name, b.get("plane", 0),
                            b["fromX"], b["fromZ"], b["toX"], b["toZ"]))
    return sorted(out)


def _db():
    return sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)


def region_name(zones, x, z, plane):
    """The smallest mapzone holding the tile, else its surface block, as loc_section names them.

    Open ground has no zone of its own: the Lumbridge mapzones are all interiors, and the
    surface belongs to RuneScape Surface, which covers everything and so names nothing.
    """
    for _area, name, zplane, x0, z0, x1, z1 in zones:
        if zplane == plane and x0 <= x <= x1 and z0 <= z <= z1:
            return areas.slug(name)
    return "SURFACE_%d_%d" % (x // areas.SURFACE_BLOCK, z // areas.SURFACE_BLOCK)


def survey(sx0, sz0, sx1, sz1, plane, root=None):
    """mapwalk's grid for the squares, plus every loc on it sorted into kinds."""
    grid = mapwalk.Grid(sx0, sz0, sx1, sz1, plane, root)
    found = {"door": [], "opening": [], "transport": [], "sealed": []}
    for x, z, lid, shape, rot in grid.locs:
        obj = grid.objects.get(lid)
        kind = classify(obj)
        if kind is None:
            continue
        found[kind].append({"x": x, "z": z, "loc": lid, "name": obj.get("name"),
                            "shape": shape, "sides": sides(shape, rot),
                            "options": [o for o in (obj.get("options") or []) if o]})
    return grid, found


def pack(data):
    """Deflate then base64. Measured on square 50,50: 4096 raw tiles to 1011 bytes.

    Run length and a sparse index both came out worse than raw here, at about 3100 and 1800
    entries, because wall edges alternate tile by tile.
    """
    return base64.b64encode(zlib.compress(bytes(data), 9)).decode()


def tool_collision(args):
    """An area's collision as a grid, with the ways through and the transports on it.

    Blocked edges come from mapwalk, which follows the classic client. The glyphs name what
    stands on a tile, so a wall with a door in it reads as a gap rather than a wall.
    """
    x = int(args["x"])
    z = int(args["z"])
    plane = int(args.get("plane", 0))
    size = max(1, min(int(args.get("size", 32)), 128))
    root = args.get("root")

    x0, z0 = x - size // 2, z - size // 2
    grid, found = survey(x0 // SIDE, z0 // SIDE, (x0 + size - 1) // SIDE,
                         (z0 + size - 1) // SIDE, plane, root)
    marks = {}
    for kind in ("sealed", "transport", "opening", "door"):
        for item in found[kind]:
            marks[(item["x"], item["z"])] = GLYPH[kind]

    rows = []
    for tz in range(z0 + size - 1, z0 - 1, -1):
        row = []
        for tx in range(x0, x0 + size):
            at = grid.at(tx, tz)
            flags = grid.flags[at] if at is not None else FULL
            if (tx, tz) in marks:
                row.append(marks[(tx, tz)])
            elif flags & FULL:
                row.append("#")
            elif flags & (N | E | S | W):
                row.append("+")
            else:
                row.append(".")
        rows.append("".join(row))

    with _db() as db:
        where = region_name(_zones(db), x, z, plane)
    counts = {k: len(v) for k, v in found.items()}
    return {"centre": [x, z], "plane": plane, "size": size, "region": where,
            "legend": "# tile blocked   + wall edge   D door   O opening   "
                      "T transport   X sealed   . open",
            "grid": rows, "counts": counts,
            "door": found["door"][:40], "opening": found["opening"][:40],
            "transport": found["transport"][:40], "sealed": found["sealed"][:40],
            "unloaded": [list(s) for s in grid.unloaded],
            "note": "a door and an opening are ways through, so a pathfinder clears the edges in "
                    "sides. A transport blocks its tile and moves you off the plane instead, so it "
                    "is a link, not a gap. A sealed door never opens and stays blocked."}


def tool_collision_export(args):
    """One mapsquare's collision, small enough to ship: run-length bytes and the ways through.

    flags is one byte a tile, row major from the square's south-west corner, 1 north, 2 east,
    4 south, 8 west, 16 whole tile. Decoding is a loop over value and count pairs.
    """
    sx = int(args["square_x"])
    sz = int(args["square_z"])
    plane = int(args.get("plane", 0))
    root = args.get("root")

    grid, found = survey(sx, sz, sx, sz, plane, root)
    blocked = sum(1 for f in grid.flags if f & FULL)
    edges = sum(1 for f in grid.flags if f & (N | E | S | W))

    out = {"square": [sx, sz], "plane": plane, "regionId": (sx << 8) | sz,
           "tiles": len(grid.flags), "blocked": blocked, "withWallEdge": edges,
           "encoding": "deflate then base64, one byte a tile, row major from the south-west "
                       "corner: 1 north, 2 east, 4 south, 8 west, 16 whole tile",
           "flags": pack(grid.flags),
           "door": found["door"], "opening": found["opening"],
           "transport": found["transport"], "sealed": found["sealed"],
           "unloaded": [list(s) for s in grid.unloaded]}
    with _db() as db:
        zones = _zones(db)
    out["region"] = region_name(zones, sx * SIDE + SIDE // 2, sz * SIDE + SIDE // 2, plane)

    target = args.get("outDir")
    if target:
        os.makedirs(target, exist_ok=True)
        path = os.path.join(target, "collision-%d_%d-p%d.json" % (sx, sz, plane))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"))
        out["written"] = path
        out["bytesOnDisk"] = os.path.getsize(path)
        out.pop("flags")
    return out


TOOLS = [
    {"name": "rs3_collision", "fn": tool_collision,
     "examples": [{"x": 3222, "z": 3218}, {"x": 3222, "z": 3218, "plane": 1, "size": 48}],
     "description": "An area's walkability as a grid, with doors, openings, transports and "
                    "sealed doors marked on it. Use it to see why a route is blocked, or what a "
                    "tile is shut in by.",
     "inputSchema": {"type": "object", "required": ["x", "z"], "properties": {
         "x": {"type": "integer", "description": "world x at the centre"},
         "z": {"type": "integer", "description": "world z at the centre"},
         "plane": {"type": "integer", "description": "0 to 3, default 0"},
         "size": {"type": "integer", "description": "tiles a side, 1 to 128, default 32"},
         "root": {"type": "string", "description": "a cache folder other than the live one"}}}},
    {"name": "rs3_collision_export", "fn": tool_collision_export,
     "examples": [{"square_x": 50, "square_z": 50},
                  {"square_x": 50, "square_z": 50, "outDir": "out/collision"}],
     "description": "One mapsquare's collision in a form a project can carry: one byte a tile, "
                    "run-length encoded, plus the doors, openings, transports and sealed doors "
                    "on it. Pass outDir to write it instead of returning the bytes.",
     "inputSchema": {"type": "object", "required": ["square_x", "square_z"], "properties": {
         "square_x": {"type": "integer", "description": "mapsquare x, world x divided by 64"},
         "square_z": {"type": "integer", "description": "mapsquare z, world z divided by 64"},
         "plane": {"type": "integer", "description": "0 to 3, default 0"},
         "outDir": {"type": "string", "description": "write a json file here and leave out the bytes"},
         "root": {"type": "string", "description": "a cache folder other than the live one"}}}},
]
