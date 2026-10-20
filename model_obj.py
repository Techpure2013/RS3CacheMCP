"""
Turn a model from the game cache into a wavefront obj.

The mesh comes from rs3mcp/models.py: positions, uvs, per-vertex colour, and triangles grouped by
material. model_png.py and the other model scripts load models through here too.

  python model_obj.py <model id> <out.obj>
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load(model):
    """A model id as verts, uvs, colours and tris. A tri is (a, b, c, part, material)."""
    from rs3mcp import models
    if not str(model).isdigit():
        raise SystemExit("give a model id, eg 95586")
    m = models.mesh(int(model))
    if m is None:
        raise SystemExit("model %s is not in the cache or does not decode" % model)
    return {"verts": m["verts"], "uvs": m["uvs"], "colours": m["colours"],
            "tris": [(a, b, c, 0, mat) for a, b, c, mat in m["tris"]]}


def write_obj(m, path):
    out = ["# from the rs3 cache", "# %d vertices, %d triangles" % (len(m["verts"]), len(m["tris"]))]
    for v in m["verts"]:
        out.append("v %g %g %g" % (v[0], v[1], v[2]))
    for t in m["uvs"]:
        out.append("vt %.6f %.6f" % (t[0], t[1]))
    last = None
    for a, b, c, _part, mat in m["tris"]:
        if mat != last:
            out.append("g material_%s" % mat)
            last = mat
        # obj counts from 1
        out.append("f %d %d %d" % (a + 1, b + 1, c + 1))
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__.strip().splitlines()[-1].strip())
    m = load(sys.argv[1])
    v, t = m["verts"], m["tris"]
    print("vertices             : %d" % len(v))
    print("uvs                  : %d" % len(m["uvs"]))
    print("triangles            : %d" % len(t))
    if v:
        xs = [p[0] for p in v]; ys = [p[1] for p in v]; zs = [p[2] for p in v]
        print("bounding box         : x %g..%g  y %g..%g  z %g..%g"
              % (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    bad = [f for f in t if max(f[:3]) >= len(v)]
    print("indices out of range : %d" % len(bad))
    write_obj(m, sys.argv[2])
    print("wrote %s" % sys.argv[2])


if __name__ == "__main__":
    main()
