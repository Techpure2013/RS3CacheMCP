"""Model textures as rgba, from the bmp index (54) or the dds index (52).

Both wrap the image the same way: a count byte, then for bmp width and height as u32 and raw rgba,
for dds a u32 size and a dds file of DXT1 or DXT5 blocks. Material textures carry a 32 pixel
border for filtering, which is cut off. The client fetches the bmp index as it needs it, so
image() falls back to the dds copy, which is complete but lossy.

A normal map's dds copy keeps X in alpha and Y in green, and its blue is 0. The bmp copy has Z in
blue, sqrt(1 - x^2 - y^2) scaled from -1..1 to 0..255, so dds() rebuilds it the same way.
"""

import math
import struct

from . import cachefile

BMP = 54
DDS = 52
PAD = 32

_normals = None
_z = None


def normal_maps():
    """Texture ids a material names as its normal map. Empty without the database."""
    global _normals
    if _normals is None:
        from . import common
        try:
            con = common.connect()
        except RuntimeError:
            return set()
        # version 0 holds the id, later ones more
        rows = con.execute("select coalesce(json_extract(data, '$.v0.normal'), "
                           "json_extract(data, '$.v1.normal.texture')) from config where type = 'materials'")
        _normals = {r[0] for r in rows if isinstance(r[0], int)}
        con.close()
    return _normals


def _ztable():
    # blue for alpha * 256 + green
    global _z
    if _z is None:
        _z = bytes(round((math.sqrt(max(0.0, 1 - (x / 127.5 - 1) ** 2 - (y / 127.5 - 1) ** 2)) + 1) * 127.5)
                   for x in range(256) for y in range(256))
    return _z


def _file(major, tex, root):
    files = cachefile.files(major, tex, root or cachefile.GAME)
    return files[min(files)] if files else None


def _crop(w, h, px):
    ow, oh = w - 2 * PAD, h - 2 * PAD
    if ow <= 0 or oh <= 0:
        return None
    return ow, oh, b"".join(px[((y * w) + PAD) * 4:((y * w) + PAD + ow) * 4] for y in range(PAD, PAD + oh))


def bmp(tex, root=None):
    """(width, height, rgba) of a texture's first mip level, or None when it is not on disk."""
    data = _file(BMP, tex, root)
    if not data or len(data) < 9 or data[5] or data[6]:
        return None
    w, h = struct.unpack_from(">II", data, 1)
    if len(data) < 9 + w * h * 4:
        return None
    return _crop(w, h, data[9:9 + w * h * 4])


def _expand(c):
    return ((c >> 11) * 255 + 15) // 31, (((c >> 5) & 63) * 255 + 31) // 63, ((c & 31) * 255 + 15) // 31


def dds(tex, root=None):
    """(width, height, rgba) from the dds copy, or None."""
    data = _file(DDS, tex, root)
    if not data or len(data) < 5 + 128:
        return None
    size = struct.unpack_from(">I", data, 1)[0]
    body = data[5:5 + size]
    if body[:4] != b"DDS ":
        return None
    h, w = struct.unpack_from("<II", body, 12)
    four = body[84:88]
    if four not in (b"DXT1", b"DXT5"):
        return None
    alpha = four == b"DXT5"
    z = _ztable() if alpha and tex in normal_maps() else None
    step = 16 if alpha else 8
    bw, bh = (w + 3) // 4, (h + 3) // 4
    if len(body) < 128 + bw * bh * step:
        return None
    px = bytearray(w * h * 4)
    at = 128
    for by in range(bh):
        for bx in range(bw):
            if alpha:
                a0, a1 = body[at], body[at + 1]
                bits = int.from_bytes(body[at + 2:at + 8], "little")
                if a0 > a1:
                    alphas = [a0, a1] + [((6 - i) * a0 + (1 + i) * a1) // 7 for i in range(6)]
                else:
                    alphas = [a0, a1] + [((4 - i) * a0 + (1 + i) * a1) // 5 for i in range(4)] + [0, 255]
                c = at + 8
            c0, c1 = struct.unpack_from("<HH", body, c if alpha else at)
            (r0, g0, b0), (r1, g1, b1) = _expand(c0), _expand(c1)
            cols = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
            if c0 > c1 or alpha:
                # the plus one is the DXT1 page's rounding
                cols += [((2 * r0 + r1 + 1) // 3, (2 * g0 + g1 + 1) // 3, (2 * b0 + b1 + 1) // 3, 255),
                         ((r0 + 2 * r1 + 1) // 3, (g0 + 2 * g1 + 1) // 3, (b0 + 2 * b1 + 1) // 3, 255)]
            else:
                cols += [((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255), (0, 0, 0, 0)]
            index = struct.unpack_from("<I", body, (c if alpha else at) + 4)[0]
            for p in range(16):
                x, y = bx * 4 + p % 4, by * 4 + p // 4
                if x >= w or y >= h:
                    continue
                r, g, b, a = cols[(index >> (2 * p)) & 3]
                if alpha:
                    a = alphas[(bits >> (3 * p)) & 7]
                    if z:
                        b = z[a * 256 + g]
                o =(y * w + x) * 4
                px[o:o + 4] = bytes((r, g, b, a))
            at += step
    return _crop(w, h, bytes(px))


def image(tex, root=None):
    """(width, height, rgba) from bmp when it is on disk, else from dds, else None."""
    return bmp(tex, root) or dds(tex, root)
