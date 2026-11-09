"""The ground of a 3D scene: its texture from the floors' own materials, and the water over it.

The tiles are the map square's NXT ones (file 5, maptiles.ground), the newer copy of the ground.
The texture is PX pixels a tile. Each underlay's material is laid in world space,
repeating every materialScale / 128 tiles, so neighbouring tiles join without a seam; underlays
blend across tile centres, and overlays paint the part of a tile their shape covers. A floor with no
material shows its colour.

A water tile's own floor is its bed; its surface is a floor of its own over the part of the tile
its shape gives the water, handed back separately for a mesh at the water's height.
"""

import json
import sqlite3

from . import cachefile, common, maprender, maptiles

PX = 32
# a floor's weight spreads this many tiles
BLEND = 3
# tint strength, fitted
STRENGTH = 0.6


def tint(colour, weight, base):
    """What a texture is multiplied by under colour (r, g, b from 0 to 1) when its material gives
    weight of its own colour up to it: STRENGTH of the way from white to the colour over base, the
    texture's own average. Twice the colour, as it was, assumed every texture averaged half grey:
    1842's averages 205, and Trollheim's rock came out yellow. The full way came out a quarter too
    dark beside RuneApps on Trollheim (45,55-57, 25 points off); 0.6 is 4 off on each. Fitted, not
    measured in game: in-game screenshots should set it."""
    k = weight * STRENGTH
    return [1 - k + k * min(1.0, c / max(b, 1 / 255)) for c, b in zip(colour, base)]


def base(extra, img):
    """A tinted material's own average colour, r, g, b from 0 to 1: its baseColour, which sits on
    its texture's mean for 152 of the 186 floor materials that tint; the texture's mean where it is
    unset (0)."""
    c = (extra or {}).get("baseColour") or 0
    if c:
        from .mapscene import hsl_rgb
        return [v / 255 for v in hsl_rgb(c)]
    import numpy as np
    return [float(v) / 255 for v in np.asarray(img.convert("RGB"), np.float64).reshape(-1, 3).mean(0)]


def _share(grid, r):
    """Each tile's share of grid's true tiles over the (2r+1)^2 tiles around it, none past the
    edge, as float32."""
    import numpy as np
    h, w = grid.shape
    k = 2 * r + 1
    acc = np.pad(grid.astype(np.int64), ((r + 1, r), (r + 1, r))).cumsum(0).cumsum(1)
    total = acc[k:k + h, k:k + w] - acc[0:h, k:k + w] - acc[k:k + h, 0:w] + acc[0:h, 0:w]
    return (total / (k * k)).astype(np.float32)


class Floors:
    """Floor configs and their materials' images, fetched as met."""

    def __init__(self, root=None):
        self.root = root
        self.db = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
        self.configs = cachefile.kept("configs", root, lambda: maprender._Configs(root))
        self.images = {}
        self.repeats = {}
        self.shares = {}
        self.extras = {}
        self.bases = {}
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

    def share(self, mid):
        """How much of its texture's own colour a material keeps, 0 to 255; v1 keeps it all."""
        if mid in self.shares:
            return self.shares[mid]
        row = self.db.execute("select data from config where type='materials' and id=?", (mid,)).fetchone()
        v0 = (json.loads(row[0]) if row else {}).get("v0") or {}
        self.shares[mid] = (v0.get("extra") or {}).get("baseColourShare", 255) if v0 else 255
        self.extras[mid] = v0.get("extra")
        return self.shares[mid]

    def base(self, mid):
        """base() of a material this has read the share of."""
        if mid not in self.bases:
            self.bases[mid] = base(self.extras.get(mid), self.material_image(mid))
        return self.bases[mid]

    def repeat(self, mid, scale):
        """A material's texture as one repeat at map size, float32 (n, n, 4), or None: one repeat per
        scale / 128 tiles, area averaged down so a fine texture keeps its colour instead of
        aliasing into specks."""
        import numpy as np
        from PIL import Image
        n = int(round(max(scale or 128, 16) / 128 * PX))
        key = (mid, n)
        if key not in self.repeats:
            img = self.material_image(mid)
            if img is None:
                self.repeats[key] = None
            else:
                # drop alpha: resizing scales colour by it
                rgb = img.convert("RGB")
                rgb = rgb.resize((n, n), Image.BOX if n < min(rgb.size) else Image.BICUBIC)
                self.repeats[key] = np.asarray(rgb.convert("RGBA")).astype(np.float32)
        return self.repeats[key]

    def source(self, kind, fid):
        """How this floor is laid: ("texture", one repeat as laid out by repeat(), its tint as float32
        rgb or None), ("colour", rgb), or None when it shows nothing."""
        import numpy as np
        d = self.configs.floor(kind, fid)
        if d is None:
            return None
        tex = self.repeat(d["material"], d.get("materialScale")) if d.get("material") is not None else None
        if tex is not None:
            share = self.share(d["material"])
            tinted = None
            if share < 255 and d.get("colour"):
                tinted = np.array(tint([d["colour"][k] / 255 for k in "rgb"], 1 - share / 255,
                                       self.base(d["material"])), np.float32)
            return "texture", tex, tinted
        rgb = self.configs.colour(kind, fid)
        return ("colour", rgb) if rgb is not None else None

    def block(self, kind, fid, x0, z0, w, h, r, c):
        """laid_out(kind, fid, x0, z0, w, h)[r:r + PX, c:c + PX, :3] without laying out the rest, or
        None when the floor shows nothing."""
        import numpy as np
        src = self.source(kind, fid)
        if src is None:
            return None
        if src[0] == "colour":
            out = np.empty((PX, PX, 3), np.float32)
            out[:] = tuple(src[1])
            return out
        tex, tinted = src[1], src[2]
        size = tex.shape[0]
        xs = (np.arange(c, c + PX) + x0 * PX) % size
        # north row first
        zs = (h * PX - 1 - np.arange(r, r + PX) + z0 * PX) % size
        out = tex[zs][:, xs][..., :3]
        if tinted is not None:
            out = out * tinted
        return out

    def laid_out(self, kind, fid, x0, z0, w, h):
        """rgba array (h*PX, w*PX, 4) of this floor over tiles x0.., z0.., north row first, or None.

        A ground texture's alpha is not how see-through it is: paths carry 0 and still show."""
        import numpy as np
        key = (kind, fid, x0, z0, w, h)
        if key in self.laid:
            return self.laid[key]
        src = self.source(kind, fid)
        out = None
        if src is not None and src[0] == "texture":
            tex, tinted = src[1], src[2]
            size = tex.shape[0]
            xs = (np.arange(w * PX) + x0 * PX) % size
            # north row first
            zs = (np.arange(h * PX)[::-1] + z0 * PX) % size
            out = tex[zs][:, xs]
            if tinted is not None:
                out = out.copy()
                out[..., :3] *= tinted
        elif src is not None:
            out = np.empty((h * PX, w * PX, 4), np.float32)
            out[:] = tuple(src[1]) + (255,)
        self.laid[key] = out
        return out


_GROUND = {}


def _ground(sx, sz, root):
    """maptiles.ground, kept for the life of the process: a scene asks for each square many times."""
    key = (sx, sz, root)
    if key not in _GROUND:
        if len(_GROUND) > 64:
            _GROUND.clear()
        _GROUND[key] = maptiles.ground(sx, sz, root)
    return _GROUND[key]


def _tiles(x0, z0, x1, z1, root, water=False, plane=0):
    """{(x, z): tile} of one plane over the area, or with water its water surfaces. Each plane is its
    own: a bridge deck is plane 1's floor, drawn over the water by its visual floor (mapscene)."""
    out = {}
    for sx in range(x0 // 64, x1 // 64 + 1):
        for sz in range(z0 // 64, z1 // 64 + 1):
            got = _ground(sx, sz, root)
            if not got:
                continue
            if water:
                out.update(((sx * 64 + x, sz * 64 + z), t) for (x, z), t in got[1][plane].items())
                continue
            for x in range(64):
                for z in range(64):
                    out[(sx * 64 + x, sz * 64 + z)] = got[0][plane][x][z]
    return out


def _area(x0, z0, x1, z1, root, plane=0):
    """One plane over tiles x0..x1, z0..z1: (under, overlays, surfaces). under is (h, w) north row
    first, each tile's underlay id or -1; overlays and surfaces are [(x, z, overlay, shape, rotation)],
    the dry overlays and the water surfaces in the order _tiles' dicts hold them (squares west to east
    then south to north, then x, then z), since overlay ties and water spreading keep that order."""
    import numpy as np
    w, h = x1 - x0 + 1, z1 - z0 + 1
    under = np.full((h, w), -1, np.int64)
    overlays, surfaces = [], []
    from . import mapcore
    if mapcore.lib() is None:
        tiles = _tiles(x0, z0, x1, z1, root, plane=plane)
        for (x, z), t in tiles.items():
            if x0 <= x <= x1 and z0 <= z <= z1:
                if "underlay" in t:
                    under[z1 - z, x - x0] = t["underlay"]
                if "overlay" in t:
                    overlays.append((x, z, t["overlay"], t.get("shape", 0), t.get("rotation", 0)))
        for (x, z), t in _tiles(x0, z0, x1, z1, root, water=True, plane=plane).items():
            if x0 <= x <= x1 and z0 <= z <= z1:
                surfaces.append((x, z, t.get("overlay"), t.get("shape", 0), t.get("rotation", 0)))
        return under, overlays, surfaces
    for sx in range(x0 // 64, x1 // 64 + 1):
        for sz in range(z0 // 64, z1 // 64 + 1):
            gr = maptiles.grid(sx, sz, root)
            if gr is None:
                continue
            xs, zs = sx * 64 + np.arange(64), sz * 64 + np.arange(64)
            inx, inz = (xs >= x0) & (xs <= x1), (zs >= z0) & (zs <= z1)
            if not inx.any() or not inz.any():
                continue
            u = gr["underlay"][plane]
            a, b = np.nonzero((u != 0) & inx[:, None] & inz[None, :])
            under[z1 - zs[b], xs[a] - x0] = u[a, b]
            for kind, out in (("overlay", overlays), ("surface_overlay", surfaces)):
                field = gr[kind][plane]
                keep = (gr["surface"][plane] != 0) if kind == "surface_overlay" else (field != 0)
                a, b = np.nonzero(keep & inx[:, None] & inz[None, :])
                shape = gr["surface_shape" if kind == "surface_overlay" else "shape"][plane]
                turn = gr["surface_rotation" if kind == "surface_overlay" else "rotation"][plane]
                out.extend(zip((xs[a]).tolist(), (zs[b]).tolist(), field[a, b].tolist(), shape[a, b].tolist(),
                               turn[a, b].tolist()))
    return under, overlays, surfaces


def _upsample(a):
    """A tile grid (h, w) to pixels, linear between tile centres."""
    import numpy as np
    from PIL import Image
    h, w = a.shape
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((w * PX, h * PX), Image.BILINEAR))


def _masks():
    """[shape][rotation] -> bool (PX, PX), rows from north, from maprender's 4 x 4 tables with their
    steps smoothed into the straight edges they stand for."""
    import numpy as np
    from PIL import Image
    out = []
    for mask in maprender.SHAPES:
        row = []
        for rot in maprender.ROTATIONS:
            cells = np.array([mask[rot[k]] for k in range(16)], np.float32).reshape(4, 4)
            row.append(np.asarray(Image.fromarray(cells, "F").resize((PX, PX), Image.BILINEAR)) >= 0.5)
        out.append(row)
    return out


def texture(x0, z0, x1, z1, root=None, floors=None, with_water=False):
    """The ground texture of tiles x0..x1, z0..z1 as an RGB Pillow image, north up, holes dark; with
    water, (that image as RGBA clear at the holes, the water surface as an RGBA image clear where
    there is none, or None)."""
    import numpy as np
    from PIL import Image
    floors = floors or Floors(root)
    under, overlays, surfaces = _area(x0, z0, x1, z1, root)
    masks = _masks()
    img, has = _underlays(under, x0, z1, floors)
    skip = {(x, z) for x, z, *_ in surfaces}
    img, drawn, holes = _overlays(img, overlays, skip, x0, z0, x1, z1, floors, masks)
    sea, colour = _surface(surfaces, x0, z0, x1, z1, floors, masks)
    from . import mapcore
    if with_water and mapcore.lib() is not None:
        return mapcore.finish(img, colour, has, drawn, holes, _edge_blur(sea) if sea.any() else None, PX)
    s = _smooth_edge(sea) if sea.any() else np.zeros_like(sea)
    img = img * (1 - s[..., None]) + colour * s[..., None]
    # holes and empty tiles: dark
    empty = (1 - np.kron(has, np.ones((PX, PX), np.float32))) * (1 - drawn) * (1 - s)
    gone = np.maximum(holes, empty)
    img *= 1 - gone[..., None]
    if not with_water:
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    out = Image.fromarray(np.dstack([np.clip(img, 0, 255), (1 - gone) * 255]).astype(np.uint8), "RGBA")
    if not s.any():
        return out, None
    rgba = np.dstack([np.clip(colour, 0, 255), s * 255]).astype(np.uint8)
    return out, Image.fromarray(rgba, "RGBA")


def floor(x0, z0, x1, z1, plane, root=None, floors=None, keep=None):
    """One upper plane's floor over tiles x0..x1, z0..z1 as an RGBA Pillow image, north up, clear
    where that plane has none, or None when it has none at all; with keep, only the tiles it passes:
    an (h, w) bool array north row first, or keep(x, z) for each tile."""
    import numpy as np
    from PIL import Image
    floors = floors or Floors(root)
    under, overlays, _ = _area(x0, z0, x1, z1, root, plane)
    if keep is not None:
        if callable(keep):
            keep = np.array([[bool(keep(x0 + c, z1 - r)) for c in range(x1 - x0 + 1)] for r in range(z1 - z0 + 1)])
        under = np.where(keep, under, -1)
        overlays = [o for o in overlays if keep[z1 - o[1], o[0] - x0]]
    used = under >= 0
    for x, z, *_ in overlays:
        used[z1 - z, x - x0] = True
    if not used.any():
        return None
    # its tiles and edge room
    r0, r1, c0, c1 = _reach(used, max(2, BLEND + 1))
    img, has = _underlays(under[r0:r1, c0:c1], x0 + c0, z1 - r0, floors)
    img, cover, holes = _overlays(img, overlays, set(), x0 + c0, z1 - r1 + 1, x0 + c1 - 1, z1 - r0, floors, _masks())
    alpha = np.maximum(np.kron(has, np.ones((PX, PX), np.float32)), cover) * (1 - holes)
    if not alpha.any():
        return None
    rgba = np.zeros(((z1 - z0 + 1) * PX, (x1 - x0 + 1) * PX, 4), np.uint8)
    rgba[r0 * PX:r1 * PX, c0 * PX:c1 * PX] = np.dstack([np.clip(img, 0, 255), alpha * 255]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def _underlays(under, x0, z1, floors):
    """(rgb (h*PX, w*PX, 3), (h, w) 1 where a tile has an underlay) of the underlays blended, under
    the area's underlay ids north row first (-1 none) and x0, z1 its west and north tiles. A floor's
    weight on a tile is its share of the tiles within BLEND of it, linear between tile centres: a
    mountain's three floors scattered tile by tile then read as one rock, not blobs."""
    import numpy as np
    h, w = under.shape
    from . import mapcore
    if mapcore.lib() is not None:
        parts = []
        for fid in np.unique(under[under >= 0]):
            src = floors.source("underlays", int(fid))
            if src is not None:
                r0, r1, c0, c1 = _reach(under == fid, BLEND + 1)
                parts.append((int(fid), r0, r1, c0, c1, src, _share(under == fid, BLEND)[r0:r1, c0:c1]))
        return mapcore.underlays(h, w, parts, PX, x0, z1), (under >= 0).astype(np.float32)
    img = np.zeros((h * PX, w * PX, 3), np.float32)
    weight = np.zeros((h * PX, w * PX), np.float32)
    for fid in np.unique(under[under >= 0]):
        # its tiles plus the blend's reach
        r0, r1, c0, c1 = _reach(under == fid, BLEND + 1)
        laid = floors.laid_out("underlays", int(fid), x0 + c0, z1 - r1 + 1, c1 - c0, r1 - r0)
        if laid is None:
            continue
        wgt = _upsample(_share(under == fid, BLEND)[r0:r1, c0:c1])
        img[r0 * PX:r1 * PX, c0 * PX:c1 * PX] += laid[..., :3] * wgt[..., None]
        weight[r0 * PX:r1 * PX, c0 * PX:c1 * PX] += wgt
    img /= np.maximum(weight, 1e-6)[..., None]
    return img, (under >= 0).astype(np.float32)


def _reach(grid, margin):
    """(r0, r1, c0, c1): the rows and columns of a tile grid holding anything, margin tiles wider,
    kept inside it."""
    import numpy as np
    rows, cols = np.nonzero(grid)
    h, w = grid.shape
    return (max(rows.min() - margin, 0), min(rows.max() + 1 + margin, h),
            max(cols.min() - margin, 0), min(cols.max() + 1 + margin, w))


def _overlays(img, overlays, skip, x0, z0, x1, z1, floors, masks):
    """(img with the overlays painted over it, (h*PX, w*PX) 1 where one was, the same where an
    overlay cuts a hole), overlays as _area gives them, tiles in skip left out. The draw-nothing
    overlay takes its shape out of the tile, underlay too: the Taverley mine's pit is such tiles, an
    opening in the ground."""
    import numpy as np
    w, h = x1 - x0 + 1, z1 - z0 + 1
    cover = {}
    holes = np.zeros((h * PX, w * PX), np.float32)
    for x, z, overlay, shape, rotation in overlays:
        if (x, z) in skip:
            continue
        r, c, mask = z1 - z, x - x0, masks[shape % len(masks)][rotation]
        if floors.configs.cuts("overlays", overlay):
            holes[r * PX:(r + 1) * PX, c * PX:(c + 1) * PX][mask] = 1
        else:
            cover.setdefault(overlay, []).append((r, c, mask))
    drawn = np.zeros((h * PX, w * PX), np.float32)
    from . import mapcore
    native = mapcore.lib() is not None
    if native:
        img = np.ascontiguousarray(img, np.float32)
    # lowest blend priority first
    for fid in sorted(cover, key=lambda f: (floors.configs.floor("overlays", f) or {}).get("blendPriority", 0)):
        grid = np.zeros((h, w), bool)
        for r, c, _ in cover[fid]:
            grid[r, c] = True
        # its tiles plus room for the softened edge
        r0, r1, c0, c1 = _reach(grid, 2)
        if native:
            src = floors.source("overlays", fid)
            if src is None:
                continue
        else:
            laid = floors.laid_out("overlays", fid, x0 + c0, z1 - r1 + 1, c1 - c0, r1 - r0)
            if laid is None:
                continue
        m = np.zeros(((r1 - r0) * PX, (c1 - c0) * PX), np.float32)
        for r, c, mask in cover[fid]:
            m[(r - r0) * PX:(r - r0 + 1) * PX, (c - c0) * PX:(c - c0 + 1) * PX][mask] = 1
        soft = np.maximum(m, _soften(m)) if floors.configs.floor("overlays", fid).get("blendsUnder") else m
        if native:
            mapcore.overlay(img, drawn, PX, x0, z1, (r0, r1, c0, c1), m, soft, src)
            continue
        part = (slice(r0 * PX, r1 * PX), slice(c0 * PX, c1 * PX))
        drawn[part] = np.maximum(drawn[part], m)
        img[part] = img[part] * (1 - soft[..., None]) + laid[..., :3] * soft[..., None]
    return img, drawn, holes


def _surface(surfaces, x0, z0, x1, z1, floors, masks):
    """(coverage, rgb) of the water's surface: each water tile wears its surface floor over the part
    of the tile its shape gives the water. The NXT tiles shape every bank, so nothing is linked."""
    import numpy as np
    w, h = x1 - x0 + 1, z1 - z0 + 1
    sea = np.zeros((h * PX, w * PX), np.float32)
    colour = np.zeros((h * PX, w * PX, 3), np.float32)
    painted = {}
    for x, z, overlay, shape, rotation in surfaces:
        if not overlay or floors.configs.cuts("overlays", overlay):
            continue
        r, c = (z1 - z) * PX, (x - x0) * PX
        block = floors.block("overlays", overlay, x0, z0, w, h, r, c)
        if block is None:
            continue
        sea[r:r + PX, c:c + PX][masks[shape % len(masks)][rotation]] = 1
        colour[r:r + PX, c:c + PX] = block
        painted[(x, z)] = overlay
    # water texture one tile past the water
    for (x, z), overlay in list(painted.items()):
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                k = (x + dx, z + dz)
                if k in painted or not (x0 <= k[0] <= x1 and z0 <= k[1] <= z1):
                    continue
                r, c = (z1 - k[1]) * PX, (k[0] - x0) * PX
                colour[r:r + PX, c:c + PX] = floors.block("overlays", overlay, x0, z0, w, h, r, c)
                painted[k] = overlay
    return sea, colour


def _edge_blur(m):
    """_smooth_edge's blur of a coverage mask, as uint8."""
    import numpy as np
    from PIL import Image, ImageFilter
    return np.asarray(Image.fromarray((m * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(PX / 3)))


def _smooth_edge(m):
    """A coverage mask's tile steps redrawn as a smooth, anti-aliased edge, straight diagonals kept."""
    import numpy as np
    a = np.asarray(_edge_blur(m), np.float32) / 255
    t = np.clip((a - 0.4) / 0.2, 0, 1)
    return t * t * (3 - 2 * t)


def _soften(m):
    """A coverage mask fading out over about half a tile, for overlays that bleed."""
    import numpy as np
    from PIL import Image, ImageFilter
    blurred = Image.fromarray((m * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(PX / 3))
    return np.asarray(blurred, np.float32) / 255
