"""The Rust half of the map scene (native/mapcore), when it has been built.

    cargo build --release --manifest-path native/mapcore/Cargo.toml

lib() is None without the DLL; every caller keeps its Python way for that case, and the Python is
the reference each Rust function is checked against. RS3MCP_MAPCORE names another DLL to load.
"""

import ctypes
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DLL = os.environ.get("RS3MCP_MAPCORE") or os.path.join(_HERE, "..", "native", "mapcore", "target", "release",
                                                       "mapcore.dll")
NXT_SIDE, PLANES, NXT_FIELDS = 66, 4, 11
# tile fields, as in lib.rs
PRESENT, KEYS, FLAGS, HEIGHT, WATERHEIGHT, UNDERLAY, UNDERLAYCOLOR, OVERLAY, OVERLAY_UNDER, SHAPE, UNDERLAY_UNDER = range(11)
# KEYS bits by dict key
KEY_BITS = (("waterheight", 1, WATERHEIGHT), ("underlay", 2, UNDERLAY), ("underlaycolor", 4, UNDERLAYCOLOR),
            ("overlay", 8, OVERLAY), ("overlay_under", 16, OVERLAY_UNDER), ("shape", 32, SHAPE),
            ("underlay_under", 64, UNDERLAY_UNDER))
_ERRORS = {-1: "ran out", -2: "no jagx magic", -3: "plane past 3"}

_lib = False


def lib():
    """The loaded DLL with its argument types set, or None when it is not built."""
    global _lib
    if _lib is False:
        _lib = None
        if os.path.exists(DLL):
            d = ctypes.CDLL(os.path.abspath(DLL))
            d.mapcore_nxt_decode.argtypes = (ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32))
            d.mapcore_nxt_decode.restype = ctypes.c_int32
            d.mapcore_nxt_layout.argtypes = (ctypes.POINTER(ctypes.c_int32),) * 3
            d.mapcore_nxt_layout.restype = None
            doubles = ctypes.POINTER(ctypes.c_double)
            d.mapcore_ssim2_reference.argtypes = (ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, doubles,
                                                  doubles, doubles)
            d.mapcore_ssim2_reference.restype = ctypes.c_void_p
            d.mapcore_ssim2_features.argtypes = (ctypes.c_void_p, ctypes.c_char_p, doubles)
            d.mapcore_ssim2_features.restype = ctypes.c_int32
            d.mapcore_ssim2_free.argtypes = (ctypes.c_void_p,)
            d.mapcore_ssim2_free.restype = None
            floats, longs = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int64)
            d.mapcore_upsample.argtypes = (floats, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, floats)
            d.mapcore_upsample.restype = None
            d.mapcore_underlays.argtypes = (ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int64,
                                            ctypes.c_int64, longs, ctypes.POINTER(ctypes.c_void_p), floats,
                                            ctypes.c_size_t, floats, floats)
            d.mapcore_underlays.restype = None
            d.mapcore_overlay.argtypes = (floats, floats, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
                                          ctypes.c_int64, ctypes.c_int64, longs, floats, floats, ctypes.c_int64,
                                          ctypes.c_size_t, floats, floats)
            d.mapcore_overlay.restype = None
            d.mapcore_finish.argtypes = (floats,) * 5 + (ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t,
                                                         ctypes.c_size_t, ctypes.c_char_p, ctypes.c_char_p)
            d.mapcore_finish.restype = ctypes.c_int32
            side, planes, fields = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
            d.mapcore_nxt_layout(ctypes.byref(side), ctypes.byref(planes), ctypes.byref(fields))
            if (side.value, planes.value, fields.value) != (NXT_SIDE, PLANES, NXT_FIELDS):
                raise RuntimeError("mapcore.dll was built with another tile layout; rebuild it")
            _lib = d
    return _lib


def nxt_decode(data):
    """One square's file 5 as an int32 array (PLANES, NXT_SIDE, NXT_SIDE, NXT_FIELDS); a plane the
    file lacks has PRESENT 0. Raises maptiles.Stop as maptiles.decode_nxt does."""
    import numpy as np
    from .maptiles import Stop
    out = np.zeros((PLANES, NXT_SIDE, NXT_SIDE, NXT_FIELDS), np.int32)
    code = lib().mapcore_nxt_decode(bytes(data), len(data), out.ctypes.data_as(ctypes.POINTER(ctypes.c_int32)))
    if code:
        raise Stop(_ERRORS.get(code, "error %d" % code))
    return out


def upsample(mask, px):
    """Image.fromarray(mask, "F").resize((w * px, h * px), Image.BILINEAR) as a float32 array."""
    import numpy as np
    mask = np.ascontiguousarray(mask, np.float32)
    h, w = mask.shape
    out = np.empty((h * px, w * px), np.float32)
    lib().mapcore_upsample(mask.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), h, w, px,
                           out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
    return out


def underlays(h, w, parts, px, x0, z1):
    """mapground._underlays' blend over the h x w tiles whose west tile is x0 and north tile z1:
    parts (id, r0, r1, c0, c1, source, weight) in ascending id order, source as Floors.source gives
    it and weight the floor's (r1 - r0) x (c1 - c0) tile weights over its box. Returns the
    (h * px, w * px, 3) float32 image."""
    import numpy as np
    boxes, keep, rgb, weights = [], [], [], []
    for fid, r0, r1, c0, c1, src, weight in parts:
        weights.append(np.ascontiguousarray(weight, np.float32).ravel())
        if src[0] == "texture":
            tex = np.ascontiguousarray(src[1], np.float32)
            keep.append(tex)
            boxes.append((fid, r0, r1, c0, c1, 0 if src[2] is None else 1, tex.shape[0]))
            rgb.append(src[2] if src[2] is not None else (1.0, 1.0, 1.0))
        else:
            keep.append(None)
            boxes.append((fid, r0, r1, c0, c1, 2, 0))
            rgb.append(src[1])
    boxes = np.array(boxes, np.int64).reshape(-1, 7)
    rgb = np.array(rgb, np.float32).reshape(-1, 3)
    masks = np.concatenate(weights) if weights else np.zeros(1, np.float32)
    ptrs = (ctypes.c_void_p * max(len(keep), 1))(*[k.ctypes.data if k is not None else None for k in keep])
    img = np.empty((h * px, w * px, 3), np.float32)
    lib().mapcore_underlays(h, w, px, x0, z1, boxes.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), ptrs,
                            rgb.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(boxes),
                            masks.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                            img.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
    return img


def overlay(img, drawn, px, x0, z1, box, raw, soft, src):
    """One overlay painted as mapground._overlays paints it, img and drawn (both float32, contiguous)
    updated in place: box (r0, r1, c0, c1) in tiles, raw and soft its coverage over the box before and
    after softening, src as Floors.source gives it."""
    import numpy as np
    floats = ctypes.POINTER(ctypes.c_float)
    hp, wp = drawn.shape
    raw = np.ascontiguousarray(raw, np.float32)
    soft = raw if soft is raw else np.ascontiguousarray(soft, np.float32)
    bounds = np.array(box, np.int64)
    if src[0] == "texture":
        tex = np.ascontiguousarray(src[1], np.float32)
        kind, size = (0 if src[2] is None else 1), tex.shape[0]
        rgb = np.asarray(src[2] if src[2] is not None else (1.0, 1.0, 1.0), np.float32)
        tex_ptr = tex.ctypes.data_as(floats)
    else:
        kind, size, tex_ptr = 2, 0, None
        rgb = np.asarray(src[1], np.float32)
    lib().mapcore_overlay(img.ctypes.data_as(floats), drawn.ctypes.data_as(floats), hp // px, wp // px, px, x0, z1,
                          bounds.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), raw.ctypes.data_as(floats),
                          soft.ctypes.data_as(floats), kind, size, tex_ptr, rgb.ctypes.data_as(floats))


def finish(img, colour, has, drawn, holes, blurred, px):
    """mapground.texture's ending with water: (the ground RGBA image, the water RGBA image or None),
    blurred the sea mask after _smooth_edge's blur as uint8, None for no sea."""
    import numpy as np
    from PIL import Image
    floats = ctypes.POINTER(ctypes.c_float)
    hp, wp = drawn.shape
    args = [np.ascontiguousarray(a, np.float32) for a in (img, colour, has, drawn, holes)]
    sea = None if blurred is None else np.ascontiguousarray(blurred, np.uint8)
    out, water = np.empty((hp, wp, 4), np.uint8), np.empty((hp, wp, 4), np.uint8)
    wet = lib().mapcore_finish(*[a.ctypes.data_as(floats) for a in args],
                               None if sea is None else sea.ctypes.data_as(ctypes.c_char_p), hp // px, wp // px, px,
                               out.ctypes.data_as(ctypes.c_char_p), water.ctypes.data_as(ctypes.c_char_p))
    return Image.fromarray(out, "RGBA"), (Image.fromarray(water, "RGBA") if wet else None)


def nxt_dicts(arr):
    """maptiles.decode_nxt's [plane][x][z] dicts rebuilt from nxt_decode's array, for checking."""
    planes = []
    for p in range(PLANES):
        if not arr[p, 0, 0, PRESENT]:
            planes.append(None)
            continue
        rows = []
        for a in range(NXT_SIDE):
            row = []
            for b in range(NXT_SIDE):
                t = arr[p, a, b]
                d = {"flags": int(t[FLAGS]), "height": int(t[HEIGHT])}
                for name, bit, at in KEY_BITS:
                    if t[KEYS] & bit:
                        d[name] = int(t[at])
                row.append(d)
            rows.append(row)
        planes.append(rows)
    return planes
