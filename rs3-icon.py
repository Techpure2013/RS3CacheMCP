"""
Render an item as it appears in the inventory.

The config carries the whole icon transform, so this is not a guess at a nice angle: `model`
is the inventory mesh, `pitch`, `yaw` and `roll` are how the client poses it, `zoom` is the camera
distance, `offsetX` and `offsetY` nudge it in the frame, and `recolour` swaps palette entries.

Angles are in RS's 0-2047 turn, not degrees. Colour replacement is what makes one mesh into many
items -- the skillcape mesh is shared by all 27 capes and told to swap eleven palette entries,
and the barbarian assault whips are one abyssal whip with a single swap.

  python rs3-icon.py <item id or name> [more...] [-o out.png] [-size 128] [-sheet] [-fit [margin]]
                     [-extra x,y,z]

-fit scales the model to fill the frame, with a margin as a share of it (0.06 alone). -extra adds a
turn after the item's own pose, in RS turn units.
"""

import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PIL import Image, ImageDraw

from model_png import hsl_to_rgb
from rs3mcp import common
from rs3mcp import models as cache_models
from rs3mcp import textures as cache_textures

DB = common.DB
TURN = 2048.0                      # RS measures a full turn in 2048 units, not 360 degrees
BG = (26, 26, 30)
TEXCACHE = {}


def die(msg):
    print("  " + msg)
    sys.exit(1)


def connect():
    if not os.path.exists(DB):
        die("no cache db at " + DB)
    c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def find_items(c, words):
    out, seen = [], set()
    for w in words:
        if w.lstrip("-").isdigit():
            rows = c.execute("select id,name,data from config where type='items' and id=?",
                             (int(w),)).fetchall()
        else:
            rows = c.execute("select id,name,data from config where type='items' and name=? "
                             "collate nocase", (w,)).fetchall()
            if not rows:
                rows = c.execute("select id,name,data from config where type='items' and "
                                 "(name like ? or display like ?) collate nocase order by id limit 24",
                                 ("%" + w + "%", "%" + w + "%")).fetchall()
        if not rows:
            print("  no item matching %s" % w)
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
    return out


def load_textures(c, matids):
    """material id -> (Pillow image of its diffuse texture, alpha mode, alpha cutoff).

    Two hops: a material names a diffuse texture id, and the texture lives in its own cache index.
    Alpha mode 1 is an alpha test: under the cutoff is not drawn, which is how an emblem sits on a
    cape without a border."""
    images, out = {}, {}
    for m in sorted({m for m in matids if isinstance(m, int) and m >= 0}):
        row = c.execute("select data from config where type='materials' and id=?", (m,)).fetchone()
        if not row or not row["data"]:
            continue
        d = json.loads(row["data"])
        v = d.get("v0") or d.get("v1") or {}
        tex = v.get("diffuse")
        if isinstance(tex, dict):
            tex = tex.get("texture")
        if not isinstance(tex, int):
            continue
        if tex not in images:
            got = cache_textures.image(tex)
            images[tex] = Image.frombytes("RGBA", got[:2], got[2]) if got else None
        if images[tex] is not None:
            out[m] = (images[tex], v.get("alphaMode") or 0, v.get("alphaCutoff") or 0)
    return out


def spin(v, ex, ey, ez):
    """An extra turn applied after the item's own pose, in RS turn units, matching the pose bench's
    'extra turn, applied after' in x, y, z order. No single reading of the stored pose gets every
    item facing the way the game draws it, so the leftover is recorded per item instead of guessed
    at globally."""
    x, y, z = v
    if ex:
        c, s = math.cos(ex), math.sin(ex)
        y, z = y * c - z * s, y * s + z * c
    if ey:
        c, s = math.cos(ey), math.sin(ey)
        x, z = x * c + z * s, -x * s + z * c
    if ez:
        c, s = math.cos(ez), math.sin(ez)
        x, y = x * c - y * s, x * s + y * c
    return x, y, z


def pose(v, pitch, yaw, roll):
    """RS applies roll about z, then pitch about x, then yaw about y. Getting the order wrong
    still produces a plausible looking render, just not the one the game shows.

    Then a half turn about z, which is a 180 in the image plane rather than a tip or a spin. Found
    on the pose bench, and confirmed against both a one-rotation item and a two-rotation one: an
    item using a single rotation cannot be affected by the order, so agreeing on both means the
    order and the signs are right and not just right for one case."""
    x, y, z = v
    if roll:
        c, s = math.cos(roll), math.sin(roll)
        x, y = x * c - y * s, x * s + y * c
    if pitch:
        c, s = math.cos(pitch), math.sin(pitch)
        y, z = y * c - z * s, y * s + z * c
    if yaw:
        c, s = math.cos(yaw), math.sin(yaw)
        x, z = x * c + z * s, -x * s + z * c
    return -x, -y, z


def render_icon(mesh, cfg, size, textures=None, extra=None, fit=None):
    verts, tris, cols = mesh["verts"], mesh["tris"], mesh["colours"]
    uvs = mesh.get("uvs") or []
    textures = textures or {}
    if not verts or not tris:
        return None

    swap = {}
    for pair in (cfg.get("recolour") or []):
        if isinstance(pair, dict):
            swap[pair["from"]] = pair["to"]
    # the same trick one level up: a shared mesh becomes 27 skillcapes by swapping the emblem's
    # material, not its geometry
    matswap = {}
    for pair in (cfg.get("retexture") or []):
        if isinstance(pair, dict):
            matswap[pair["from"]] = pair["to"]

    pitch = (cfg.get("pitch") or 0) / TURN * 2 * math.pi
    yaw = (cfg.get("yaw") or 0) / TURN * 2 * math.pi
    roll = (cfg.get("roll") or 0) / TURN * 2 * math.pi
    zoom = cfg.get("zoom") or 1000
    tx = cfg.get("offsetX") or 0
    ty = cfg.get("offsetY") or 0

    pts = [pose(v, pitch, yaw, roll) for v in verts]
    if extra and any(extra):
        ex, ey, ez = [t / TURN * 2 * math.pi for t in extra]
        pts = [spin(v, ex, ey, ez) for v in pts]
    # zoom is a camera DISTANCE, not a scale: the client pushes the model back by it and
    # divides through. Treating it as a scale makes every item fill the frame, and makes a large
    # zoom look big when it should look small. The offsets shift in model space, before the
    # divide, so it moves more when the item is near.
    cx = cy = size / 2.0
    focal = size * 3.2
    scr = []
    for x, y, z in pts:
        depth = z + zoom
        if depth < 1:
            depth = 1
        scr.append((cx + (x + tx) * focal / depth, cy - (y + ty) * focal / depth, z))

    # The projection above is faithful to the client, zoom and translate included, which is what a
    # replica wants. A replacement icon wants the frame filled instead: the stored zoom is chosen
    # for the pose the client draws, so any other pose lands small, off-centre, or over the edge.
    # Fitting after projection is a flat 2d nudge, so it cannot disturb the pose or the depths.
    if fit is not None:
        xs = [p[0] for p in scr]
        ys = [p[1] for p in scr]
        spanx, spany = max(xs) - min(xs), max(ys) - min(ys)
        span = max(spanx, spany)
        if span > 0:
            scale = size * (1 - 2 * fit) / span
            ox = cx - (min(xs) + max(xs)) / 2 * scale
            oy = cy - (min(ys) + max(ys)) / 2 * scale
            scr = [(x * scale + ox, y * scale + oy, z) for x, y, z in scr]

    frame = np.zeros((size, size, 3), dtype=np.float32)
    frame[:, :] = BG
    alpha = np.zeros((size, size), dtype=np.float32)
    zbuf = np.full((size, size), -1e30, dtype=np.float32)
    yy, xx = np.mgrid[0:size, 0:size]

    for a, b, cidx, mat in tris:
        if max(a, b, cidx) >= len(scr):
            continue
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = scr[a], scr[b], scr[cidx]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue
        lo_x, hi_x = int(max(0, min(x0, x1, x2))), int(min(size - 1, max(x0, x1, x2)) + 1)
        lo_y, hi_y = int(max(0, min(y0, y1, y2))), int(min(size - 1, max(y0, y1, y2)) + 1)
        if lo_x >= hi_x or lo_y >= hi_y:
            continue
        px = xx[lo_y:hi_y, lo_x:hi_x] + 0.5
        py = yy[lo_y:hi_y, lo_x:hi_x] + 0.5
        w0 = ((x1 - px) * (y2 - py) - (x2 - px) * (y1 - py)) / area
        w1 = ((x2 - px) * (y0 - py) - (x0 - px) * (y2 - py)) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue
        z = w0 * z0 + w1 * z1 + w2 * z2
        sub_z = zbuf[lo_y:hi_y, lo_x:hi_x]
        win = inside & (z > sub_z)
        if not win.any():
            continue

        ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
        vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
        nz = ux * vy - uy * vx
        nx, ny = uy * vz - uz * vy, uz * vx - ux * vz
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        shade = 0.30 + 0.70 * abs(nz / ln)

        entry = textures.get(matswap.get(mat, mat)) if mat >= 0 else None
        tex, amode, acut = entry if entry else (None, 0, 0)
        if tex is not None and max(a, b, cidx) < len(uvs):
            # sample per pixel through the interpolated uv, so the emblem lands where it should
            tw, th = tex.size
            ua, va = uvs[a]; ub, vb = uvs[b]; uc, vc = uvs[cidx]
            uu = (w0 * ua + w1 * ub + w2 * uc)[win]
            vv = (w0 * va + w1 * vb + w2 * vc)[win]
            sx = np.clip((uu % 1.0) * (tw - 1), 0, tw - 1).astype(np.int32)
            sy = np.clip((vv % 1.0) * (th - 1), 0, th - 1).astype(np.int32)
            texel = TEXCACHE.setdefault(id(tex), np.asarray(tex, dtype=np.float32))
            if amode == 1:
                # drop the cut-out pixels, then carry on with only what survives
                keep = texel[sy, sx, 3] >= acut
                if not keep.any():
                    continue
                widx = np.where(win)
                win = np.zeros_like(win)
                win[widx[0][keep], widx[1][keep]] = True
                sx, sy = sx[keep], sy[keep]
            # A neutral weave takes the item's colour; a decal does not. Alpha tested materials are
            # the decals here, so tinting them is what made every emblem come out cape-coloured.
            if amode != 1 and cols and max(a, b, cidx) < len(cols):
                rgb = [0.0, 0.0, 0.0]
                for vi in (a, b, cidx):
                    r, g, bl = hsl_to_rgb(swap.get(cols[vi], cols[vi]))
                    rgb[0] += r; rgb[1] += g; rgb[2] += bl
                tint = [255.0 * ((v / 3.0 / 255.0) ** 0.45) for v in rgb]
            else:
                tint = [255.0, 255.0, 255.0]
            col = texel[sy, sx, :3] * np.array(tint, dtype=np.float32) / 255.0 * shade
            frame[lo_y:hi_y, lo_x:hi_x][win] = col
            alpha[lo_y:hi_y, lo_x:hi_x][win] = 255.0
            sub_z[win] = z[win]
            continue

        if cols and max(a, b, cidx) < len(cols):
            rgb = [0.0, 0.0, 0.0]
            for vi in (a, b, cidx):
                packed = swap.get(cols[vi], cols[vi])
                r, g, bl = hsl_to_rgb(packed)
                rgb[0] += r; rgb[1] += g; rgb[2] += bl
            base = [v / 3.0 for v in rgb]
        else:
            base = [170.0, 170.0, 175.0]
        col = [255.0 * ((v / 255.0) ** 0.45) * shade for v in base]

        frame[lo_y:hi_y, lo_x:hi_x][win] = col
        alpha[lo_y:hi_y, lo_x:hi_x][win] = 255.0
        sub_z[win] = z[win]

    rgba = np.dstack([np.clip(frame, 0, 255), np.clip(alpha, 0, 255)]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def main():
    argv = sys.argv[1:]
    size, out, sheet, extra, fit = 128, None, False, None, None
    words = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--out") and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        elif argv[i] in ("-size", "--size") and i + 1 < len(argv):
            size = int(argv[i + 1]); i += 2
        elif argv[i] in ("-extra", "--extra") and i + 1 < len(argv):
            extra = [int(t) for t in argv[i + 1].split(",")]; i += 2
        elif argv[i] in ("-fit", "--fit"):
            # a bare -fit takes a sensible margin, a number overrides it
            if i + 1 < len(argv) and argv[i + 1].replace(".", "", 1).isdigit():
                fit = float(argv[i + 1]); i += 2
            else:
                fit = 0.06; i += 1
        elif argv[i] in ("-sheet", "--sheet"):
            sheet = True; i += 1
        else:
            words.append(argv[i]); i += 1
    if not words:
        sys.exit(__doc__)

    c = connect()
    items = find_items(c, words)
    if not items:
        die("nothing to render")
    cfgs = [(r["id"], r["name"], json.loads(r["data"]) if r["data"] else {}) for r in items]

    # which materials do these meshes actually use, once each item's swaps are applied
    meshes, wanted = {}, set()
    for iid, name, d in cfgs:
        mid = (d.get("model") or [None])[0]
        m = cache_models.mesh(mid) if isinstance(mid, int) else None
        if not m:
            continue
        meshes[iid] = m
        mswap = {p["from"]: p["to"] for p in (d.get("retexture") or [])}
        for t in m["tris"]:
            if t[3] >= 0:
                wanted.add(mswap.get(t[3], t[3]))
    textures = load_textures(c, wanted)
    print("  %d materials referenced, %d textures decoded" % (len(wanted), len(textures)))

    imgs = []
    for iid, name, d in cfgs:
        if iid not in meshes:
            print("  %-7d %-40s no model" % (iid, name or ""))
            continue
        img = render_icon(meshes[iid], d, size, textures, extra, fit)
        if img is None:
            continue
        imgs.append((iid, name, d.get("name") or "", img))
        print("  %-7d %-40s %s" % (iid, name or "", d.get("name") or ""))

    if not imgs:
        die("nothing rendered")

    if sheet or len(imgs) > 1:
        cols = min(len(imgs), max(4, int(len(imgs) ** 0.5 * 1.4)))
        rows = (len(imgs) + cols - 1) // cols
        pad, lab = 8, 24
        canvas = Image.new("RGBA", (cols * (size + pad) + pad, rows * (size + pad + lab) + pad),
                           BG + (255,))
        d0 = ImageDraw.Draw(canvas)
        for n, (iid, name, disp, img) in enumerate(imgs):
            r, cc = divmod(n, cols)
            x = pad + cc * (size + pad)
            y = pad + r * (size + pad + lab)
            canvas.alpha_composite(img, (x, y))
            d0.text((x + 2, y + size + 2), (disp or name or "")[:22], fill=(215, 215, 220, 255))
            d0.text((x + 2, y + size + 13), str(iid), fill=(130, 130, 140, 255))
        dst = out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons.png")
        canvas.save(dst)
    else:
        dst = out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "%s.png" % (imgs[0][1] or imgs[0][0]))
        imgs[0][3].save(dst)
    print("  wrote %s" % dst)


if __name__ == "__main__":
    main()
