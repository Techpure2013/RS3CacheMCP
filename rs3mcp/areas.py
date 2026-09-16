"""Which area every loc belongs to, as the loc_section table.

Three tiers, in order:

  1. the named mapzone a loc stands in, the smallest box when zones nest
  2. SURFACE_<bx>_<bz> for open world outside any zone, in blocks four mapsquares a side
  3. the config name prefix for a loc never placed on the map: quest interiors, instances
"""

import collections
import json
import re

from . import maplocs

SURFACE_BLOCK = 256
# the whole overworld in 34 boxes, which would swallow every loc outside a town. It is the only
# catch-all zone, and its boxes are smaller than some real areas, so it goes by name.
CATCHALL = {"RuneScape Surface"}


def zone_boxes(zones):
    """(area, name, x0, z0, x1, z1) for mapzone records, as json text or dicts."""
    out = []
    for data in zones:
        d = json.loads(data) if isinstance(data, str) else data
        nm = d.get("name") or d.get("internalName")
        if not nm or nm in ("Loading...", "null") or nm in CATCHALL:
            continue
        for b in (d.get("bounds") or []):
            w = b["toX"] - b["fromX"] + 1
            h = b["toZ"] - b["fromZ"] + 1
            if w > 0 and h > 0:
                out.append((w * h, nm, b["fromX"], b["fromZ"], b["toX"], b["toZ"]))
    return out


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "MISC"


def sections(zones, objects, squares=None):
    """[(loc id, section, placed)] from mapzone records, (id, name) of every loc, and the map."""
    boxes = zone_boxes(zones)
    grid = collections.defaultdict(list)
    for box in boxes:
        _, _, x0, z0, x1, z1 = box
        for msx in range(x0 // 64, x1 // 64 + 1):
            for msz in range(z0 // 64, z1 // 64 + 1):
                grid[(msx, msz)].append(box)
    for k in grid:
        grid[k].sort()

    best = {}
    for msx, msz, data in (squares if squares is not None else maplocs.squares()):
        near = grid.get((msx, msz)) or []
        for lid, uses in maplocs.decode(data):
            if not uses:
                continue
            _plane, x, y = uses[0][:3]
            ax, az = msx * 64 + x, msz * 64 + y
            pick = next(((area, slug(nm)) for area, nm, x0, z0, x1, z1 in near
                         if x0 <= ax <= x1 and z0 <= az <= z1), None)
            if pick is None:
                pick = (1 << 40, "SURFACE_%d_%d" % (ax // SURFACE_BLOCK, az // SURFACE_BLOCK))
            if lid not in best or pick[0] < best[lid][0]:
                best[lid] = pick

    rows = [(lid, sec, 1) for lid, (_, sec) in best.items()]
    rows += [(lid, (name.split("_", 1)[0] or "MISC").upper(), 0)
             for lid, name in objects if name and lid not in best]
    return rows
