"""The ground of every mapsquare: index 5, one group per mapsquare, file 3.

    python -m rs3mcp.maptiles <x> <z>                    one mapsquare's plane 0, summed up
    python -m rs3mcp.maptiles verify                     every mapsquare, decoded to its last tile
    python -m rs3mcp.maptiles png <x0> <z0> <x1> <z1> <out.png>   a map of those squares, north up

"jagx" and a version byte, then 4 planes of 64 x 64 tiles, x outer and z inner. Each tile is a
flags byte and the fields it names, in this order:

    0x01  overlay: u8 shape << 2 | rotation, then the overlay id (a smart)
    0x02  settings (a smart): 1 blocks walking, 2 a bridge, 4 a roof, 8 and 16 unnamed
    0x04  underlay id (a smart)
    0x08  height, u16

A tile's overlay and underlay ids count from 1: id n is config n - 1 (config_id). Read as configs
straight, Lumbridge keep's floors came out water, grey brick and pink brick; one less, they are the
planks, checks and grey slabs the game shows.

Every square in the cache reads this way to the end of its tiles: shapes 0 to 11 and rotations 0
to 3 only. After the tiles each square carries more,
8 bytes that are all zeros or all ones, then floats among other things, likely lighting and fog; it is
kept as bytes. Water is file 4, its own layer: see water().

File 5 holds the same ground again, as the NXT tiles (decode_nxt, ground): what the scenes draw.
How its fields line up with files 3 and 4 was measured 2026-11-05 on every 7th square holding both,
tile by tile, about 12 million tiles (rs3cache/worldmap/work/nxt_vs_old.py):
  - flags 2, 4, 8, 32 and 64 are set exactly where file 3 sets settings 1, 2, 4, 8 and 16
  - flag 16 is set on 918,960 of the 921,789 tiles file 4 holds water on, and on 2,829 others
  - a water tile's overlay_under is file 3's overlay on that tile (112 on 112 908,583 times)
  - heights are file 3's (identical where both store one); a water tile's waterheight is file 3's
    height there and its height is file 4's
  - a water tile's stored shape maps to the shape file 3 drew its surface with by _SURFACE, each
    pair seen 400 to 1,200 times and over 99.8% one way
  - of 2.56 million upper floor tiles that store a height, 45% store 30, the step a blank one takes
"""

import struct
import sys

from . import cachefile

MAJOR = 5
FILE = 3
WATER = 4
STRIDE = 128
MAGIC = b"jagx"
PLANES, SIDE = 4, 64

BLOCKED, BRIDGE, ROOF, BELOW = 1, 2, 4, 8


class Stop(Exception):
    pass


def config_id(tile_id):
    """The overlays or underlays config a tile's floor id names, or None for 0."""
    return tile_id - 1 if tile_id else None


def _smart(b, i):
    if i >= len(b):
        raise Stop("ran out at %d" % i)
    if b[i] & 0x80:
        if i + 2 > len(b):
            raise Stop("ran out at %d" % i)
        return ((b[i] << 8) | b[i + 1]) & 0x7FFF, i + 2
    return b[i], i + 1


def decode(data, planes_in=PLANES):
    """(tiles, rest): tiles[plane][x][z] a dict of the fields present, rest the bytes after them."""
    if data[:4] != MAGIC:
        raise Stop("no jagx magic")
    i = 5
    planes = []
    for _ in range(planes_in):
        columns = []
        for _ in range(SIDE):
            column = []
            for _ in range(SIDE):
                if i >= len(data):
                    raise Stop("ran out at %d" % i)
                f = data[i]
                i += 1
                if f & 0xF0:
                    raise Stop("flags %02x at %d" % (f, i - 1))
                t = {}
                if f & 0x01:
                    if i >= len(data):
                        raise Stop("ran out at %d" % i)
                    t["shape"] = data[i] >> 2
                    t["rotation"] = data[i] & 3
                    t["overlay"], i = _smart(data, i + 1)
                if f & 0x02:
                    t["settings"], i = _smart(data, i)
                if f & 0x04:
                    t["underlay"], i = _smart(data, i)
                if f & 0x08:
                    if i + 2 > len(data):
                        raise Stop("ran out at %d" % i)
                    t["height"] = struct.unpack_from(">H", data, i)[0]
                    i += 2
                column.append(t)
            columns.append(column)
        planes.append(columns)
    return planes, data[i:]


def square(x, z, root=None):
    """(tiles, rest) for mapsquare x, z, or None when it has no ground."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    g = x + z * STRIDE
    if g not in desc or FILE not in desc[g]["files"]:
        return None
    files = cachefile.files(MAJOR, g, root, desc) or {}
    return decode(files[FILE]) if FILE in files else None


def water(x, z, root=None):
    """tiles[x][z] of mapsquare x, z's water layer, or None when it has none.

    File 4, one plane in the ground's tile format, read to its end in every square that has it. A
    tile with an underlay is water of that type; overlay 42 (config 41, the magenta draw-nothing floor) cuts
    it back to its shape, which is how banks come out diagonal. An all-zero file is no water."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    g = x + z * STRIDE
    if g not in desc or WATER not in desc[g]["files"]:
        return None
    data = (cachefile.files(MAJOR, g, root, desc) or {}).get(WATER)
    if not data or data[:4] != MAGIC:
        return None
    return decode(data, 1)[0][0]


NXT, NXT_SIDE = 5, 66
# nxt flag -> file 3 settings bit (measured)
NXT_SETTINGS = ((2, BLOCKED), (4, BRIDGE), (8, ROOF), (32, BELOW))
NXT_WATER = 16
# stored water shape -> surface (shape, turns), measured
_SURFACE = {0: (0, 0), 1: (1, 2), 2: (4, 0), 3: (5, 0), 4: (2, 0), 5: (3, 0), 6: (6, 2), 7: (8, 0), 8: (7, 0),
            9: (10, 0), 10: (9, 0), 11: (11, 0)}
# blank upper floor step, the commonest stored
FLOOR_STEP = 30


def decode_nxt(data):
    """[plane][x + 1][z + 1] -> dict, one square's NXT tiles (file 5) with a tile of its neighbours'
    all round. Each tile: flags, height, and when it has a floor (flag 1) underlay with its blended
    colour, overlay with shape; a water tile (flag 16) adds waterheight, overlay_under (its surface's
    floor, file 3's overlay there on every tile compared) and underlay_under. Read to the last byte
    on every square with ground."""
    if data[:4] != MAGIC:
        raise Stop("no jagx magic")

    def u16(i):
        return (data[i] << 8) | data[i + 1], i + 2

    def var(i):
        return (((data[i] << 8) | data[i + 1]) & 0x7FFF, i + 2) if data[i] & 0x80 else (data[i], i + 1)

    i, planes = 5, [None] * PLANES
    while i < len(data):
        op = data[i]
        i += 1
        if op >= PLANES:
            raise Stop("plane %d at %d" % (op, i - 1))
        cells = []
        for _ in range(NXT_SIDE * NXT_SIDE):
            f = data[i]
            t = {"flags": f}
            t["height"], i = u16(i + 1)
            if f & 1:
                if f & NXT_WATER:
                    t["waterheight"], i = u16(i)
                t["underlay"], i = var(i)
                if t["underlay"]:
                    t["underlaycolor"], i = u16(i)
                t["overlay"], i = var(i)
                if f & NXT_WATER:
                    t["overlay_under"], i = var(i)
                if t["overlay"]:
                    t["shape"] = data[i]
                    i += 1
                    if f & NXT_WATER:
                        t["underlay_under"], i = var(i)
            cells.append(t)
        planes[op] = [cells[k * NXT_SIDE:(k + 1) * NXT_SIDE] for k in range(NXT_SIDE)]
    return planes


def grid(x, z, root=None):
    """ground() as int arrays [plane, x, z]: height, settings, water (1 where ground() gives the tile a
    sink), sink, underlay, overlay, shape and rotation (0 where ground() leaves the key out), and the
    water surfaces' surface, overlay, shape and rotation (surface 1 where water[plane] holds the tile). None when the square has no ground,
    or when mapcore is not built. Kept while the cache is frozen; treat it as read only."""
    return cachefile.kept("grid", (x, z, root), lambda: _grid(x, z, root))


def _grid(x, z, root):
    import numpy as np
    from . import mapcore
    if mapcore.lib() is None:
        return None
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    g = x + z * STRIDE
    if g not in desc or NXT not in desc[g]["files"]:
        return None
    data = (cachefile.files(MAJOR, g, root, desc) or {}).get(NXT)
    if not data:
        return None
    t = mapcore.nxt_decode(data)[:, 1:SIDE + 1, 1:SIDE + 1]
    present = t[..., mapcore.PRESENT].astype(bool)
    f = np.where(present, t[..., mapcore.FLAGS], 0)
    stored = np.where(present, t[..., mapcore.HEIGHT], 0)
    keys = t[..., mapcore.KEYS]
    water = f & NXT_WATER != 0
    if (water & (keys & 1 == 0)).any():
        raise Stop("a water tile with no water height")
    raw = np.where(water, t[..., mapcore.WATERHEIGHT], stored)
    upper = np.arange(PLANES)[:, None, None] > 0
    floor = f & 1 != 0
    shape = t[..., mapcore.SHAPE]
    out = {"height": np.where((raw != 0) | ~upper, raw, FLOOR_STEP),
           "settings": sum(np.where(f & flag, bit, 0) for flag, bit in NXT_SETTINGS),
           "water": water.astype(np.int64),
           "sink": np.where(water, stored, 0),
           "underlay": np.where(floor, t[..., mapcore.UNDERLAY], 0)}
    dry = floor & ~water & (t[..., mapcore.OVERLAY] != 0)
    out["overlay"] = np.where(dry, t[..., mapcore.OVERLAY], 0)
    out["shape"] = np.where(dry, shape >> 2, 0)
    out["rotation"] = np.where(dry, shape & 3, 0)
    # water surfaces
    wet = floor & water & (t[..., mapcore.OVERLAY_UNDER] != 0)
    drawn = np.full(64, -1)
    turns = np.zeros(64, np.int64)
    for stored_shape, (d, turn) in _SURFACE.items():
        drawn[stored_shape], turns[stored_shape] = d, turn
    if (floor & water & (drawn[shape >> 2] < 0)).any():
        raise Stop("a water shape with no surface")
    out["surface"] = wet.astype(np.int64)
    out["surface_overlay"] = np.where(wet, t[..., mapcore.OVERLAY_UNDER], 0)
    out["surface_shape"] = np.where(wet, drawn[shape >> 2], 0)
    out["surface_rotation"] = np.where(wet, ((shape & 3) + turns[shape >> 2]) & 3, 0)
    return {k: np.ascontiguousarray(v, np.int64) for k, v in out.items()}


def ground(x, z, root=None):
    """(tiles, water) for mapsquare x, z from its NXT tiles (file 5), or None when it has none.
    tiles[plane][x][z] has the old fields (underlay, overlay, shape, rotation, settings, height);
    water[plane] is {(x, z): the surface's overlay, shape and rotation}.

    The 5,242 squares with ground have a file 5 with data; 3,473 more carry an empty one. Heights are
    as stored, which is file 3's own on every tile compared; an upper floor storing none steps
    FLOOR_STEP. A water tile stands at waterheight, file 3's height there, and its floor is the bed
    height below, file 4's water height there; the bed's own overlays lie under the surface and are
    left out. sink is how far a tile's floor lies under its height, so a column's heights still add
    up plane on plane. Treat what comes back as read only."""
    return cachefile.kept("ground", (x, z, root), lambda: _ground(x, z, root))


def _ground(x, z, root):
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    g = x + z * STRIDE
    if g not in desc or NXT not in desc[g]["files"]:
        return None
    data = (cachefile.files(MAJOR, g, root, desc) or {}).get(NXT)
    if not data:
        return None
    nxt = decode_nxt(data)
    tiles, wet = [], []
    for p in range(PLANES):
        rows, surface = [], {}
        for a in range(SIDE):
            row = []
            for b in range(SIDE):
                n = nxt[p][a + 1][b + 1] if nxt[p] else {"flags": 0, "height": 0}
                f = n["flags"]
                water_tile = f & NXT_WATER
                raw = n.get("waterheight") if water_tile else n["height"]
                t = {"height": raw if raw or not p else FLOOR_STEP,
                     "settings": sum(bit for flag, bit in NXT_SETTINGS if f & flag)}
                if water_tile:
                    t["sink"] = n["height"]
                if f & 1:
                    if n.get("underlay"):
                        t["underlay"] = n["underlay"]
                    shape = n.get("shape", 0)
                    if water_tile:
                        drawn, turn = _SURFACE[shape >> 2]
                        if n.get("overlay_under"):
                            surface[(a, b)] = {"overlay": n["overlay_under"], "shape": drawn,
                                               "rotation": ((shape & 3) + turn) & 3}
                    elif n.get("overlay"):
                        t.update(overlay=n["overlay"], shape=shape >> 2, rotation=shape & 3)
                row.append(t)
            rows.append(row)
        tiles.append(rows)
        wet.append(surface)
    return tiles, wet


def squares(root=None):
    """(x, z, bytes) for every mapsquare that has ground."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    for g in sorted(desc):
        if FILE not in desc[g]["files"]:
            continue
        files = cachefile.files(MAJOR, g, root, desc) or {}
        if FILE in files:
            yield g % STRIDE, g // STRIDE, files[FILE]


def verify(root=None):
    good = bad = 0
    first = None
    for x, z, data in squares(root):
        try:
            decode(data)
            good += 1
        except (Stop, IndexError, struct.error) as e:
            bad += 1
            first = first or (x, z, str(e))
    return {"squares": good + bad, "decoded": good, "firstProblem": first}


def png(x0, z0, x1, z1, out, scale=4, root=None):
    """Plane 0 of squares x0..x1, z0..z1, north up: overlay colour over underlay, blocked tiles darker."""
    import json
    import sqlite3
    from PIL import Image

    from .common import DB
    colour = {}
    db = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    for kind in ("overlays", "underlays"):
        for cid, d in db.execute("select id, data from config where type=?", (kind,)):
            c = json.loads(d).get("colour")
            colour[(kind, cid)] = (c["r"], c["g"], c["b"]) if c else None
    w, h = (x1 - x0 + 1) * SIDE, (z1 - z0 + 1) * SIDE
    img = Image.new("RGB", (w, h))
    px = img.load()
    for sx in range(x0, x1 + 1):
        for sz in range(z0, z1 + 1):
            got = square(sx, sz, root)
            if got is None:
                continue
            ground = got[0][0]
            for x in range(SIDE):
                for z in range(SIDE):
                    t = ground[x][z]
                    c = colour.get(("overlays", config_id(t["overlay"]))) if "overlay" in t else None
                    if c is None and "underlay" in t:
                        c = colour.get(("underlays", config_id(t["underlay"])))
                    c = c or (16, 16, 16)
                    if t.get("settings", 0) & BLOCKED:
                        c = tuple(v * 3 // 5 for v in c)
                    px[(sx - x0) * SIDE + x, h - 1 - ((sz - z0) * SIDE + z)] = c
    img = img.resize((w * scale, h * scale), Image.NEAREST)
    img.save(out)
    return out


def _summary(x, z):
    got = square(x, z)
    if got is None:
        return {"square": [x, z], "ground": None}
    tiles, rest = got
    ground = [t for column in tiles[0] for t in column]
    return {"square": [x, z], "blocked": sum(1 for t in ground if t.get("settings", 0) & BLOCKED),
            "overlaid": sum(1 for t in ground if "overlay" in t),
            "heights": [min(t.get("height", 0) for t in ground), max(t.get("height", 0) for t in ground)],
            "restBytes": len(rest)}


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    if args[:1] == ["verify"]:
        print(json.dumps(verify()))
    elif args[:1] == ["png"]:
        print(png(*map(int, args[1:5]), out=args[5]))
    elif len(args) == 2:
        print(json.dumps(_summary(int(args[0]), int(args[1]))))
    else:
        raise SystemExit(__doc__)
