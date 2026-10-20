"""The ground of a 3D scene: its texture from the floors' own materials, and the water over it.

The texture is PX pixels a tile. Each underlay's material is laid in world space, repeating every
materialScale game units, so neighbouring tiles join without a seam; underlays blend across tile
centres, and overlays paint the part of a tile their shape covers. A floor with no material shows
its colour.

The water layer (mapsquare file 4) is a surface of its own: a tile is water where overlay 42 does
not cut it, at the water height it stores, or its neighbours' where it stores none, as bank tiles
do. Its alpha follows the cut, so banks keep their diagonals.
"""

import json
import sqlite3

from . import common, maprender, maptiles

PX = 32
# tile overlay drawing nothing (config 41)
CUT = 42


class Floors:
    """Floor configs and their materials' images, fetched as met."""

    def __init__(self, root=None):
        self.root = root
        self.db = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
        self.configs = maprender._Configs(root)
        self.images = {}
        self.laid = {}

    def material_image(self, mid):
        """A material's diffuse texture as an RGBA Pillow image, or None."""
        if mid not in self.images:
            self.images[mid] = None
            row = self.db.execute("select data from config where type='materials' and id=?", (mid,)).fetchone()
            d = json.loads(row[0]) if row else {}
            # two layouts
            tex = (d.get("v0") or {}).get("diffuse") if d.get("v0") else ((d.get("v1") or {}).get("diffuse") or {}).get("texture")
            if tex is not None:
                from PIL import Image
                from . import textures
                got = textures.image(tex, self.root)
                if got:
                    w, h, px = got
                    self.images[mid] = Image.frombytes("RGBA", (w, h), bytes(px))
        return self.images[mid]

    def laid_out(self, kind, fid, x0, z0, w, h):
        """rgba array (h*PX, w*PX, 4) of this floor over tiles x0.., z0.., north row first, or None.

        A ground texture's alpha is not how see-through it is: paths carry 0 and still show."""
        import numpy as np
        key = (kind, fid, x0, z0, w, h)
        if key in self.laid:
            return self.laid[key]
        d = self.configs.floor(kind, fid)
        if d is None:
            self.laid[key] = None
            return None
        out = None
        img = self.material_image(d["material"]) if d.get("material") is not None else None
        if img is not None:
            tex = np.asarray(img).astype(np.float32)
            th, tw = tex.shape[:2]
            span = max(d.get("materialScale") or 512, 64) / 512 * PX
            xs = ((np.arange(w * PX) + x0 * PX) / span * tw).astype(int) % tw
            # north row first
            zs = ((np.arange(h * PX)[::-1] + z0 * PX) / span * th).astype(int) % th
            out = tex[zs][:, xs]
        else:
            rgb = self.configs.colour(kind, fid)
            if rgb is not None:
                out = np.empty((h * PX, w * PX, 4), np.float32)
                out[:] = tuple(rgb) + (255,)
        self.laid[key] = out
        return out


def _tiles(x0, z0, x1, z1, root, water=False):
    """{(x, z): tile} of level 0 over the area, the ground's or the water layer's."""
    out = {}
    for sx in range(x0 // 64, x1 // 64 + 1):
        for sz in range(z0 // 64, z1 // 64 + 1):
            if water:
                layer = maptiles.water(sx, sz, root)
            else:
                got = maptiles.square(sx, sz, root)
                layer = got[0][0] if got else None
            if not layer:
                continue
            for x in range(64):
                for z in range(64):
                    out[(sx * 64 + x, sz * 64 + z)] = layer[x][z]
    return out


def _upsample(a):
    """A tile grid (h, w) to pixels, linear between tile centres."""
    import numpy as np
    from PIL import Image
    h, w = a.shape
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((w * PX, h * PX), Image.BILINEAR))


def _masks():
    """[shape][rotation] -> bool (PX, PX), rows from north, from maprender's 4 x 4 tables."""
    import numpy as np
    out = []
    for mask in maprender.SHAPES:
        row = []
        for rot in maprender.ROTATIONS:
            cells = np.array([mask[rot[k]] for k in range(16)], bool).reshape(4, 4)
            row.append(np.kron(cells, np.ones((PX // 4, PX // 4), bool)))
        out.append(row)
    return out


def texture(x0, z0, x1, z1, root=None, floors=None):
    """The ground texture of tiles x0..x1, z0..z1 as an RGB Pillow image, north up."""
    import numpy as np
    from PIL import Image
    floors = floors or Floors(root)
    w, h = x1 - x0 + 1, z1 - z0 + 1
    tiles = _tiles(x0, z0, x1, z1, root)
    under = np.full((h, w), -1)
    for (x, z), t in tiles.items():
        if x0 <= x <= x1 and z0 <= z <= z1 and "underlay" in t:
            under[z1 - z, x - x0] = t["underlay"]
    img = np.zeros((h * PX, w * PX, 3), np.float32)
    weight = np.zeros((h * PX, w * PX), np.float32)
    for fid in np.unique(under[under >= 0]):
        laid = floors.laid_out("underlays", int(fid), x0, z0, w, h)
        if laid is None:
            continue
        # a tile's own weight, softened over its neighbours
        wgt = _upsample((under == fid).astype(np.float32))
        img += laid[..., :3] * wgt[..., None]
        weight += wgt
    img /= np.maximum(weight, 1e-6)[..., None]
    masks = _masks()
    for (x, z), t in tiles.items():
        if not (x0 <= x <= x1 and z0 <= z <= z1) or "overlay" not in t or t["overlay"] == CUT:
            continue
        laid = floors.laid_out("overlays", t["overlay"], x0, z0, w, h)
        if laid is None:
            continue
        r, c = (z1 - z) * PX, (x - x0) * PX
        m = masks[t.get("shape", 0) % len(masks)][t.get("rotation", 0)]
        cell = img[r:r + PX, c:c + PX]
        cell[m] = laid[r:r + PX, c:c + PX, :3][m]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


def water(x0, z0, x1, z1, height, root=None, floors=None):
    """(corner heights {(x, z): units}, RGBA Pillow image north up) of the water over the area, or None.

    height turns a stored height into units, the scene's HEIGHT."""
    import numpy as np
    from PIL import Image
    floors = floors or Floors(root)
    tiles = _tiles(x0, z0, x1, z1, root, water=True)
    w, h = x1 - x0 + 1, z1 - z0 + 1
    masks = _masks()
    alpha = np.zeros((h * PX, w * PX), np.uint8)
    colour = np.zeros((h * PX, w * PX, 4), np.uint8)
    level = {}
    for (x, z), t in tiles.items():
        if not (x0 <= x <= x1 and z0 <= z <= z1) or "underlay" not in t:
            continue
        cut = t.get("overlay") == CUT
        if cut and t.get("shape", 0) == 0:
            continue
        r, c = (z1 - z) * PX, (x - x0) * PX
        keep = ~masks[t.get("shape", 0) % len(masks)][t.get("rotation", 0)] if cut else np.ones((PX, PX), bool)
        alpha[r:r + PX, c:c + PX][keep] = 255
        # the game map's colour here, teal in swamps
        colour[r:r + PX, c:c + PX, :3] = floors.configs.water(x, z)
        if "height" in t:
            level[(x, z)] = t["height"] * height
    if not alpha.any():
        return None
    # bank tiles take the nearest stored level
    corners = {}
    for x in range(x0, x1 + 2):
        for z in range(z0, z1 + 2):
            near = [level[k] for k in ((x - 1, z - 1), (x, z - 1), (x - 1, z), (x, z)) if k in level]
            if near:
                corners[(x, z)] = sum(near) / len(near)
    if not corners:
        return None
    fill = sum(corners.values()) / len(corners)
    colour[..., 3] = alpha
    return {"corners": corners, "fill": fill, "image": Image.fromarray(colour, "RGBA")}
