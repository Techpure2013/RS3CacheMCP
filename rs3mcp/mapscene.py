"""A 3D scene of the map around a tile, as a binary glTF (.glb) any viewer opens.

    python -m rs3mcp.mapscene <x> <z> <out.glb> [level] [radius]

The ground is one mesh wearing its floors' materials (mapground), and each floor above it up to
level one more, clear where that plane has no floor. Each loc is a node on its object's models,
stored once however often used: turned, mirrored, scaled, set on the ground and moved by offsets
whose y points down. A morphing loc shows what its var picks (every var 0 unless given). An L
corner draws its model twice, mirrored at its rotation and plain a quarter turn on. Roofs are left
out, as indoors. What a tile or loc shows on is its visual floor (_visual): what stands over a
roofed tile belongs to the floor above that roof, so an open tower top shows from the ground and a
room under a roof only from its own floor. Anything over level is left out, and a node over the
ground carries its visual floor as extras.level, so a viewer can show one scene a floor at a time.

A tile is TILE wide and y is up; each level's height adds to the one below. HEIGHT, the units per
stored step, is measured: a live worldscan's loc heights fit it with a median miss of 4 units.
Built in world axes (x east, z north) under a root that flips z.
"""

import array
import functools
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile

from . import cachefile, common, mapground, maplocs, mapnear, maptiles, mapwalk, models

TILE = 512
# half width, wheat's
GROUND_SQUARE = 192
HEIGHT = 32
ROOFS = range(12, 22)
# config offset units: the Taverley mine's pieces meet only at 4
OFFSET = 4
# texture edge, pixels
TEXTURE_MAX = 256
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


def _srgb_to_linear():
    """sRGB byte -> linear, as 16 bit. glTF vertex colours are linear, the game's are sRGB; written
    as they are, every colour and tint showed paler and greyer than authored."""
    import numpy as np
    v = np.arange(256) / 255
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return np.round(lin * 65535).astype(np.uint16)


_LINEAR = _srgb_to_linear()


@functools.lru_cache(maxsize=None)
def _rgba(c):
    """hsl_rgb as the 4 bytes of a vertex colour."""
    return bytes(hsl_rgb(c)) + b"\xff"


def _vertex_colours(colours, swap):
    """(n, 4) uint8 rgba of hsl colours after recolouring, a fresh array to write into."""
    import numpy as np
    uniq, inv = np.unique(colours, return_inverse=True)
    table = np.frombuffer(b"".join(_rgba(swap.get(c, c)) for c in uniq.tolist()), np.uint8).reshape(-1, 4)
    return table[inv.reshape(-1)].copy()


def _model_arrays(mid, root):
    """models.mesh as arrays, normals unit length and float32 like the glTF takes them; None when
    the model has no vertices."""
    def make():
        import numpy as np
        m = models.mesh(mid, root)
        if not m or not m["verts"]:
            return None
        normals = np.asarray(m["normals"], np.float64)
        size = np.sqrt(normals[:, 0] * normals[:, 0] + normals[:, 1] * normals[:, 1] + normals[:, 2] * normals[:, 2])
        size[size == 0] = 1
        uvs = np.asarray(m["uvs"], np.float64)
        return {"pos": np.asarray(m["verts"], np.float64).astype(np.float32),
                "nrm": (normals / size[:, None]).astype(np.float32),
                "uv": np.where(np.isfinite(uvs), uvs, 0).astype(np.float32),
                "colours": np.asarray(m["colours"], np.int64),
                "alphas": np.asarray(m["alphas"], np.int64) if m.get("alphas") else None,
                "tris": np.asarray(m["tris"], np.int64).reshape(-1, 4)}
    return cachefile.kept("model-arrays", (mid, root), make)


def _ground_square(m):
    """Which faces are a plain flat square centred on the model's base; None if it has none."""
    import numpy as np
    tris, pos = m["tris"], m["pos"]
    flat = (tris[:, 3] < 0) & np.all(pos[tris[:, :3], 1] == 0, axis=1)
    if flat.sum() != 2:
        return None
    corners = pos[np.unique(tris[flat][:, :3])]
    low, high = corners.min(axis=0), corners.max(axis=0)
    if len(corners) != 4 or high[0] < GROUND_SQUARE or high[0] != high[2] or low[0] != -high[0] or low[2] != -high[2]:
        return None
    return flat


class _Glb:
    """Just enough glTF: accessors over one buffer, meshes, nodes, one texture."""

    def __init__(self):
        self.bin = bytearray()
        self.gltf = {"asset": {"version": "2.0", "generator": "rs3mcp.mapscene"}, "buffers": [{}],
                     "bufferViews": [], "accessors": [], "meshes": [], "nodes": [], "materials": [],
                     "scenes": [{"nodes": [0]}], "scene": 0}
        self.gltf["nodes"].append({"name": "world", "scale": [1, 1, -1], "children": []})
        # images, PNG once written
        self.pictures = []
        self.plain = self.material({"pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 1},
                                    "doubleSided": True})

    def texture(self, img, sampler):
        """A texture wearing img; returns its index."""
        self.gltf.setdefault("images", []).append({"mimeType": "image/png"})
        self.pictures.append(img)
        self.gltf.setdefault("textures", []).append({"sampler": sampler, "source": len(self.pictures) - 1})
        return len(self.gltf["textures"]) - 1

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
        import io
        for picture, image in zip(self.pictures, self.gltf.get("images", [])):
            buf = io.BytesIO()
            picture.save(buf, "PNG")
            image["bufferView"] = self.view(buf.getvalue())
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
        # shared while frozen
        self.objects = cachefile.kept("scene-objects", root, dict)
        self.meshes = {}
        from .maprender import _Configs
        self.configs = cachefile.kept("configs", root, lambda: _Configs(root))
        self.morphs = cachefile.kept("morphs", root, mapwalk._Objects)
        self.materials = {}
        self.tinted = {}
        # tinted material -> its texture's average
        self.bases = {}
        # mesh -> furthest vertex from its origin, units
        self.reach = {}
        # mesh -> vertex positions
        self.positions = {}
        # mesh keys whose every face is see-through
        self.unseen = set()
        # material -> its see-through twin
        self.blends = {}
        # models -> one untextured square
        self.helpers = {}
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
        row = cachefile.kept("material-row", mid, lambda: self.db.execute(
            "select data from config where type='materials' and id=?", (mid,)).fetchone())
        d = json.loads(row[0]) if row else {}
        # two layouts
        v0, v1 = d.get("v0") or {}, d.get("v1") or {}
        # material -> vertex colour weight
        share = (v0.get("extra") or {}).get("baseColourShare", 255) if v0 else 255
        if share < 255:
            self.tinted[mid] = 1 - share / 255
        tex = v0.get("diffuse") if v0 else (v1.get("diffuse") or {}).get("texture")
        from . import textures
        got = textures.image(tex, self.root) if tex is not None else None
        if not got:
            return None
        from PIL import Image
        w, h, px = got
        img = Image.frombytes("RGBA", (w, h), bytes(px))
        if mid in self.tinted:
            from .mapground import base
            self.bases[mid] = base(v0.get("extra") if v0 else None, img)
        # alpha use: v0's mode, else the texture
        mode = v0.get("alphaMode", 0) if v0 else None
        cutout = mode == 1 or (mode is None and img.getextrema()[3][0] < 128)
        if not cutout and mode != 2:
            # opaque: drop alpha before anything scales by it
            img = img.convert("RGB")
        if max(w, h) > TEXTURE_MAX:
            img = img.resize((min(w, TEXTURE_MAX), min(h, TEXTURE_MAX)), Image.BILINEAR)
        g = self.glb
        m = {"pbrMetallicRoughness": {"baseColorTexture": {"index": g.texture(img, self.repeat)},
                                      "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True}
        if cutout:
            # foliage models carry both faces; drawing the backs shows black
            # 0.1 keeps bushes full; 0.5 thinned them to sticks
            m.update(alphaMode="MASK", alphaCutoff=(v0.get("alphaCutoff") or 25.5) / 255 if v0 else 0.1,
                     doubleSided=False)
        elif mode == 2:
            m.update(alphaMode="BLEND")
        self.materials[mid] = g.material(m)
        return self.materials[mid]

    def _blended(self, mat):
        """The see-through twin of a glTF material, for faces whose vertex colour is partly clear."""
        if mat not in self.blends:
            m = dict(self.glb.gltf["materials"][mat], alphaMode="BLEND")
            m.pop("alphaCutoff", None)
            self.blends[mat] = self.glb.material(m)
        return self.blends[mat]

    def helper(self, mids):
        """True when these models are one untextured square: the shape of a floor-raise helper."""
        key = tuple(mids)
        if key not in self.helpers:
            verts, plain = 0, True
            for mid in mids:
                m = models.mesh(mid, self.root) or {"verts": [], "tris": []}
                verts += len(m["verts"])
                plain = plain and all(t[3] < 0 for t in m["tris"])
            self.helpers[key] = plain and 0 < verts <= 4
        return self.helpers[key]

    def mesh(self, mids, recolour, retexture):
        """A glTF mesh of these models joined, recoloured, a primitive per texture; None when none decode.

        A textured face shows its texture alone unless its material keeps less than all its base
        colour (a v0 material with baseColourShare under 255); those take the vertex colour as floors
        do (mapground.tint). Vertex colour 0xFF80 is hsl black: a white colour-only material wearing
        it is a chimney's dark mouth. Tinting the rest turns plants black and invents rugs. A face
        whose vertex colour is fully transparent (alpha 0) is left out."""
        key = (tuple(mids), recolour, retexture)
        if key in self.meshes:
            return self.meshes[key]
        import numpy as np
        pos, nrm, uv, col = [], [], [], []
        groups, cut, base = {}, 0, 0
        swap, reskin = dict(recolour), dict(retexture)
        squares = {}
        for mid in mids:
            m = _model_arrays(mid, self.root)
            if m is not None:
                squares[mid] = _ground_square(m)
        # a loc that is only squares keeps them
        solid = any(s is None or not s.all() for s in squares.values())
        for mid in mids:
            m = _model_arrays(mid, self.root)
            if m is None:
                continue
            tris = m["tris"]
            if solid and squares[mid] is not None:
                tris = tris[~squares[mid]]
                if not len(tris):
                    continue
            a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
            mats = tris[:, 3].copy()
            for was, now in reskin.items():
                mats[tris[:, 3] == was] = now
            flat = _vertex_colours(m["colours"], swap)
            seen = m["alphas"]
            if seen is not None and len(seen) == len(m["colours"]):
                flat[:, 3] = seen
            if seen is not None:
                # vertex coloured and fully see-through, or partly
                most = np.maximum(np.maximum(seen[a], seen[b]), seen[c])
                least = np.minimum(np.minimum(seen[a], seen[b]), seen[c])
                clear, part = most == 0, least < 255
            else:
                clear = part = np.zeros(len(tris), bool)
            # faces that make a material
            visit = set()
            for mat in np.unique(mats).tolist():
                own = mats == mat
                for pick in (own, own & part & ~clear, own & part):
                    hit = np.flatnonzero(pick)
                    if len(hit):
                        visit.add(int(hit[0]))
            for t in sorted(visit):
                mat = int(mats[t])
                use = self.material(mat) if mat >= 0 else None
                if clear[t] and (use is None or mat in self.tinted):
                    continue
                if part[t]:
                    self._blended(self.glb.plain if use is None else use)
            uniq, inv = np.unique(mats, return_inverse=True)
            used = [self.materials.get(x) if x >= 0 else None for x in uniq.tolist()]
            has = np.array([u is not None for u in used], bool)[inv]
            cutting = np.array([u is None or x in self.tinted for x, u in zip(uniq.tolist(), used)], bool)[inv]
            gone = clear & cutting if seen is not None else np.zeros(len(tris), bool)
            cut += int(gone.sum())
            keep = ~gone
            # first textured face tints
            textured = keep & has
            weight = np.array([self.tinted.get(x, 0) for x in uniq.tolist()], np.float64)[inv]
            # mapground.tint's divisor
            avg = np.array([self.bases.get(x, (0.5, 0.5, 0.5)) for x in uniq.tolist()], np.float64).reshape(-1, 3)[inv]
            order = tris[textured][:, :3].ravel()
            if len(order):
                vs, first = np.unique(order, return_index=True)
                u = np.repeat(weight[textured], 3)[first][:, None] * mapground.STRENGTH
                b = np.repeat(avg[textured], 3, axis=0)[first]
                ch = flat[vs, :3] / 255
                flat[vs, :3] = np.round(255 * (1 - u + u * np.minimum(1.0, ch / np.maximum(b, 1 / 255)))).astype(np.uint8)
            group = np.array([self.glb.plain if u is None else u for u in used], np.int64)[inv]
            blended = keep & part
            if blended.any():
                group[blended] = [self.blends[int(x)] for x in group[blended].tolist()]
            kept_group, kept_tris = group[keep], tris[keep]
            ids, first = np.unique(kept_group, return_index=True)
            for gid in ids[np.argsort(first, kind="stable")].tolist():
                groups.setdefault(gid, []).append(base + kept_tris[kept_group == gid][:, :3].ravel())
            pos.append(m["pos"])
            nrm.append(m["nrm"])
            uv.append(m["uv"])
            col.append(flat)
            base += len(m["pos"])
        if not groups:
            self.meshes[key] = None
            if cut:
                # all of it see-through, not missing
                self.unseen.add(key)
            return None
        pos = np.concatenate(pos)
        n = len(pos)
        lo = [float(v) for v in pos.min(axis=0)]
        hi = [float(v) for v in pos.max(axis=0)]
        g = self.glb
        rgba = np.concatenate(col).ravel()
        colour = _LINEAR[rgba]
        # alpha stays linear
        colour[3::4] = rgba[3::4].astype(np.uint16) * 257
        attrs = {"POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
                 "NORMAL": g.accessor(np.concatenate(nrm), "VEC3", 5126, n, 34962),
                 "TEXCOORD_0": g.accessor(np.concatenate(uv), "VEC2", 5126, n, 34962),
                 "COLOR_0": g.accessor(colour, "VEC4", 5123, n, 34962, normalized=True)}
        prims = []
        for mat, parts in groups.items():
            idx = np.concatenate(parts).astype(np.uint32)
            prims.append({"attributes": attrs, "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": mat})
        g.gltf["meshes"].append({"primitives": prims})
        self.meshes[key] = len(g.gltf["meshes"]) - 1
        self.reach[self.meshes[key]] = max(abs(lo[0]), abs(hi[0]), abs(lo[2]), abs(hi[2]))
        self.positions[self.meshes[key]] = pos
        return self.meshes[key]

    def lay(self, heights, x0, z0, w, h):
        """Keeps each level's heights as a grid from tile x0, z0, for models that follow the ground."""
        import numpy as np
        self.hx, self.hz = x0, z0
        self.hgrid = {}
        for p, level in heights.items():
            a = np.zeros((w, h), np.float32)
            for (x, z), v in level.items():
                a[x - x0, z - z0] = v
            self.hgrid[p] = a

    def ground_at(self, p, gx, gz):
        """Level p's height at fractional tile positions, between its tile corners."""
        import numpy as np
        a = self.hgrid[min(p, 3)]
        x = np.clip(gx - self.hx, 0, a.shape[0] - 1.001)
        z = np.clip(gz - self.hz, 0, a.shape[1] - 1.001)
        ix, iz = x.astype(int), z.astype(int)
        fx, fz = x - ix, z - iz
        return (a[ix, iz] * (1 - fx) * (1 - fz) + a[ix + 1, iz] * fx * (1 - fz)
                + a[ix, iz + 1] * (1 - fx) * fz + a[ix + 1, iz + 1] * fx * fz)

    def follow(self, mesh, m, p, ground):
        """A copy of mesh whose heights follow level p's ground where matrix m puts it, set at ground:
        each vertex rises or falls by the ground under it less the ground at the loc. Only y moves,
        in the model's own space, so m still applies."""
        import numpy as np
        v = np.frombuffer(self.positions[mesh], np.float32).reshape(-1, 3).astype(np.float64)
        mat = np.array(m, np.float64).reshape(4, 4).T
        world = v @ mat[:3, :3].T + mat[:3, 3]
        lift = self.ground_at(p, world[:, 0] / TILE, world[:, 2] / TILE) - ground
        # scaled flat: nothing to bend
        if abs(np.linalg.det(mat[:3, :3])) < 1e-9:
            return mesh
        # world lift into model space
        new = (v + lift[:, None] * np.linalg.inv(mat[:3, :3])[:, 1][None, :]).astype(np.float32)
        g = self.glb
        pos = g.accessor(new, "VEC3", 5126, len(new), 34962, (new.min(axis=0).tolist(), new.max(axis=0).tolist()))
        prims = [dict(pr, attributes=dict(pr["attributes"], POSITION=pos)) for pr in g.gltf["meshes"][mesh]["primitives"]]
        g.gltf["meshes"].append({"primitives": prims})
        return len(g.gltf["meshes"]) - 1


def _heights(sx0, sz0, sx1, sz1, root, surface=False):
    """{level: {(x, z): units}} over the squares: each level on the one below. A water tile's floor
    is its bed, sunk under the column's water surface; with surface, the surface instead."""
    import numpy as np
    out = {p: {} for p in range(4)}
    for sx in range(sx0, sx1 + 1):
        for sz in range(sz0, sz1 + 1):
            gr = maptiles.grid(sx, sz, root)
            if gr is not None:
                # each level on the one below
                units = np.cumsum(gr["height"], axis=0)
                if not surface:
                    units = units - gr["sink"]
                for p, values in enumerate((units * HEIGHT).reshape(4, -1).tolist()):
                    out[p].update(zip(_square_keys(sx, sz), values))
                continue
            got = maptiles.ground(sx, sz, root)
            if got is None:
                continue
            tiles = got[0]
            for x in range(64):
                for z in range(64):
                    total = 0
                    for p in range(4):
                        t = tiles[p][x][z]
                        total += t.get("height", 0)
                        out[p][(sx * 64 + x, sz * 64 + z)] = (total - (0 if surface else t.get("sink", 0))) * HEIGHT
    return out


@functools.lru_cache(maxsize=256)
def _square_keys(sx, sz):
    """(x, z) of a square's tiles, x outer and z inner."""
    return tuple((sx * 64 + x, sz * 64 + z) for x in range(64) for z in range(64))


def _visual(sx0, sz0, sx1, sz1, root):
    """(x0, z0, [plane][x - x0][z - z0] -> the lowest floor a viewer sees that tile from), over the
    squares. A roof flag (settings 4) marks a tile as under a roof, so what stands above it in the
    column belongs to the floor over that roof; a bridge column (settings 2 on plane 1) counts its
    planes one lower; a tile flagged to draw below (settings 8, tree canopies) shows from the ground."""
    import numpy as np
    x0, z0 = sx0 * 64, sz0 * 64
    w, h = (sx1 - sx0 + 1) * 64, (sz1 - sz0 + 1) * 64
    floors = np.zeros((4, w, h), np.int8)
    for sx in range(sx0, sx1 + 1):
        for sz in range(sz0, sz1 + 1):
            gr = maptiles.grid(sx, sz, root)
            if gr is not None:
                s = gr["settings"]
                shift = (s[1] & maptiles.BRIDGE != 0).astype(np.int64)
                for p in range(1, 4):
                    seen = np.zeros((64, 64), np.int64)
                    for r in range(p):
                        under = (s[r] & maptiles.ROOF != 0) & (r - shift >= 0)
                        seen = np.where(under, np.maximum(seen, r - shift + 1), seen)
                    seen[s[p] & maptiles.BELOW != 0] = 0
                    floors[p, sx * 64 - x0:sx * 64 - x0 + 64, sz * 64 - z0:sz * 64 - z0 + 64] = seen
                continue
            got = maptiles.ground(sx, sz, root)
            if got is None:
                continue
            planes = got[0]
            for x in range(64):
                for z in range(64):
                    column = [planes[p][x][z].get("settings", 0) for p in range(4)]
                    shift = 1 if column[1] & maptiles.BRIDGE else 0
                    roofed = [r for r in range(4) if column[r] & maptiles.ROOF]
                    for p in range(1, 4):
                        if column[p] & maptiles.BELOW:
                            continue
                        under = [r - shift + 1 for r in roofed if r < p and r - shift >= 0]
                        floors[p, sx * 64 + x - x0, sz * 64 + z - z0] = max(under, default=0)
    return x0, z0, floors


def _grid(g, name, x0, z0, x1, z1, height_at, image, blend=False, cutout=False, level=0):
    """A mesh with a vertex on every tile corner, wearing image (north up) over the whole area;
    with cutout, left out where the image is clear."""
    mat = {"pbrMetallicRoughness": {"baseColorTexture": {"index": g.texture(image, 0)},
                                    "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True}
    if blend:
        mat.update(alphaMode="BLEND")
        mat["pbrMetallicRoughness"]["baseColorFactor"] = [1, 1, 1, WATER_ALPHA]
        mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.3
    if cutout:
        mat.update(alphaMode="MASK", alphaCutoff=0.5)
    w, h = x1 - x0 + 1, z1 - z0 + 1
    pos, nrm, uv, idx = array.array("f"), array.array("f"), array.array("f"), array.array("I")
    y = {(x, z): height_at(x, z) for x in range(x0 - 1, x1 + 3) for z in range(z0 - 1, z1 + 3)}
    for i in range(w + 1):
        for j in range(h + 1):
            x, z = x0 + i, z0 + j
            pos.extend((x * TILE, y[(x, z)], z * TILE))
            # smooth normals, not one per face
            dx = (y[(x + 1, z)] - y[(x - 1, z)]) / (2 * TILE)
            dz = (y[(x, z + 1)] - y[(x, z - 1)]) / (2 * TILE)
            size = math.sqrt(dx * dx + 1 + dz * dz)
            nrm.extend((-dx / size, 1 / size, -dz / size))
            uv.extend((i / w, 1 - j / h))
    for i in range(w):
        for j in range(h):
            a = i * (h + 1) + j
            b, c, d = a + h + 1, a + 1, a + h + 2
            # faces up once the root mirrors z
            idx.extend((a, d, b, a, c, d))
    n = len(pos) // 3
    lo, hi = [min(pos[k::3]) for k in range(3)], [max(pos[k::3]) for k in range(3)]
    g.gltf["meshes"].append({"name": name, "primitives": [{"attributes": {
        "POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
        "NORMAL": g.accessor(nrm, "VEC3", 5126, n, 34962),
        "TEXCOORD_0": g.accessor(uv, "VEC2", 5126, n, 34962)},
        "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": g.material(mat)}]})
    node = {"name": name, "mesh": len(g.gltf["meshes"]) - 1}
    if level:
        node["extras"] = {"level": level}
    g.node(node)


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
    mat = g.material({"pbrMetallicRoughness": {"baseColorTexture": {"index": g.texture(image, 0)},
                                               "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True})
    n = len(pos) // 3
    lo, hi = [min(pos[k::3]) for k in range(3)], [max(pos[k::3]) for k in range(3)]
    g.gltf["meshes"].append({"name": "far", "primitives": [{"attributes": {
        "POSITION": g.accessor(pos, "VEC3", 5126, n, 34962, (lo, hi)),
        "TEXCOORD_0": g.accessor(uv, "VEC2", 5126, n, 34962)},
        "indices": g.accessor(idx, "SCALAR", 5125, len(idx), 34963), "material": mat}]})
    g.node({"name": "far", "mesh": len(g.gltf["meshes"]) - 1})


def build(cx, cz, out, level=0, radius=32, root=None, var=None, far=True):
    """The scene of the tiles within radius of cx, cz, written to out; var(kind, id) picks morphs.
    far adds the low land past the edge, for a horizon. With out None the scene comes back unwritten
    (a _Glb), for a renderer in this process."""
    x0, z0, x1, z1 = cx - radius, cz - radius, cx + radius, cz + radius
    sx0, sz0, sx1, sz1 = x0 // 64, z0 // 64, x1 // 64, z1 // 64
    heights = _heights(sx0, sz0, sx1 + 1, sz1 + 1, root)
    ground = heights[0]
    # locs stand on the water surface
    tops = _heights(sx0, sz0, sx1 + 1, sz1 + 1, root, surface=True)
    sc = _Scene(root)
    sc.lay(tops, sx0 * 64, sz0 * 64, (sx1 + 2 - sx0) * 64, (sz1 + 2 - sz0) * 64)
    vx, vz, seen = _visual(sx0, sz0, sx1, sz1, root)
    g = sc.glb

    # the locs
    placed = skipped = 0
    for sx in range(sx0, sx1 + 1):
        for sz in range(sz0, sz1 + 1):
            files = cachefile.files(maplocs.MAJOR, sx + sz * maplocs.STRIDE, root or cachefile.GAME,
                                    cachefile.descriptor(maplocs.MAJOR, root or cachefile.GAME)) or {}
            if maplocs.FILE not in files or maptiles.NXT not in files:
                continue
            for lid, uses in maplocs.decode(files[maplocs.FILE]):
                sid = mapnear.shown(sc.morphs, lid, var or (lambda kind, vid: 0))
                if sid is None:
                    continue
                o = sc.obj(sid)
                parent = sc.obj(lid)
                if sid != lid:
                    # a morph stands on the placed loc's footprint
                    o = dict(o, width=parent.get("width", 1), length=parent.get("length", 1))
                for p, lx, lz, shape, rot, extra in uses:
                    x, z = sx * 64 + lx, sz * 64 + lz
                    if not (x0 <= x <= x1 and z0 <= z <= z1) or shape in ROOFS:
                        continue
                    # highest visual floor around its footprint: a wall stands on a tile edge
                    fw, fl = (o.get("width", 1), o.get("length", 1))[::1 if rot % 2 == 0 else -1]
                    under = seen[p, max(x - vx - 1, 0):min(x - vx + fw + 1, seen.shape[1]),
                                 max(z - vz - 1, 0):min(z - vz + fl + 1, seen.shape[2])]
                    shown_on = int(under.max()) if under.size else 0
                    if shown_on > level:
                        continue
                    if _place(sc, o, p, x, z, shape, rot, extra or {}, tops, shown_on):
                        placed += 1
                    else:
                        skipped += 1

    # ground, then water at its own height
    from . import mapground
    floors = cachefile.kept("floors", root, lambda: mapground.Floors(root))
    # areas laid for another scene
    floors.laid.clear()
    image, water = mapground.texture(x0, z0, x1, z1, root, floors, with_water=True)
    _grid(g, "ground", x0, z0, x1, z1, lambda x, z: ground.get((x, z), 0), image, cutout=True)
    if water is not None:
        _grid(g, "water", x0, z0, x1, z1, lambda x, z: tops[0].get((x, z), 0), water, cutout=True)
    # upper floors, a mesh per visual floor
    import numpy as np
    ii = np.arange(x1 - x0 + 1) + x0 - vx
    jj = z1 - np.arange(z1 - z0 + 1) - vz
    inside = ((jj >= 0) & (jj < seen.shape[2]))[:, None] & ((ii >= 0) & (ii < seen.shape[1]))[None, :]
    rows, cols = np.nonzero(inside)
    for p in range(1, 4):
        shown = np.full(inside.shape, p, np.int64)
        shown[rows, cols] = seen[p, ii[cols], jj[rows]]
        for v in range(0, min(p, level) + 1):
            image = mapground.floor(x0, z0, x1, z1, p, root, floors, shown == v)
            if image is not None:
                _grid(g, "floor%d_%d" % (p, v), x0, z0, x1, z1, lambda x, z, p=p: heights[p].get((x, z), 0), image,
                      cutout=True, level=v)
    if far:
        _far(g, sc, x0, z0, x1, z1, root)
    if out is None:
        return g
    g.write(out)
    return {"out": out, "tiles": [x0, z0, x1, z1], "level": level, "locs": placed, "noModel": skipped,
            "models": sum(1 for v in sc.meshes.values() if v is not None), "bytes": os.path.getsize(out),
            "heightUnits": HEIGHT}


def _qmul(a, b):
    """Quaternion product a * b, each (x, y, z, w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def _rotation(q):
    """A unit quaternion (x, y, z, w) as a 3 x 3 rotation, rows."""
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def _between(level, x, z):
    """A level's ground height at a point, between the corners of the tile it falls in."""
    ix, iz = math.floor(x), math.floor(z)
    fx, fz = x - ix, z - iz
    h = lambda dx, dz: level.get((ix + dx, iz + dz), 0)
    return (h(0, 0) * (1 - fx) * (1 - fz) + h(1, 0) * fx * (1 - fz)
            + h(0, 1) * (1 - fx) * fz + h(1, 1) * fx * fz)


def _place(sc, o, p, x, z, shape, rot, extra, heights, shown_on=0):
    entries = o.get("models") or []
    # diagonal objects and offset decorations borrow a model
    want = {11: 10, 5: 4, 6: 4, 7: 4, 8: 4}.get(shape, shape)
    mids = [m for e in entries if e.get("shape", 10) == want for m in e["models"]]
    if not mids:
        return False
    # floor-raise helper, never drawn
    if shape == 22 and o.get("groundDecorOffset") and sc.helper(mids):
        return True
    recolour = tuple((r["from"], r["to"]) for r in o.get("recolour") or [])
    retexture = tuple((r["from"], r["to"]) for r in o.get("retexture") or [])
    mesh = sc.mesh(mids, recolour, retexture)
    if mesh is None:
        # placed, with nothing to show
        return (tuple(mids), recolour, retexture) in getattr(sc, "unseen", ())
    # footprint, walls included
    w, l = (o.get("width", 1), o.get("length", 1)) if rot in (0, 2) else (o.get("length", 1), o.get("width", 1))
    cx, cz = (x + w / 2) * TILE, (z + l / 2) * TILE
    level = heights[p]
    y = _between(level, x + w / 2, z + l / 2)
    if o.get("groundContour"):
        # contoured loc: highest ground under it
        r = math.ceil(sc.reach.get(mesh, 0) / TILE)
        y = max([y] + [level.get((x + dx, z + dz), 0) for dx in range(-r, r + w + 1) for dz in range(-r, r + l + 1)])
    sx, sy, sz = (o.get("scale" + a, 128) / 128 * extra.get("scale" + a, extra.get("scale", 128)) / 128 for a in "XYZ")
    # offsets, y down
    ox = OFFSET * o.get("offsetX", 0) + extra.get("translateX", 0)
    oy = -(OFFSET * o.get("offsetY", 0) + extra.get("translateY", 0))
    oz = OFFSET * o.get("offsetZ", 0) + extra.get("translateZ", 0)
    # placement tilt, y mirrored: negate x and z
    q = extra.get("rotation")
    tilt = (-q[0] / 32768, q[1] / 32768, -q[2] / 32768, q[3] / 32768) if q else (0, 0, 0, 1)
    mirror = bool(o.get("mirror"))
    # an L corner is its model twice
    if shape == 2:
        pieces = [(rot, not mirror), ((rot + 1) & 3, mirror)]
    else:
        pieces = [(rot + (0.5 if shape in (6, 7, 8, 11) else 0), mirror)]
    for turns, flip in pieces:
        half = turns * math.pi / 4
        r = _rotation(_qmul(tilt, (0, math.sin(half), 0, math.cos(half))))
        scale = (sx, sy, -sz if flip else sz)
        # turn, scale, place; column-major
        m = [r[i][j] * scale[j] for j in range(3) for i in range(3)]
        m = m[0:3] + [0] + m[3:6] + [0] + m[6:9] + [0] + [cx + ox, y + oy, cz + oz, 1]
        node = {"mesh": mesh, "matrix": m}
        if o.get("followsGround"):
            node["mesh"] = sc.follow(mesh, m, p, y)
        if shown_on:
            # the floor it shows on
            node["extras"] = {"level": shown_on}
        sc.glb.node(node)
    return True


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 3:
        raise SystemExit(__doc__)
    print(json.dumps(build(int(a[0]), int(a[1]), a[2], int(a[3]) if len(a) > 3 else 0,
                           int(a[4]) if len(a) > 4 else 32)))
