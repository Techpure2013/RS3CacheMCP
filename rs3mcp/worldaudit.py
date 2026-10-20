"""The sandbox world against the real one: what qd worldscan recorded (data/rec-world.jsonl) set
beside what the cache says.

    python -m rs3mcp.worldaudit [rec-world.jsonl]

Locs are matched on placed id and tile. For each match the shape, rotation and level are compared,
and the loc the client shows against the one the sandbox picks with the scan's own vars (every var
0 for a scan without them); where they differ, the var value that would show the client's is read
back out of the morph list. Steps say how the player really moved, tick by tick. Heights
fit the client's ground height at a loc against the sum of the stored heights under it, which is
what mapscene.HEIGHT should be. Routes are the client's own plans, walked again through
mapwalk from the same start. Nothing here is a guess about the game: each number is the client's.
"""

import collections
import json
import os
import sys

from . import cachefile, common, maplocs, mapnear, maptiles, mapwalk

REC = os.path.join(common.ROOT, "rec-world.jsonl")
# seconds between one scan's posts
SAME_SCAN = 120
SAMPLE = 12


def load(path=REC):
    """The latest scan: {"player", "locs": [dict], "npcs": [dict], "routes": [dict]}, or None."""
    if not os.path.exists(path):
        return None
    lines = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    locs = [l for l in lines if l.get("kind") == "locs"]
    if not locs:
        return None
    last = max(l["t"] for l in locs)
    scan = [l for l in lines if last - SAME_SCAN <= l["t"] <= last + 30 * 60]

    def rows(kind, window):
        out = []
        for l in scan:
            if l.get("kind") == kind and window(l):
                out += [dict(zip(l["columns"], r)) for r in l["rows"]]
        return out
    player = next(l["player"] for l in scan if l.get("kind") == "locs")
    near = lambda l: abs(l["t"] - last) <= SAME_SCAN
    return {"player": player, "locs": rows("locs", near), "npcs": rows("npcs", near),
            "routes": rows("routes", lambda l: True), "steps": rows("steps", lambda l: True),
            "vars": {r["id"]: r["value"] for r in rows("vars", near)}}


def var_reader(bases):
    """var(kind, cache id) for mapnear out of a scan's base var values: a varp is its base, a
    varbit its bits of one. None when the scan carried no vars, so callers fall back."""
    if not bases:
        return None
    import sqlite3
    # read whole: the reader outlives this thread
    con = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
    layout = {-vid - 1: (base, mask, shift) for vid, base, mask, shift
              in con.execute("select id, base_id, mask, shift from varbit where domain = 'VarPlayer'")}
    con.close()

    def var(kind, vid):
        if kind == "varp":
            return bases.get(vid, 0)
        got = layout.get(vid)
        if got is None:
            return 0
        base, mask, shift = got
        return (bases.get(base, 0) & mask) >> shift
    return var


def _placements(x0, z0, x1, z1, root=None):
    """{(loc id, x, z): [(level, shape, rotation)]} of the cache's placements over the tiles, the
    level after a bridge: under a plane 1 bridge tile everything sits one lower, as mapwalk has it."""
    out = collections.defaultdict(list)
    desc = cachefile.descriptor(maplocs.MAJOR, root or cachefile.GAME) or {}
    for sx in range(x0 // 64, x1 // 64 + 1):
        for sz in range(z0 // 64, z1 // 64 + 1):
            files = cachefile.files(maplocs.MAJOR, sx + sz * maplocs.STRIDE, root or cachefile.GAME, desc) or {}
            ground = maptiles.square(sx, sz, root)
            if maplocs.FILE not in files or ground is None:
                continue
            tiles = ground[0]
            for lid, uses in maplocs.decode(files[maplocs.FILE]):
                for p, x, z, shape, rot, _ in uses:
                    wx, wz = sx * 64 + x, sz * 64 + z
                    if x0 <= wx <= x1 and z0 <= wz <= z1:
                        bridged = p < 3 and tiles[1][x][z].get("settings", 0) & maptiles.BRIDGE
                        out[(lid, wx, wz)].append((p - 1 if bridged else p, shape, rot))
    return out


def audit_locs(locs, root=None, var=None):
    xs, zs = [r["x"] for r in locs], [r["z"] for r in locs]
    ours = _placements(min(xs), min(zs), max(xs), max(zs), root)
    objs = mapwalk._Objects()
    pick = var or (lambda kind, vid: 0)
    seen, report = set(), collections.Counter()
    samples = collections.defaultdict(list)
    implied = collections.defaultdict(collections.Counter)
    for r in locs:
        key = (r["parent"], r["x"], r["z"])
        uses = ours.get(key)
        if not uses:
            report["clientOnly"] += 1
            samples["clientOnly"].append(r)
            continue
        seen.add(key)
        report["matched"] += 1
        best = min(uses, key=lambda u: (u[1] != r["shape"], u[2] != r["rotation"]))
        if best[1] != r["shape"]:
            report["shapeDiffers"] += 1
            samples["shapeDiffers"].append({"client": r, "ours": best})
        elif best[2] != r["rotation"]:
            report["rotationDiffers"] += 1
            samples["rotationDiffers"].append({"client": r, "ours": best})
        if best[0] != r["level"]:
            report["levelDiffers"] += 1
            samples["levelDiffers"].append({"client": r, "ours": best})
        shown = mapnear.shown(objs, r["parent"], pick)
        # a morph showing nothing reports its own id
        if shown is None and r["id"] == r["parent"]:
            report["morphShowsNothing"] += 1
        elif shown != r["id"]:
            report["morphDiffers"] += 1
            m = objs.get(r["parent"])["morph"] or {}
            var = ("varbit", m.get("varbit")) if m.get("varbit") is not None else ("varp", m.get("varp"))
            if r["id"] in m.get("ids", []):
                implied[var][m["ids"].index(r["id"])] += 1
            elif r["id"] == m.get("atEnd"):
                implied[var]["at the end"] += 1
            elif r["id"] == m.get("pastEnd"):
                implied[var]["past the end"] += 1
            else:
                implied[var]["neither"] += 1
                samples["morphNeither"].append(r)
    # client lists active locs only
    return {"counts": dict(report), "impliedVars": {"%s %s" % k: dict(v) for k, v in implied.items()},
            "samples": {k: v[:SAMPLE] for k, v in samples.items()}}


def fit_heights(locs, root=None):
    """client ground y against the stored heights summed to its level: slope, offset, spread."""
    pairs = []
    squares = {}
    for r in locs:
        if r["shape"] not in (10, 22) or (r["width"], r["length"]) != (1, 1):
            continue
        sq = (r["x"] // 64, r["z"] // 64)
        if sq not in squares:
            got = maptiles.square(*sq, root)
            squares[sq] = got[0] if got else None
        tiles = squares[sq]
        if tiles is None:
            continue
        lx, lz = r["x"] % 64, r["z"] % 64
        stored = sum(tiles[p][lx][lz].get("height", 0) for p in range(r["level"] + 1))
        pairs.append((stored, r["y"]))
    if len(pairs) < 3:
        return {"pairs": len(pairs)}
    n = len(pairs)
    mx, my = sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pairs) / sxx if sxx else 0
    offset = my - slope * mx
    misses = sorted(abs(p[1] - (slope * p[0] + offset)) for p in pairs)
    return {"pairs": n, "slope": round(slope, 3), "offset": round(offset, 1),
            "medianMiss": misses[n // 2], "worstMiss": misses[-1]}


def audit_routes(routes, root=None):
    """Each client route walked again by mapwalk: steps each way, and where the ends differ.

    A client route lists a diagonal as two straight tiles, mostly, and sometimes as one, while the
    player really moves diagonally: running steps of one tile across and two along need one. So a
    route agrees when its end is ours and its length falls between our steps and those plus our
    diagonals."""
    out = []
    for r in routes:
        start, goal = (r["x"], r["z"]), (r["toX"], r["toZ"])
        grid = mapwalk.Grid(min(start[0], goal[0]) // 64 - 1, min(start[1], goal[1]) // 64 - 1,
                            max(start[0], goal[0]) // 64 + 1, max(start[1], goal[1]) // 64 + 1, r["level"], root)
        ours = grid.path(start, goal, nearest=True) or []
        client = [tuple(t[:2]) for t in r["route"]]
        steps, at = 0, start
        for t in client:
            steps += max(abs(t[0] - at[0]), abs(t[1] - at[1]))
            at = t
        # client diagonals are two tiles
        diagonals = sum(1 for a, b in zip(ours, ours[1:]) if a[0] != b[0] and a[1] != b[1])
        out.append({"from": list(start), "to": list(goal), "clientSteps": steps, "ourSteps": max(len(ours) - 1, 0),
                    "ourDiagonals": diagonals,
                    "clientEnd": list(client[-1]) if client else None, "ourEnd": list(ours[-1]) if ours else None})
    agree = sum(1 for o in out if o["ourSteps"] <= o["clientSteps"] <= o["ourSteps"] + o["ourDiagonals"]
                and o["clientEnd"] == o["ourEnd"])
    return {"routes": len(out), "agree": agree, "each": out}


def audit_steps(steps):
    """How the player really moved, one server tick at a time: straight, diagonal or further."""
    moves = collections.Counter()
    tiles = [(s["x"], s["z"]) for s in steps]
    for a, b in zip(tiles, tiles[1:]):
        d = (abs(b[0] - a[0]), abs(b[1] - a[1]))
        moves["diagonal" if d == (1, 1) else "straight" if max(d) == 1 else "two tiles" if max(d) == 2 else "further"] += 1
    return {"steps": len(steps), "moves": dict(moves)}


def audit(path=REC, root=None):
    scan = load(path)
    if scan is None:
        return {"error": "no scan in %s; run qd worldscan in the client" % path}
    var = var_reader(scan["vars"])
    return {"player": scan["player"], "vars": len(scan["vars"]),
            "locs": audit_locs(scan["locs"], root, var),
            "heights": fit_heights(scan["locs"], root), "npcs": len(scan["npcs"]),
            "npcNames": collections.Counter(n["name"] for n in scan["npcs"]).most_common(20),
            "routes": audit_routes(scan["routes"], root), "moves": audit_steps(scan["steps"])}


if __name__ == "__main__":
    print(json.dumps(audit(sys.argv[1] if len(sys.argv) > 1 else REC), indent=1, default=str))
