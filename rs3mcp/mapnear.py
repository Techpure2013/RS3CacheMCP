"""What is around a tile: the locs near it, named, with their options and a door's state.

    python -m rs3mcp.mapnear <x> <z> [level] [radius]

A loc that morphs shows ids[value]; a value at the list's length shows its trailing field, and one
past it nothing, or in the wide form its other field (measured on a live scan). NONE shows nothing. Without a world's vars every var reads 0, as an unset var does in
the client. Scenery with no name and no options is left out unless every is asked for. A door's options follow its state, so
a door a world opened offers Close.
"""

import json
import sys

from . import mapwalk

# morph slot showing nothing
NONE = 32767
# loc shapes by what they are
KINDS = [(3, "wall"), (8, "wall decoration"), (9, "diagonal wall"), (11, "object"), (21, "roof"), (22, "floor decoration")]


def kind_of(shape):
    return next(name for top, name in KINDS if shape <= top)


def shown(objects, lid, var):
    """The loc showing in lid's place, or None when its var picks nothing."""
    m = objects.get(lid)["morph"]
    if not m:
        return lid
    value = var("varbit", m["varbit"]) if m["varbit"] is not None else var("varp", m["varp"])
    n = len(m["ids"])
    pick = m["ids"][value] if 0 <= value < n else m["atEnd"] if value == n else m["pastEnd"]
    return None if pick in (None, NONE) else pick


def size(o, shape, rot):
    if shape not in (9, 10, 11):
        return 1, 1
    return (o["width"], o["length"]) if rot in (0, 2) else (o["length"], o["width"])


def around(grid, x, z, radius=10, doors=None, var=None, every=False):
    """The locs within radius tiles of x, z on grid's plane, nearest first; doors as Grid.path takes them."""
    var = var or (lambda kind, vid: 0)
    out = []
    for lx, lz, lid, shape, rot in grid.locs:
        if max(abs(lx - x), abs(lz - z)) > radius + 8:
            continue
        sid = shown(grid.objects, lid, var)
        if sid is None:
            continue
        o = grid.objects.get(sid)
        options = [op for op in o["options"] if op]
        if not every and not o["name"] and not options:
            continue
        w, l = size(o, shape, rot)
        # to the nearest tile of the footprint
        dx = max(lx - x, 0, x - (lx + w - 1))
        dz = max(lz - z, 0, z - (lz + l - 1))
        distance = max(dx, dz)
        if distance > radius:
            continue
        row = {"name": o["name"], "id": sid, "x": lx, "z": lz, "level": grid.plane, "kind": kind_of(shape),
               "options": options, "distance": distance}
        if sid != lid:
            row["morphedFrom"] = lid
        if (w, l) != (1, 1):
            row["size"] = [w, l]
        if grid.doors.get((lx, lz)) == lid:
            now = grid.door_open((lx, lz), doors)
            row["door"] = "open" if now else "closed"
            if now != ((lx, lz) in grid.open_doors):
                swap = {"open": "Close", "close": "Open"}
                row["options"] = [swap.get(op.lower(), op) for op in options]
        out.append(row)
    out.sort(key=lambda r: (r["distance"], r["name"] or ""))
    return out


if __name__ == "__main__":
    nums = list(map(int, sys.argv[1:]))
    if len(nums) < 2:
        raise SystemExit(__doc__)
    x, z = nums[0], nums[1]
    level = nums[2] if len(nums) > 2 else 0
    radius = nums[3] if len(nums) > 3 else 10
    g = mapwalk.Grid(x // 64 - 1, z // 64 - 1, x // 64 + 1, z // 64 + 1, level)
    print(json.dumps(around(g, x, z, radius), indent=1))
