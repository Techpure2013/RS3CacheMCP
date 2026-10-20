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
planks, checks and grey slabs the game shows, and rsmv looks them up the same way.

Every square in the cache reads this way to the end of its tiles: shapes 0 to 11 and rotations 0
to 3 only. After the tiles each square carries more,
8 bytes that are all zeros or all ones, then floats among other things, likely lighting and fog; it is
kept as bytes. Water is file 4, its own layer: see water().
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

BLOCKED, BRIDGE, ROOF = 1, 2, 4


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
