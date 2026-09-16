"""
Render a model straight to a png, so it can be looked at without a 3d viewer. Takes a model id out
of the game cache, or an rsmv model json.

Software rasteriser, no gpu and no dependencies past Pillow and numpy. Painter's algorithm: sort the
triangles back to front and fill them. Good enough for "what is this thing", which is the point.

Vertex colour in the cache is RS's packed hsl -- 6 bits hue, 3 saturation, 7 lightness in one
ushort -- not rgb, so it has to be unpacked or everything comes out grey noise.

  python model_png.py <model id or rsmv models-N.json> <out.png> [size] [label]
"""

import colorsys
import io
import math
import sys

import numpy as np

from PIL import Image, ImageDraw

from model_obj import load

BG = (26, 26, 30, 255)


def hsl_to_rgb(packed):
    """RS packs colour as hue<<10 | sat<<7 | lightness, and lightness is the top 7 bits."""
    h = ((packed >> 10) & 0x3F) / 64.0
    s = ((packed >> 7) & 0x07) / 8.0
    l = (packed & 0x7F) / 128.0
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def rotate(p, yaw, pitch):
    x, y, z = p
    cy, sy = math.cos(yaw), math.sin(yaw)
    x, z = x * cy - z * sy, x * sy + z * cy
    cp, sp = math.cos(pitch), math.sin(pitch)
    y, z = y * cp - z * sp, y * sp + z * cp
    return x, y, z


def render(m, size, yaw, pitch):
    """Z-buffered, per pixel. Sorting whole triangles by average depth is not good enough here:
    these meshes interleave, so a painter's pass lets far faces punch through near ones and the
    result is unreadable. Depth has to be resolved per pixel."""
    verts, tris, cols = m["verts"], m["tris"], m["colours"]
    if not verts or not tris:
        return None
    pts = [rotate(v, yaw, pitch) for v in verts]

    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    scale = (size * 0.82) / span
    cx, cy = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
    # y is up in the cache and down on screen, so flip it
    scr = [(size / 2 + (p[0] - cx) * scale, size / 2 - (p[1] - cy) * scale, p[2]) for p in pts]

    frame = np.zeros((size, size, 3), dtype=np.float32)
    frame[:, :] = BG[:3]
    zbuf = np.full((size, size), -1e30, dtype=np.float32)
    yy, xx = np.mgrid[0:size, 0:size]

    for a, b, c, sub, mat in tris:
        if max(a, b, c) >= len(scr):
            continue
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = scr[a], scr[b], scr[c]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue

        lo_x, hi_x = int(max(0, min(x0, x1, x2))), int(min(size - 1, max(x0, x1, x2)) + 1)
        lo_y, hi_y = int(max(0, min(y0, y1, y2))), int(min(size - 1, max(y0, y1, y2)) + 1)
        if lo_x >= hi_x or lo_y >= hi_y:
            continue
        px = xx[lo_y:hi_y, lo_x:hi_x] + 0.5
        py = yy[lo_y:hi_y, lo_x:hi_x] + 0.5

        # barycentric coverage
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

        # headlight off the face normal in view space, so facing surfaces read bright
        ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
        vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
        nz = ux * vy - uy * vx
        nx, ny = uy * vz - uz * vy, uz * vx - ux * vz
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        shade = 0.30 + 0.70 * abs(nz / ln)

        if cols and max(a, b, c) < len(cols):
            ca, cb, cc = hsl_to_rgb(cols[a]), hsl_to_rgb(cols[b]), hsl_to_rgb(cols[c])
            base = [(ca[i] + cb[i] + cc[i]) / 3.0 for i in range(3)]
        else:
            base = [170.0, 170.0, 175.0]
        # RS lightness only runs to 127 and the game lights models a lot harder than a raw
        # multiply, so straight hsl comes out nearly black. Lift the midtones.
        col = [255.0 * ((v / 255.0) ** 0.45) * shade for v in base]

        sub_f = frame[lo_y:hi_y, lo_x:hi_x]
        sub_f[win] = col
        sub_z[win] = z[win]

    return Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).convert("RGBA")


def best_yaw(m, steps=72):
    """Turn the model to face the camera before rendering.

    Yaw zero is an arbitrary cache axis, not the front of anything: a door is thin on x and wide
    on z, so it renders as a sliver. Spin it and keep the angle with the largest silhouette, which
    is the one that actually shows what the thing is."""
    verts = m["verts"]
    if not verts:
        return 0.0
    best, bestarea = 0.0, -1.0
    for i in range(steps):
        yaw = i * 2 * math.pi / steps
        cy, sy = math.cos(yaw), math.sin(yaw)
        xs = [v[0] * cy - v[2] * sy for v in verts]
        ys = [v[1] for v in verts]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if area > bestarea:
            best, bestarea = yaw, area
    return best


def main():
    src, dst = sys.argv[1], sys.argv[2]
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 320
    label = sys.argv[4] if len(sys.argv) > 4 else ""
    m = load(src)
    print("%d vertices, %d triangles" % (len(m["verts"]), len(m["tris"])))

    base = best_yaw(m)
    print("facing yaw %.0f degrees" % math.degrees(base))
    # four turntable angles from there, because one view of an unknown mesh is rarely enough
    # the widest silhouette first, which is a wolf's side and a door's face, so not named front
    views = [("widest", base), ("turned 90", base + math.pi / 2),
             ("turned 180", base + math.pi), ("turned 270", base - math.pi / 2)]
    pitch = math.radians(20)
    pad, lab = 8, 16
    sheet = Image.new("RGBA", (4 * size + 5 * pad, size + 2 * pad + lab + (lab if label else 0)), BG)
    d = ImageDraw.Draw(sheet)
    top = pad + (lab if label else 0)
    if label:
        d.text((pad, 4), label, fill=(215, 215, 220, 255))
    for i, (name, yaw) in enumerate(views):
        im = render(m, size, yaw, pitch)
        if im is None:
            continue
        x = pad + i * (size + pad)
        sheet.alpha_composite(im, (x, top))
        d.text((x + 2, top + size + 2), name, fill=(150, 150, 160, 255))
    sheet.save(dst)
    print("wrote", dst)


if __name__ == "__main__":
    main()
