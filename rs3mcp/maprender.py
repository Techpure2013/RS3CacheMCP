"""A detailed map of mapsquares, drawn the way the game's minimap draws them, 4 pixels a tile.

    python -m rs3mcp.maprender <x0> <z0> <x1> <z1> <out.png> [level] [zoom]

Underlays blend over the 5 x 5 tiles around each, the heights shade it, overlays fill the part of a
tile their shape covers (the classic twelve 4 x 4 masks, turned by rotation), and an overlay drawn
with a material takes that texture's average colour. Walls are white lines and doors red; objects
with a map scene (trees, rocks) and a map function (banks, shops) put their sprites on top. Water is
mapsquare file 4, painted in the game world map's colours: grey-blue, or teal where that map has swamp.
"""

import json
import sqlite3
import sys

from . import cachefile, common, maplocs, maptiles, mapwalk

PX = 4
SIDE = maptiles.SIDE
SPRITES = common.SPRITES

# overlay masks per shape, rows from north
SHAPES = [
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 1, 1],
    [1, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
    [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 1],
    [0, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 0, 1, 1],
    [1, 1, 1, 1, 1, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1],
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1],
]
# source pixel per quarter turn
ROTATIONS = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [12, 8, 4, 0, 13, 9, 5, 1, 14, 10, 6, 2, 15, 11, 7, 3],
    [15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    [3, 7, 11, 15, 2, 6, 10, 14, 1, 5, 9, 13, 0, 4, 8, 12],
]
# slope shading per height step, and its cap
SHADE, SHADE_MAX = 0.012, 0.35
NO_COLOUR = (255, 0, 255)
WALL, DOOR = (238, 238, 238), (220, 40, 40)
# the game map's water colours
WATER, SWAMP = (136, 144, 157), (92, 138, 123)
# per-zone world map images
WORLDMAP, IMAGES, SURFACE = 23, 4, 28


class _Configs:
    """Floor colours, material colours, and what objects put on the map, off the database."""

    def __init__(self, root=None):
        self.db = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
        self.root = root
        self.floors = {}
        for kind in ("overlays", "underlays"):
            for cid, d in self.db.execute("select id, data from config where type=?", (kind,)):
                self.floors[(kind, cid)] = json.loads(d)
        self.textures = {}
        self.sprites = {}
        self.objects = {}
        self.surface = None

    def material_colour(self, mid):
        if mid not in self.textures:
            self.textures[mid] = None
            row = self.db.execute("select data from config where type='materials' and id=?", (mid,)).fetchone()
            d = json.loads(row[0]) if row else {}
            # two layouts
            diffuse = (d.get("v0") or {}).get("diffuse") if d.get("v0") else ((d.get("v1") or {}).get("diffuse") or {}).get("texture")
            if diffuse is not None:
                from . import textures
                got = textures.image(diffuse, self.root)
                if got:
                    w, h, px = got
                    n = w * h
                    self.textures[mid] = tuple(sum(px[i::4][:n]) // n for i in range(3))
        return self.textures[mid]

    def floor(self, kind, tile_id):
        """The config a tile's floor id names, or None when it draws nothing: no colour, or magenta
        (rsmv's rule; the floor under it shows instead)."""
        d = self.floors.get((kind, maptiles.config_id(tile_id)))
        c = (d or {}).get("colour")
        if not c or (c["r"], c["g"], c["b"]) == NO_COLOUR:
            return None
        return d

    def colour(self, kind, tile_id):
        """A tile floor's colour, or None when it draws nothing."""
        d = self.floor(kind, tile_id)
        if d is None:
            return None
        c = d["colour"]
        return c["r"], c["g"], c["b"]

    def world_map(self):
        """The game's surface world map, loaded once: .surface (rows bottom up, a pixel a tile),
        .bounds (level 0 remaps) and .origin (the map coord of pixel 0, 0)."""
        if self.surface is None:
            import io
            import struct
            import numpy as np
            from PIL import Image
            root = self.root or cachefile.GAME
            data = cachefile.files(WORLDMAP, IMAGES, root, cachefile.descriptor(WORLDMAP, root))[SURFACE]
            n = struct.unpack_from(">I", data, 0)[0]
            # stored bottom row first
            self.surface = np.asarray(Image.open(io.BytesIO(data[4:4 + n])).convert("RGB"))
            row = self.db.execute("select data from config where type='mapzones' and id=?", (SURFACE,)).fetchone()
            bounds = json.loads(row[0])["bounds"]
            self.bounds = [b for b in bounds if b["plane"] == 0]
            self.origin = (min(b["mapFromX"] for b in bounds), min(b["mapFromZ"] for b in bounds))
        return self

    def water(self, wx, wz):
        """Water's colour at a surface tile: swamp where the game's own world map paints swamp.

        No config marks swamp water; the same type is teal in one marsh and grey in the next river."""
        self.world_map()
        for b in self.bounds:
            if b["fromX"] <= wx <= b["toX"] and b["fromZ"] <= wz <= b["toZ"]:
                px = wx - b["fromX"] + b["mapFromX"] - self.origin[0]
                py = wz - b["fromZ"] + b["mapFromZ"] - self.origin[1]
                if 0 <= py < self.surface.shape[0] and 0 <= px < self.surface.shape[1]:
                    return SWAMP if tuple(self.surface[py, px]) == SWAMP else WATER
        return WATER

    def obj(self, lid):
        if lid not in self.objects:
            row = self.db.execute("select data from config where type='objects' and id=?", (lid,)).fetchone()
            d = json.loads(row[0]) if row else {}
            self.objects[lid] = {k: d.get(k) for k in ("mapscene", "mapFunction", "width", "length")}
        return self.objects[lid]

    def sprite(self, kind, cid):
        """A map scene's or a map function's sprite, as a Pillow image, or None."""
        key = (kind, cid)
        if key not in self.sprites:
            self.sprites[key] = None
            table = "mapscenes" if kind == "scene" else "maplabels"
            row = self.db.execute("select data from config where type=? and id=?", (table, cid)).fetchone()
            sid = (json.loads(row[0]) if row else {}).get("sprite")
            if sid is not None:
                import os
                from PIL import Image
                path = os.path.join(SPRITES, "sprites-%d.png" % sid)
                if os.path.exists(path):
                    self.sprites[key] = Image.open(path).convert("RGBA")
        return self.sprites[key]


def _ground(sx, sz, level, root):
    """{(x, z): tile} of one square as walked on level: a bridge's deck in place of the water."""
    got = maptiles.square(sx, sz, root)
    if got is None:
        return {}
    tiles = got[0]
    out = {}
    for x in range(SIDE):
        for z in range(SIDE):
            p = level + 1 if level < 3 and tiles[1][x][z].get("settings", 0) & maptiles.BRIDGE else level
            out[(sx * SIDE + x, sz * SIDE + z)] = tiles[p][x][z]
    return out


def render(x0, z0, x1, z1, out, level=0, zoom=2, root=None, ground_only=False):
    import numpy as np
    from PIL import Image

    cfg = _Configs(root)
    # one square of margin for blending
    ground = {}
    for sx in range(x0 - 1, x1 + 2):
        for sz in range(z0 - 1, z1 + 2):
            ground.update(_ground(sx, sz, level, root))
    bx, bz = (x0 - 1) * SIDE, (z0 - 1) * SIDE
    W, H = (x1 - x0 + 3) * SIDE, (z1 - z0 + 3) * SIDE
    under = np.zeros((W, H, 3))
    has = np.zeros((W, H))
    height = np.zeros((W, H))
    for (x, z), t in ground.items():
        i, j = x - bx, z - bz
        height[i, j] = t.get("height", 0)
        c = cfg.colour("underlays", t["underlay"]) if "underlay" in t else None
        if c:
            under[i, j] = c
            has[i, j] = 1

    # 5 x 5 underlay blend
    def box(a):
        p = np.pad(a, [(2, 2), (2, 2)] + [(0, 0)] * (a.ndim - 2))
        s = np.zeros_like(a)
        for dx in range(5):
            for dz in range(5):
                s += p[dx:dx + a.shape[0], dz:dz + a.shape[1]]
        return s
    blended = box(under) / np.maximum(box(has), 1)[..., None]

    # light from the north west
    hp = np.pad(height, 1, mode="edge")
    slope = (hp[:-2, 2:] - hp[2:, :-2]) * SHADE
    shade = 1 + np.clip(slope, -SHADE_MAX, SHADE_MAX)

    near = box(has)
    wet = {}
    if level == 0:
        for sx in range(x0, x1 + 1):
            for sz in range(z0, z1 + 1):
                layer = maptiles.water(sx, sz, root)
                for x in range(SIDE if layer else 0):
                    for z in range(SIDE):
                        if "underlay" in layer[x][z]:
                            wet[(sx * SIDE + x, sz * SIDE + z)] = layer[x][z]

    ow, oh = (x1 - x0 + 1) * SIDE, (z1 - z0 + 1) * SIDE
    img = np.zeros((oh * PX, ow * PX, 3), dtype=np.uint8)
    for lx in range(ow):
        for lz in range(oh):
            i, j = lx + SIDE, lz + SIDE
            t = ground.get((bx + i, bz + j), {})
            # a floorless tile takes its neighbours' blend
            base = blended[i, j] if near[i, j] else (0, 0, 0)
            cell = np.empty((PX, PX, 3))
            cell[:] = base
            if "overlay" in t:
                oc = cfg.colour("overlays", t["overlay"])
                if oc is not None:
                    mask, rot = SHAPES[t.get("shape", 0) % len(SHAPES)], ROTATIONS[t.get("rotation", 0)]
                    for k in range(16):
                        if mask[rot[k]]:
                            cell[k // 4, k % 4] = oc
            cell *= shade[i, j]
            w = wet.get((bx + i, bz + j))
            if w is not None:
                # water over the ground; an overlay cuts it, or recolours it when it has a colour
                paint = np.empty((PX, PX, 3))
                paint[:] = cfg.water(bx + i, bz + j)
                keep = np.ones((PX, PX), dtype=bool)
                if "overlay" in w:
                    oc = cfg.colour("overlays", w["overlay"])
                    mask, rot = SHAPES[w.get("shape", 0) % len(SHAPES)], ROTATIONS[w.get("rotation", 0)]
                    for k in range(16):
                        if mask[rot[k]]:
                            if oc is None:
                                keep[k // 4, k % 4] = False
                            else:
                                paint[k // 4, k % 4] = oc
                cell[keep] = paint[keep]
            top = (oh - 1 - lz) * PX
            img[top:top + PX, lx * PX:lx * PX + PX] = np.clip(cell, 0, 255)

    pic = Image.fromarray(img, "RGB")
    if not ground_only:
        _walls(pic, x0, z0, x1, z1, oh, level, root)
        _icons(pic, cfg, x0, z0, x1, z1, oh, level, root)
    if zoom > 1:
        pic = pic.resize((pic.width * zoom, pic.height * zoom), Image.NEAREST)
    pic.save(out)
    return out


def _walls(pic, x0, z0, x1, z1, oh, level, root):
    from PIL import ImageDraw
    grid = mapwalk.Grid(x0, z0, x1, z1, level, root)
    draw = ImageDraw.Draw(pic)
    for lz in range(grid.h):
        for lx in range(grid.w):
            f = grid.flags[lx + lz * grid.w]
            if not f & 15:
                continue
            colour = DOOR if (grid.bx + lx, grid.bz + lz) in grid.doors else WALL
            px, py = lx * PX, (oh - 1 - lz) * PX
            if f & mapwalk.N:
                draw.line([px, py, px + PX - 1, py], fill=colour)
            if f & mapwalk.S:
                draw.line([px, py + PX - 1, px + PX - 1, py + PX - 1], fill=colour)
            if f & mapwalk.W:
                draw.line([px, py, px, py + PX - 1], fill=colour)
            if f & mapwalk.E:
                draw.line([px + PX - 1, py, px + PX - 1, py + PX - 1], fill=colour)


def _icons(pic, cfg, x0, z0, x1, z1, oh, level, root):
    """Map scenes first, then map functions over them, centred on the object's footprint."""
    scenes, functions = [], []
    desc = cachefile.descriptor(maplocs.MAJOR, root or cachefile.GAME) or {}
    for sx in range(x0, x1 + 1):
        for sz in range(z0, z1 + 1):
            files = cachefile.files(maplocs.MAJOR, sx + sz * maplocs.STRIDE, root or cachefile.GAME, desc) or {}
            if maplocs.FILE not in files:
                continue
            for lid, uses in maplocs.decode(files[maplocs.FILE]):
                o = cfg.obj(lid)
                if o["mapscene"] is None and o["mapFunction"] is None:
                    continue
                for p, x, z, shape, rot, _extra in uses:
                    if p != level:
                        continue
                    w, l = (o["width"] or 1, o["length"] or 1) if rot in (0, 2) else (o["length"] or 1, o["width"] or 1)
                    cx = ((sx - x0) * SIDE + x + w / 2) * PX
                    cy = (oh - ((sz - z0) * SIDE + z + l / 2)) * PX
                    if o["mapscene"] is not None:
                        scenes.append((cx, cy, cfg.sprite("scene", o["mapscene"])))
                    if o["mapFunction"] is not None:
                        functions.append((cx, cy, cfg.sprite("function", o["mapFunction"])))
    for cx, cy, s in scenes + functions:
        if s is not None:
            pic.paste(s, (int(cx - s.width / 2), int(cy - s.height / 2)), s)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 5:
        raise SystemExit(__doc__)
    print(render(*map(int, args[:4]), out=args[4], level=int(args[5]) if len(args) > 5 else 0,
                 zoom=int(args[6]) if len(args) > 6 else 2))
