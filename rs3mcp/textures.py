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
    import numpy as np
    blocks = np.frombuffer(body, np.uint8, bw * bh * step, 128).reshape(-1, step).astype(np.int64)
    cb = blocks[:, 8:] if alpha else blocks
    c0, c1 = cb[:, 0] | cb[:, 1] << 8, cb[:, 2] | cb[:, 3] << 8
    index = cb[:, 4] | cb[:, 5] << 8 | cb[:, 6] << 16 | cb[:, 7] << 24
    e0 = np.stack([((c0 >> 11) * 255 + 15) // 31, (((c0 >> 5) & 63) * 255 + 31) // 63, ((c0 & 31) * 255 + 15) // 31], 1)
    e1 = np.stack([((c1 >> 11) * 255 + 15) // 31, (((c1 >> 5) & 63) * 255 + 31) // 63, ((c1 & 31) * 255 + 15) // 31], 1)
    four = ((c0 > c1) | alpha)[:, None]
    # the plus one is the DXT1 page's rounding
    third = np.where(four, (2 * e0 + e1 + 1) // 3, (e0 + e1) // 2)
    last = np.where(four, (e0 + 2 * e1 + 1) // 3, 0)
    solid = np.full((len(blocks), 1), 255)
    palette = np.stack([np.hstack([e0, solid]), np.hstack([e1, solid]), np.hstack([third, solid]),
                        np.hstack([last, np.where(four, 255, 0)])], 1)
    shifts = np.arange(16)
    pick = (index[:, None] >> (2 * shifts)) & 3
    px = palette[np.arange(len(blocks))[:, None], pick]
    if alpha:
        a0, a1 = blocks[:, 0], blocks[:, 1]
        bits = sum(blocks[:, 2 + k] << (8 * k) for k in range(6))
        i6, i4 = np.arange(6), np.arange(4)
        seven = np.hstack([a0[:, None], a1[:, None], ((6 - i6) * a0[:, None] + (1 + i6) * a1[:, None]) // 7])
        five = np.hstack([a0[:, None], a1[:, None], ((4 - i4) * a0[:, None] + (1 + i4) * a1[:, None]) // 5,
                          np.zeros((len(blocks), 1), np.int64), np.full((len(blocks), 1), 255)])
        alphas = np.where((a0 > a1)[:, None], seven, five)
        px[:, :, 3] = alphas[np.arange(len(blocks))[:, None], (bits[:, None] >> (3 * shifts)) & 7]
        if z:
            px[:, :, 2] = np.frombuffer(z, np.uint8)[px[:, :, 3] * 256 + px[:, :, 1]]
    # 4 x 4 blocks into rows
    img =px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w]
    return _crop(w, h, img.astype(np.uint8).tobytes())


def image(tex, root=None):
    """(width, height, rgba) from bmp when it is on disk, else from dds, else None."""
    return cachefile.kept("texture", (tex, root), lambda: bmp(tex, root) or dds(tex, root))
