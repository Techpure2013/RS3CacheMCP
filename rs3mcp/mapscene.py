"""A 3D scene of the map around a tile, as a binary glTF (.glb) any viewer opens.

    python -m rs3mcp.mapscene <x> <z> <out.glb> [level] [radius]

The ground is one mesh wearing the detailed map as its texture. Each loc is a node on its object's
models, stored once however often used: turned, mirrored, scaled, set on the ground and moved by
offsets whose y points down. A morphing loc shows what its var picks (every var 0 unless given).
An L corner draws its model twice, mirrored at its rotation and plain a quarter turn on. Roofs and
higher levels are left out, as indoors.

A tile is TILE wide and y is up; each level's height adds to the one below. HEIGHT, the units per
stored step, is measured: a live worldscan's loc heights fit it with a median miss of 4 units.
Built in world axes (x east, z north) under a root that flips z.
"""

import array
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile

from . import cachefile, common, maplocs, mapnear, maptiles, mapwalk, models

TILE = 512
HEIGHT = 32
ROOFS = range(12, 22)
# texture edge, pixels
TEXTURE_MAX = 256
# textured-face colour meaning no tint
UNTINTED = 0xFF80
WATER_ALPHA = 0.85
# land past the edge
FAR, FAR_STEP, FAR_SINK = 128, 4, 24
# world map edge lines, darker than this
OUTLINE = 40
# hsl bits: 6 hue, 3 saturation, 7 lightness
BRIGHTNESS = 0.8


def hsl_rgb(c):
    """A model's 16-bit hsl colour as rgb bytes, the classic palette's maths."""
    h = ((c >> 10) & 63) / 64 + 1 / 128
    s = ((c >> 7) & 7) / 8 + 1 / 16
    l = (c & 127) / 128
    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q

        def ch(t):
            t %= 1
            if t < 1 / 6:
                return p + (q - p) * 6 * t
            if t < 1 / 2:
                return q
            if t < 2 / 3:
                return p + (q - p) * (2 / 3 - t) * 6
            return p
        r, g, b = ch(h + 1 / 3), ch(h), ch(h - 1 / 3)
    return tuple(int(255 * (v ** BRIGHTNESS)) for v in (r, g, b))


class _Glb:
    """Just enough glTF: accessors over one buffer, meshes, nodes, one texture."""

    def __init__(self):
        self.bin = bytearray()
        self.gltf = {"asset": {"version": "2.0", "generator": "rs3mcp.mapscene"}, "buffers": [{}],
                     "bufferViews": [], "accessors": [], "meshes": [], "nodes": [], "materials": [],
                     "scenes": [{"nodes": [0]}], "scene": 0}
        self.gltf["nodes"].append({"name": "world", "scale": [1, 1, -1], "children": []})
        self.plain = self.material({"pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 1},
                                    "doubleSided": True})

    def view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin.extend(data)
        self.gltf["bufferViews"].append(v)
        return len(self.gltf["bufferViews"]) - 1

    def accessor(self, arr, kind, ctype, count, target=None, bounds=None, normalized=False):
        a = {"bufferView": self.view(arr.tobytes() if hasattr(arr, "tobytes") else bytes(arr), target),
             "componentType": ctype, "count": count, "type": kind}
        if normalized:
            a["normalized"] = True
        if bounds:
            a["min"], a["max"] = bounds
        self.gltf["accessors"].append(a)
        return len(self.gltf["accessors"]) - 1

    def material(self, m):
        self.gltf["materials"].append(m)
        return len(self.gltf["materials"]) - 1

    def node(self, n):
        self.gltf["nodes"].append(n)
        self.gltf["nodes"][0]["children"].append(len(self.gltf["nodes"]) - 1)

    def write(self, out):
        self.gltf["buffers"][0]["byteLength"] = len(self.bin)
        for key in ("materials", "meshes"):
            if not self.gltf[key]:
                del self.gltf[key]
        js = json.dumps(self.gltf, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        while len(self.bin) % 4:
            self.bin.append(0)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        with open(out, "wb") as fh:
            fh.write(struct.pack("<III", 0x46546C67, 2, total))
            fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            fh.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))
        return out


class _Scene:
    def __init__(self, root=None):
        self.root = root
        self.db = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
        self.objects = {}
        self.meshes = {}
        from .maprender import _Configs
        self.configs = _Configs(root)
        self.morphs = mapwalk._Objects()
        self.materials = {}
        self.glb = _Glb()
        self.glb.gltf["samplers"] = [{"magFilter": 9729, "minFilter": 9987},
                                     {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        self.repeat = 1

    def obj(self, lid):
        if lid not in self.objects:
            row = self.db.execute("select data from config where type='objects' and id=?", (lid,)).fetchone()
            self.objects[lid] = json.loads(row[0]) if row else {}
        return self.objects[lid]

    def material(self, mid):
        """A glTF material wearing this material's texture, or None when it has none to show."""
        if mid in self.materials:
            return self.materials[mid]
        self.materials[mid] = None
        row = self.db.execute("select data from config where type='materials' and id=?", (mid,)).fetchone()
        d = json.loads(row[0]) if row else {}
        # two layouts
        v0, v1 = d.get("v0") or {}, d.get("v1") or {}
        tex = v0.get("diffuse") if v0 else (v1.get("diffuse") or {}).get("texture")
        from . import textures
        got = textures.image(tex, self.root) if tex is not None else None
        if not got:
            return None
        import io
        from PIL import Image
        w, h, px = got
        img = Image.frombytes("RGBA", (w, h), bytes(px))
        if max(w, h) > TEXTURE_MAX:
            img = img.resize((min(w, TEXTURE_MAX), min(h, TEXTURE_MAX)), Image.BILINEAR)
        cutout = img.getextrema()[3][0] < 128
        buf = io.BytesIO()
        img.save(buf, "PNG")
        g = self.glb
        g.gltf.setdefault("images", []).append({"bufferView": g.view(buf.getvalue()), "mimeType": "image/png"})
        g.gltf.setdefault("textures", []).append({"sampler": self.repeat, "source": len(g.gltf["images"]) - 1})
        m = {"pbrMetallicRoughness": {"baseColorTexture": {"index": len(g.gltf["textures"]) - 1},
                                      "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True}
        if cutout:
            # foliage models carry both faces; drawing the backs shows black
            m.update(alphaMode="MASK", alphaCutoff=0.5, doubleSided=False)
        self.materials[mid] = g.material(m)
        return self.materials[mid]

    def mesh(self, mids, recolour, retexture):
        """A glTF mesh of these models joined, recoloured, a primitive per texture; None when none decode.

        A textured face is tinted by twice its vertex colour: Lumbridge keep's floor boards are pale
        wood coloured dark brown, and come out the mid brown the game shows."""
        key = (tuple(mids), recolour, retexture)
        if key in self.meshes:
            return self.meshes[key]
        pos, nrm, uv, col = array.array("f"), array.array("f"), array.array("f"), bytearray()
        groups = {}
        swap, reskin = dict(recolour), dict(retexture)
        for mid in mids:
            m = models.mesh(mid, self.root)
            if not m or not m["verts"]:
                continue
            base = len(pos) // 3
            for v in m["verts"]:
                pos.extend(v)
            for nx, ny, nz in m["normals"]:
                size = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
                nrm.extend((nx / size, ny / size, nz / size))
            for u, v in m["uvs"]:
                uv.extend((u if math.isfinite(u) else 0, v if math.isfinite(v) else 0))
            flat = bytearray(b"".join(bytes(hsl_rgb(swap.get(c, c))) + b"\xff" for c in m["colours"]))
            tinted = set()
            for a, b, c, material in m["tris"]:
                material = reskin.get(material, material)
                use = self.material(material) if material >= 0 else None
                if use is not None:
                    # doubled vertex colour tint
                    for v in (a, b, c):
                        if v not in tinted:
                            tinted.add(v)
                            if v < len(m["colours"]) and m["colours"][v] == UNTINTED:
                                flat[v * 4:v * 4 + 3] = b"\xff\xff\xff"
                            else:
                                flat[v * 4:v * 4 + 3] = bytes(min(255, 2 * ch) for ch in flat[v * 4:v * 4 + 3])
                groups.setdefault(self.glb.plain if use is None else use, array.array("I")).extend(
                    (base + a, base + b, base + c))
            col.extend(flat)
        if not groups:
            self.meshes[key] = None
            return None
        n = len(pos) // 3
        lo = [min(pos[i::3]) for i in range(3)]
        hi = [max(pos[i::3]) for i in range(3)]
        g = self.glb
        attrs = {"POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
                 "NORMAL": g.accessor(nrm, "VEC3", 5126, n, 34962),
                 "TEXCOORD_0": g.accessor(uv, "VEC2", 5126, n, 34962),
                 "COLOR_0": g.accessor(col, "VEC4", 5121, n, 34962, normalized=True)}
        prims = [{"attributes": attrs, "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": mat}
                 for mat, idx in groups.items()]
        g.gltf["meshes"].append({"primitives": prims})
        self.meshes[key] = len(g.gltf["meshes"]) - 1
        return self.meshes[key]


def _heights(sx0, sz0, sx1, sz1, root):
    """{level: {(x, z): units}} over the squares: each level on the one below."""
    out = {p: {} for p in range(4)}
    for sx in range(sx0, sx1 + 1):
        for sz in range(sz0, sz1 + 1):
            got = maptiles.square(sx, sz, root)
            if got is None:
                continue
            tiles = got[0]
            for x in range(64):
                for z in range(64):
                    total = 0
                    for p in range(4):
                        total += tiles[p][x][z].get("height", 0)
                        out[p][(sx * 64 + x, sz * 64 + z)] = total * HEIGHT
    return out


def _grid(g, name, x0, z0, x1, z1, height_at, image, blend=False):
    """A mesh with a vertex on every tile corner, wearing image (north up) over the whole area."""
    import io
    buf = io.BytesIO()
    image.save(buf, "PNG")
    g.gltf.setdefault("images", []).append({"bufferView": g.view(buf.getvalue()), "mimeType": "image/png"})
    g.gltf.setdefault("textures", []).append({"sampler": 0, "source": len(g.gltf["images"]) - 1})
    mat = {"pbrMetallicRoughness": {"baseColorTexture": {"index": len(g.gltf["textures"]) - 1},
                                    "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True}
    if blend:
        mat.update(alphaMode="BLEND")
        mat["pbrMetallicRoughness"]["baseColorFactor"] = [1, 1, 1, WATER_ALPHA]
        mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.3
    w, h = x1 - x0 + 1, z1 - z0 + 1
    pos, uv, idx = array.array("f"), array.array("f"), array.array("I")
    for i in range(w + 1):
        for j in range(h + 1):
            x, z = x0 + i, z0 + j
            pos.extend((x * TILE, height_at(x, z), z * TILE))
            uv.extend((i / w, 1 - j / h))
    for i in range(w):
        for j in range(h):
            a = i * (h + 1) + j
            b, c, d = a + h + 1, a + 1, a + h + 2
            idx.extend((a, b, d, a, d, c))
    n = len(pos) // 3
    lo, hi = [min(pos[k::3]) for k in range(3)], [max(pos[k::3]) for k in range(3)]
    g.gltf["meshes"].append({"name": name, "primitives": [{"attributes": {
        "POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
        "TEXCOORD_0": g.accessor(uv, "VEC2", 5126, n, 34962)},
        "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": g.material(mat)}]})
    g.node({"name": name, "mesh": len(g.gltf["meshes"]) - 1})


def _no_outlines(rgb):
    """The world map with its dark edge lines filled from the pixels beside them."""
    import numpy as np
    out = rgb.astype(np.float32)
    dark = out.max(axis=2) < OUTLINE
    for _ in range(4):
        if not dark.any():
            break
        total = np.zeros_like(out)
        count = np.zeros(dark.shape, np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            lit = np.roll(~dark, (dy, dx), (0, 1))
            total += np.roll(out, (dy, dx), (0, 1)) * lit[..., None]
            count += lit
        fill = dark & (count > 0)
        out[fill] = total[fill] / count[fill][:, None]
        dark &= ~fill
    return out.astype(np.uint8)


def _far(g, sc, x0, z0, x1, z1, root):
    """Land past the scene's edge to FAR tiles: heights every FAR_STEP tiles, coloured from the
    game's own world map, sunk a little so the detailed ground covers the seam."""
    import numpy as np
    from PIL import Image
    fx0, fz0 = (x0 - FAR) // FAR_STEP * FAR_STEP, (z0 - FAR) // FAR_STEP * FAR_STEP
    fx1, fz1 = (x1 + FAR) // FAR_STEP * FAR_STEP, (z1 + FAR) // FAR_STEP * FAR_STEP
    heights = _heights(fx0 // 64, fz0 // 64, fx1 // 64, fz1 // 64, root)[0]
    cfg = sc.configs.world_map()
    bound = next((b for b in cfg.bounds if b["fromX"] <= x0 <= b["toX"] and b["fromZ"] <= z0 <= b["toZ"]), None)
    if bound is None:
        return
    ox = bound["mapFromX"] - bound["fromX"] - cfg.origin[0]
    oz = bound["mapFromZ"] - bound["fromZ"] - cfg.origin[1]
    surf = cfg.surface
    rows = np.clip(np.arange(fz1, fz0 - 1, -1) + oz, 0, surf.shape[0] - 1)
    cols = np.clip(np.arange(fx0, fx1 + 1) + ox, 0, surf.shape[1] - 1)
    image = Image.fromarray(_no_outlines(surf[rows][:, cols]))
    nx, nz = (fx1 - fx0) // FAR_STEP, (fz1 - fz0) // FAR_STEP
    pos, uv, idx = array.array("f"), array.array("f"), array.array("I")
    for i in range(nx + 1):
        for j in range(nz + 1):
            x, z = fx0 + i * FAR_STEP, fz0 + j * FAR_STEP
            pos.extend((x * TILE, heights.get((x, z), 0) - FAR_SINK, z * TILE))
            uv.extend((i / nx, 1 - j / nz))
    for i in range(nx):
        for j in range(nz):
            cx0, cz0 = fx0 + i * FAR_STEP, fz0 + j * FAR_STEP
            # the detailed ground is here
            if x0 <= cx0 and cx0 + FAR_STEP <= x1 + 1 and z0 <= cz0 and cz0 + FAR_STEP <= z1 + 1:
                continue
            a = i * (nz + 1) + j
            b, c, d = a + nz + 1, a + 1, a + nz + 2
            idx.extend((a, b, d, a, d, c))
    import io
    buf = io.BytesIO()
    image.save(buf, "PNG")
    g.gltf["images"].append({"bufferView": g.view(buf.getvalue()), "mimeType": "image/png"})
    g.gltf["textures"].append({"sampler": 0, "source": len(g.gltf["images"]) - 1})
    mat = g.material({"pbrMetallicRoughness": {"baseColorTexture": {"index": len(g.gltf["textures"]) - 1},
                                               "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True})
    n = len(pos) // 3
    lo, hi = [min(pos[k::3]) for k in range(3)], [max(pos[k::3]) for k in range(3)]
    g.gltf["meshes"].append({"name": "far", "primitives": [{"attributes": {
        "POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
        "TEXCOORD_0": g.accessor(uv, "VEC2", 5126, n, 34962)},
        "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": mat}]})
    g.node({"name": "far", "mesh": len(g.gltf["meshes"]) - 1})


def build(cx, cz, out, level=0, radius=32, root=None, var=None):
    """The scene of the tiles within radius of cx, cz, written to out; var(kind, id) picks morphs."""
    x0, z0, x1, z1 = cx - radius, cz - radius, cx + radius, cz + radius
    sx0, sz0, sx1, sz1 = x0 // 64, z0 // 64, x1 // 64, z1 // 64
    heights = _heights(sx0, sz0, sx1 + 1, sz1 + 1, root)
    ground = heights[0]
    sc = _Scene(root)
    g = sc.glb

    # the locs
    placed = skipped = 0
    for sx in range(sx0, sx1 + 1):
        for sz in range(sz0, sz1 + 1):
            files = cachefile.files(maplocs.MAJOR, sx + sz * maplocs.STRIDE, root or cachefile.GAME,
                                    cachefile.descriptor(maplocs.MAJOR, root or cachefile.GAME)) or {}
            got = maptiles.square(sx, sz, root)
            if maplocs.FILE not in files or got is None:
                continue
            tiles = got[0]
            for lid, uses in maplocs.decode(files[maplocs.FILE]):
                sid = mapnear.shown(sc.morphs, lid, var or (lambda kind, vid: 0))
                if sid is None:
                    continue
                o = sc.obj(sid)
                for p, lx, lz, shape, rot, extra in uses:
                    x, z = sx * 64 + lx, sz * 64 + lz
                    if not (x0 <= x <= x1 and z0 <= z <= z1) or shape in ROOFS:
                        continue
                    bridged = p > 0 and tiles[1][lx][lz].get("settings", 0) & maptiles.BRIDGE
                    if p - (1 if bridged else 0) > level:
                        continue
                    if _place(sc, o, p, x, z, shape, rot, extra or {}, heights):
                        placed += 1
                    else:
                        skipped += 1

    # ground, then water
    from . import mapground
    floors = mapground.Floors(root)
    _grid(g, "ground", x0, z0, x1, z1, lambda x, z: ground.get((x, z), 0),
          mapground.texture(x0, z0, x1, z1, root, floors))
    wet = mapground.water(x0, z0, x1, z1, HEIGHT, root, floors)
    if wet:
        _grid(g, "water", x0, z0, x1, z1, lambda x, z: wet["corners"].get((x, z), wet["fill"]), wet["image"],
              blend=True)
    if level == 0:
        _far(g, sc, x0, z0, x1, z1, root)
    g.write(out)
    return {"out": out, "tiles": [x0, z0, x1, z1], "level": level, "locs": placed, "noModel": skipped,
            "models": sum(1 for v in sc.meshes.values() if v is not None), "bytes": os.path.getsize(out),
            "heightUnits": HEIGHT}


def _place(sc, o, p, x, z, shape, rot, extra, heights):
    entries = o.get("models") or []
    # diagonal objects and offset decorations borrow a model
    want = {11: 10, 5: 4, 6: 4, 7: 4, 8: 4}.get(shape, shape)
    mids = [m for e in entries if e.get("shape", 10) == want for m in e["models"]]
    if not mids:
        return False
    recolour = tuple((r["from"], r["to"]) for r in o.get("recolour") or [])
    retexture = tuple((r["from"], r["to"]) for r in o.get("retexture") or [])
    mesh = sc.mesh(mids, recolour, retexture)
    if mesh is None:
        return False
    # footprint, walls included
    w, l = (o.get("width", 1), o.get("length", 1)) if rot in (0, 2) else (o.get("length", 1), o.get("width", 1))
    cx, cz = (x + w / 2) * TILE, (z + l / 2) * TILE
    level = heights[p]
    # ground under the footprint's centre
    corners = [level.get((x + dx, z + dz), 0) for dx in (0, w) for dz in (0, l)]
    y = sum(corners) / 4
    sx = o.get("scaleX", 128) / 128 * (extra.get("scaleX", extra.get("scale", 128)) / 128)
    sy = o.get("scaleY", 128) / 128 * (extra.get("scaleY", extra.get("scale", 128)) / 128)
    sz = o.get("scaleZ", 128) / 128 * (extra.get("scaleZ", extra.get("scale", 128)) / 128)
    ox = o.get("offsetX", 0) + extra.get("translateX", 0)
    # offsets point y down
    oy = -(o.get("offsetY", 0) + extra.get("translateY", 0))
    oz = o.get("offsetZ", 0) + extra.get("translateZ", 0)
    mirror = bool(o.get("mirror"))
    # an L corner is its model twice
    if shape == 2:
        pieces = [(rot, not mirror), ((rot + 1) & 3, mirror)]
    else:
        pieces = [(rot + (0.5 if shape in (6, 7, 8, 11) else 0), mirror)]
    for turns, flip in pieces:
        c, s = math.cos(turns * math.pi / 2), math.sin(turns * math.pi / 2)
        fz = -sz if flip else sz
        # turn, scale, place; column-major
        m = [c * sx, 0, -s * sx, 0,
             0, sy, 0, 0,
             s * fz, 0, c * fz, 0,
             cx + ox, y + oy, cz + oz, 1]
        sc.glb.node({"mesh": mesh, "matrix": m})
    return True


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 3:
        raise SystemExit(__doc__)
    print(json.dumps(build(int(a[0]), int(a[1]), a[2], int(a[3]) if len(a) > 3 else 0,
                           int(a[4]) if len(a) > 4 else 32)))
