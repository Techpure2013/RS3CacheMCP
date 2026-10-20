"""RS3 models, index 47: what each one is made of.

    python -m rs3mcp.models <id> [...]      one model's summary
    python -m rs3mcp.models verify [n]      decode every model, or the first n, and count failures

The layout covers current builds, including float positions, wrapped index counts and big
endian int indices. Scalars are big endian unless marked;
vertex buffers are little endian. A model has to decode to exactly its last byte, as the config specs do, so a field
read wrong is a failure rather than a strange number.

Version 3 and older keep vertices per mesh. Newer ones share one vertex block between meshes.

What comes back is a summary, not the mesh: vertices, triangles, meshes, materials and the
bounding box. Enough to size an npc or loc or find what uses a material, in a few megabytes.
"""

import array
import struct
import sys

from . import cachefile

MAJOR = 47

# TODO: a model viewer on top of decode(mesh=True). Needs materials,
# textures and animation layered on before it is worth it.


class Stop(Exception):
    pass


class Buf:
    def __init__(self, data):
        self.b = data
        self.i = 0

    def take(self, n):
        if self.i + n > len(self.b):
            raise Stop("ran out of bytes at %d wanting %d" % (self.i, n))
        s = self.b[self.i:self.i + n]
        self.i += n
        return s

    def u8(self):
        return self.take(1)[0]

    def u16le(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def u32le(self):
        return struct.unpack("<I", self.take(4))[0]

    def arr(self, code, n, order="little"):
        """n values of an array type code, as an array."""
        a = array.array(code)
        a.frombytes(self.take(n * a.itemsize))
        if sys.byteorder != order:
            a.byteswap()
        return a


def flags(f):
    return {"vertices": f & 1, "alpha": f >> 1 & 1, "faceBones": f >> 2 & 1,
            "boneIds": f >> 3 & 1, "hidden": f >> 4 & 1, "skin": f >> 5 & 1}


def decode(data, mesh=False):
    """(summary, problem). problem is None when the model read to exactly its end.

    With mesh, the summary also carries "parts": each a dict of positions (x, y, z flat), colours
    (packed hsl, one per vertex, or one per face in version 3 and older) and faces as
    (material, triangle indices)."""
    b = Buf(data)
    out = {"parts": []} if mesh else {}
    try:
        _decode(b, out)
    except Stop as e:
        return out, {"kind": "broken", "at": b.i, "why": str(e)}
    if b.i != len(data):
        return out, {"kind": "trailing", "at": b.i, "bytes": len(data) - b.i}
    return out, None


def _decode(b, out):
    out["format"], out["version"], _always_0f = b.u8(), b.u8(), b.u8()
    meshes = b.u8()
    counts = [b.u8() for _ in range(4)] + [b.u8() if out["version"] >= 5 else 0]
    positions, materials, triangles, vertices = [], set(), 0, 0
    skinned = bones = alpha = False
    parts = out.get("parts")
    if out["version"] <= 3:
        for _ in range(meshes):
            f = flags(b.u8())
            b.u32()
            material = b.u16le() - 1
            faces = b.u16le()
            colours = b.arr("H", faces) if f["vertices"] else None
            if f["alpha"]:
                b.take(faces)
            if f["faceBones"]:
                b.take(faces * 2)
            lods = b.u8()
            lod0 = None
            for k in range(lods):
                n = b.u16le()
                idx = b.arr("H", n)
                if k == 0:
                    triangles += n // 3
                    lod0 = idx
            n = b.u16le() if f["vertices"] else 0
            if f["vertices"]:
                positions.append(b.arr("h", n * 3))
                normals = b.take(n * 3)
                b.take(n * 4)       # tangents
                uvs = b.take(n * 4)
                if parts is not None:
                    parts.append({"positions": positions[-1], "colours": colours, "perFace": True,
                                  "faces": [(material, lod0 or array.array("H"))], "uvs": uvs,
                                  "normals": normals})
            if f["boneIds"]:
                b.take(n * 2)
            if f["skin"]:
                weights = b.u32le()
                b.take(weights * 3)
            vertices += n
            if material >= 0:
                materials.add(material)
            skinned, bones, alpha = skinned or f["skin"], bones or f["boneIds"], alpha or f["alpha"]
    else:
        f = flags(b.u8())
        # 1 where positions are big endian floats
        floats = b.u8() == 1
        b.u16le()                   # face count, which the index buffers say better
        n = b.u32le()
        vertices = n
        uvs = normals = b""
        if f["vertices"]:
            positions.append(b.arr("f", n * 3, "big") if floats else b.arr("h", n * 3))
            normals = b.take(n * 3)
            b.take(n * 4)           # tangents
            uvs = b.take(n * 4)
        if f["boneIds"]:
            b.take(n * 2)
        if f["skin"]:
            for _ in range(n):
                b.take(b.u16le() * 2)
                b.take(b.u16le())
        colours = None
        if f["vertices"]:
            colours = b.arr("H", n)
            b.take(n)               # alpha
        if f["faceBones"]:
            b.take(n * 2)
        got = _renders(b, meshes, n, sum(c * s for c, s in zip(counts[1:], (39, 50, 18, 0))))
        if got is None:
            raise Stop("no index counts fit the model's length")
        faces = []
        for rf, material, k, head in got:
            triangles += k // 3
            if material >= 0:
                materials.add(material)
            alpha = alpha or rf["alpha"]
            if parts is not None:
                width = 4 if n > 0xFFFF else 2
                faces.append((material, _indices(b.b[head:head + k * width], width)))
        if parts is not None and positions:
            parts.append({"positions": positions[0], "colours": colours, "perFace": False, "faces": faces,
                          "uvs": uvs, "normals": normals})
        skinned, bones = f["skin"], f["boneIds"]
    for count, size in zip(counts[1:], (39, 50, 18, 0)):
        b.take(count * size)
    out.update(meshes=meshes, vertices=vertices, triangles=triangles, materials=sorted(materials),
               skinned=bool(skinned), bones=bool(bones), alpha=bool(alpha))
    if positions and any(len(p) for p in positions):
        lo, hi = [], []
        for axis in range(3):
            vals = [min(p[axis::3]) for p in positions if len(p)], [max(p[axis::3]) for p in positions if len(p)]
            lo.append(round(min(vals[0])))
            hi.append(round(max(vals[1])))
        out["box"] = {"min": lo, "max": hi}


def _indices(raw, width):
    """An index buffer: little endian shorts, or big endian ints on a model too big for shorts."""
    idx = array.array("I" if width == 4 else "H")
    idx.frombytes(raw)
    if width == 4:
        idx.byteswap()
    return idx


def _renders(b, meshes, n, tail):
    """[(flags, material, index count, where the indices start)] for the meshes, leaving b at the
    tail, or None.

    The index count is sixteen bits and wraps on a mesh of 65,536 indices or more. When the stored
    counts do not end the model, the count that fits is searched for: the stored one plus some
    65,536s, every index naming a real vertex and the last mesh ending where the tail begins."""
    width = 4 if n > 0xFFFF else 2
    start = b.i
    # the stored counts come first, unchecked
    plain = []
    try:
        for _ in range(meshes):
            rf = flags(b.u8())
            b.u32()
            material = b.u16le() - 1
            b.u8()
            k = b.u16le()
            head = b.i
            b.take(k * width)
            plain.append((rf, material, k, head))
        if len(b.b) - b.i == tail:
            return plain
    except Stop:
        pass
    b.i = start

    def walk(left):
        if not left:
            return [] if len(b.b) - b.i == tail else None
        at = b.i
        rf = flags(b.u8())
        b.u32()
        material = b.u16le() - 1
        b.u8()
        stored = b.u16le()
        head = b.i
        for wraps in range(4):
            k = stored + 0x10000 * wraps
            if head + k * width > len(b.b) - tail:
                break
            idx = _indices(b.b[head:head + k * width], width)
            if k and max(idx) >= n:
                continue
            b.i = head + k * width
            rest = walk(left - 1)
            if rest is not None:
                return [(rf, material, k, head)] + rest
        b.i = at
        return None

    try:
        return walk(meshes)
    except Stop:
        b.i = start
        return None


def records(root=None, minors=None):
    """(model id, bytes) for every model, or the ones in the given groups."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    for minor in (sorted(desc) if minors is None else minors):
        files = cachefile.files(MAJOR, minor, root, desc)
        if files:
            yield minor, files[min(files)]


def mesh(mid, root=None):
    """One model with every part in one vertex list, or None when it does not decode:
    {"verts": [(x, y, z)], "tris": [(a, b, c, material)], "colours": packed hsl per vertex,
    "uvs": [(u, v)], "normals": [(x, y, z)] as signed bytes}. A part coloured per face gives each
    vertex the colour of its last face. Normals face as the triangles wind, counter-clockwise."""
    root = root or cachefile.GAME
    files = cachefile.files(MAJOR, mid, root) or {}
    if not files:
        return None
    out, problem = decode(files[min(files)], mesh=True)
    if problem:
        return None
    verts, tris, colours, uvs, normals = [], [], [], [], []
    for part in out["parts"]:
        base = len(verts)
        p = part["positions"]
        n = len(p) // 3
        verts.extend((p[i], p[i + 1], p[i + 2]) for i in range(0, n * 3, 3))
        # signed bytes, about 126 long
        raw = array.array("b", part.get("normals") or b"")
        normals.extend((raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]) if (i + 1) * 3 <= len(raw) else (0, 127, 0)
                       for i in range(n))
        raw = part.get("uvs") or b""
        # half floats, big endian, u then v
        uvs.extend(struct.unpack_from(">ee", raw, i * 4) if (i + 1) * 4 <= len(raw) else (0.0, 0.0)
                   for i in range(n))
        cols = part["colours"]
        mine = [0] * n
        if cols is not None and not part["perFace"]:
            mine = list(cols[:n]) + [0] * max(0, n - len(cols))
        for material, idx in part["faces"]:
            for k, i in enumerate(range(0, len(idx) - 2, 3)):
                a, b, c = idx[i], idx[i + 1], idx[i + 2]
                tris.append((base + a, base + b, base + c, material))
                if cols is not None and part["perFace"] and k < len(cols):
                    for v in (a, b, c):
                        if v < n:
                            mine[v] = cols[k]
        colours.extend(mine)
    return {"verts": verts, "tris": tris, "colours": colours, "uvs": uvs, "normals": normals}


def verify(limit=None):
    import collections
    import time
    started = time.time()
    total, failed, why = 0, [], collections.Counter()
    for mid, data in records():
        total += 1
        _, problem = decode(data)
        if problem:
            failed.append(mid)
            why[problem["kind"] + ": " + problem.get("why", "")[:40]] += 1
        if limit and total >= limit:
            break
    print("%d models in %.1fs, %d did not decode" % (total, time.time() - started, len(failed)))
    for k, n in why.most_common(8):
        print("  %6d  %s" % (n, k))
    print("  e.g.", failed[:10])


if __name__ == "__main__":
    if sys.argv[1:2] == ["verify"]:
        verify(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif sys.argv[1:]:
        desc = cachefile.descriptor(MAJOR)
        for a in sys.argv[1:]:
            files = cachefile.files(MAJOR, int(a), cachefile.GAME, desc) or {}
            print(a, decode(files[min(files)]) if files else "no such model")
    else:
        raise SystemExit(__doc__)
