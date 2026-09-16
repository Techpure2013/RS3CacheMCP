"""
Turn a model into a wavefront obj: a model id, read out of the game cache, or an rsmv model json.

rsmv writes the vertex data as strings shaped `buffer <type>[<stride>][]{a,b,c,...}` rather than
json arrays, so it needs unpacking before it is any use. Everything needed for a renderable mesh
is in there: positions, normals, uvs, per-vertex colour, and one index buffer per submesh in
`meshdata.renders[].buf`, three indices to a triangle.

  python model_obj.py <models-N.json or model id> <out.obj>
"""

import io
import json
import re
import sys

BUF = re.compile(r"^buffer (\w+)\[(\d*)\](?:\[\])?\{(.*)\}$", re.S)


def unpack(s):
    """-> (stride, [values]). Stride is 1 when the buffer is a flat list."""
    if not isinstance(s, str):
        return 1, []
    m = BUF.match(s.strip())
    if not m:
        return 1, []
    stride = int(m.group(2)) if m.group(2) else 1
    body = m.group(3).strip()
    if not body:
        return stride, []
    return stride, [int(x) for x in body.split(",")]


def groups(vals, stride):
    return [tuple(vals[i:i + stride]) for i in range(0, len(vals) - stride + 1, stride)]


def half(v):
    """A uv is a half float stored byte-swapped, not a 0..65535 fraction. Read raw it looks like
    huge nonsense; swapped and decoded every model lands in 0..1, which is the tell that it is
    right. 60 is 0x003C, which swaps to 0x3C00, which is exactly 1.0."""
    v = ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)
    sign = -1.0 if (v & 0x8000) else 1.0
    exponent = (v & 0x7C00) >> 10
    mantissa = v & 0x03FF
    res = mantissa * 2.0 ** -10 + (0.0 if exponent == 0 else 1.0)
    return sign * res * 2.0 ** (exponent - 15)


def load(path):
    """A model as verts, norms, uvs, colours and tris. path is an rsmv model json, or a model id,
    which is read from the game cache instead (no normals then)."""
    if str(path).isdigit():
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from rs3mcp import models
        m = models.mesh(int(path))
        if m is None:
            raise SystemExit("model %s is not in the cache or does not decode" % path)
        # the same shape as below: rsmv's material argument is the material plus one
        return {"declared_vertexCount": len(m["verts"]), "verts": m["verts"], "norms": [],
                "uvs": m["uvs"], "colours": m["colours"],
                "tris": [(a, b, c, 0, mat + 1) for a, b, c, mat in m["tris"]], "submeshes": 1}
    d = json.load(io.open(path, encoding="utf-8"))
    md = d.get("meshdata") or {}
    ps, pv = unpack(md.get("positionBuffer"))
    ns, nv = unpack(md.get("normalBuffer"))
    us, uv = unpack(md.get("uvBuffer"))
    cs, cv = unpack(md.get("vertexColours"))
    verts = groups(pv, ps or 3)
    norms = groups(nv, ns or 3)
    uvs = [(half(a), half(b)) for a, b in groups(uv, us or 2)]
    tris = []
    for i, r in enumerate(md.get("renders") or []):
        _, idx = unpack(r.get("buf"))
        for j in range(0, len(idx) - 2, 3):
            tris.append((idx[j], idx[j + 1], idx[j + 2], i, r.get("materialArgument")))
    return {"declared_vertexCount": md.get("vertexCount"), "verts": verts, "norms": norms,
            "uvs": uvs, "colours": cv, "tris": tris,
            "submeshes": len(md.get("renders") or [])}


def write_obj(m, path):
    out = ["# from the rs3 cache via rsmv", "# %d vertices, %d triangles, %d submeshes"
           % (len(m["verts"]), len(m["tris"]), m["submeshes"])]
    for v in m["verts"]:
        out.append("v %d %d %d" % (v[0], v[1], v[2]))
    for n in m["norms"]:
        out.append("vn %d %d %d" % (n[0], n[1], n[2]))
    for t in m["uvs"]:
        # rs stores uv as ushort, obj wants floats
        out.append("vt %.6f %.6f" % (t[0] / 65535.0, t[1] / 65535.0))
    last = None
    for a, b, c, sub, mat in m["tris"]:
        if sub != last:
            out.append("g submesh_%d_material_%s" % (sub, mat))
            last = sub
        # obj is 1-indexed
        out.append("f %d %d %d" % (a + 1, b + 1, c + 1))
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")


def main():
    src, dst = sys.argv[1], sys.argv[2]
    m = load(src)
    v, t = m["verts"], m["tris"]
    print("declared vertexCount : %s" % m["declared_vertexCount"])
    print("positions parsed     : %d" % len(v))
    print("normals / uvs        : %d / %d" % (len(m["norms"]), len(m["uvs"])))
    print("triangles            : %d across %d submeshes" % (len(t), m["submeshes"]))
    if v:
        xs = [p[0] for p in v]; ys = [p[1] for p in v]; zs = [p[2] for p in v]
        print("bounding box         : x %d..%d  y %d..%d  z %d..%d"
              % (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    bad = [f for f in t if max(f[:3]) >= len(v)]
    print("indices out of range : %d" % len(bad))
    write_obj(m, dst)
    print("wrote %s" % dst)


if __name__ == "__main__":
    main()
