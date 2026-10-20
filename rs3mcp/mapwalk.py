"""Where a player can walk: the ground's blocked tiles, and the walls and objects placed on it.

    python -m rs3mcp.mapwalk path <x> <z> <x> <z> [plane] [--doors]   world tiles, shortest walk
    python -m rs3mcp.mapwalk png <x0> <z0> <x1> <z1> <out.png> [plane] mapsquares, walls drawn

The classic client's rules. An object blocks by its type: 0 when passable, 1 when it carries unk1B
(opcode 27, which blocks walking but not projectiles), else 2. Walls, loc shapes 0 to 3, block the
edges they stand on; diagonal walls (9) and objects (10, 11) block their footprint, turned with
their rotation; floor decoration (22) blocks only at type 1. A plane 1 tile with the bridge flag
carries its tiles and locs down to plane 0. Doors are walls whose first option is Open (closed) or
Close (open), kept in doors and open_doors. A path's doors are the cache's (open_doors), every one
(True), or a set of open door tiles, a world's own. An open door's walls all pass: the leaf it swings
to is not placed. Every loc on the plane is kept in locs, for what is around a tile.

A step is to any of the 8 neighbours; a diagonal needs both the straight steps beside it clear.
The search tries west, east, south, north, then the diagonals, as the classic client does: of the
equally short walks it keeps the straight one, which is the one a live client plans.
Walking covers a tile a server tick and running two, as qd tickprobe measured it.
"""

import collections
import json
import sqlite3
import sys
import threading

from . import cachefile, common, maplocs, maptiles

SIDE = maptiles.SIDE
# wall edge bits per tile
N, E, S, W = 1, 2, 4, 8
FULL = 16
DIRS = [(0, 1, N, S), (1, 0, E, W), (0, -1, S, N), (-1, 0, W, E)]
# straight wall side by rotation
WALL_SIDE = [W, N, E, S]
# L corner sides by rotation
CORNER_SIDES = [(W, N), (N, E), (E, S), (S, W)]
# straight steps first
STEPS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)]


class Grid:
    """Collision for a block of mapsquares on one plane, in world tiles."""

    def __init__(self, x0, z0, x1, z1, plane=0, root=None):
        self.plane = plane
        self.bx, self.bz = x0 * SIDE, z0 * SIDE
        self.w, self.h = (x1 - x0 + 1) * SIDE, (z1 - z0 + 1) * SIDE
        self.flags = bytearray(self.w * self.h)
        self.doors = {}
        self.open_doors = set()
        self.locs = []
        self.unloaded = []
        self.objects = objs = _Objects()
        for sx in range(x0, x1 + 1):
            for sz in range(z0, z1 + 1):
                self._square(sx, sz, objs, root)

    def at(self, x, z):
        lx, lz = x - self.bx, z - self.bz
        if 0 <= lx < self.w and 0 <= lz < self.h:
            return lx + lz * self.w
        return None

    def _mark(self, x, z, bits):
        i = self.at(x, z)
        if i is not None:
            self.flags[i] |= bits

    def _square(self, sx, sz, objs, root):
        ground = maptiles.square(sx, sz, root)
        if ground is None:
            self.unloaded.append((sx, sz))
            return
        tiles = ground[0]
        ox, oz = sx * SIDE, sz * SIDE

        # bridged tiles walk one plane down
        def real_plane(p, x, z):
            if p < 3 and tiles[1][x][z].get("settings", 0) & maptiles.BRIDGE:
                return p - 1
            return p

        for p in range(4):
            for x in range(SIDE):
                for z in range(SIDE):
                    if real_plane(p, x, z) == self.plane and tiles[p][x][z].get("settings", 0) & maptiles.BLOCKED:
                        self._mark(ox + x, oz + z, FULL)
        desc = cachefile.descriptor(maplocs.MAJOR, root or cachefile.GAME) or {}
        files = cachefile.files(maplocs.MAJOR, sx + sz * maplocs.STRIDE, root or cachefile.GAME, desc) or {}
        if maplocs.FILE not in files:
            return
        for lid, uses in maplocs.decode(files[maplocs.FILE]):
            o = objs.get(lid)
            for p, x, z, shape, rot, _extra in uses:
                if real_plane(p, x, z) != self.plane:
                    continue
                self.locs.append((ox + x, oz + z, lid, shape, rot))
                if o["kind"]:
                    self._place(ox + x, oz + z, shape, rot, o, lid)

    def _place(self, x, z, shape, rot, o, lid):
        if shape in (0, 2) and o["door"]:
            self.doors[(x, z)] = lid
            if o["open"]:
                self.open_doors.add((x, z))
        if shape == 0:
            self._wall(x, z, WALL_SIDE[rot])
        elif shape == 2:
            for side in CORNER_SIDES[rot]:
                self._wall(x, z, side)
        elif shape in (1, 3):
            # corner pillars: the diagonal rule covers them
            return
        elif shape in (9, 10, 11):
            w, l = (o["width"], o["length"]) if rot in (0, 2) else (o["length"], o["width"])
            for dx in range(w):
                for dz in range(l):
                    self._mark(x + dx, z + dz, FULL)
        elif shape == 22 and o["kind"] == 1:
            self._mark(x, z, FULL)

    def _wall(self, x, z, side):
        for dx, dz, bit, back in DIRS:
            if bit == side:
                self._mark(x, z, bit)
                self._mark(x + dx, z + dz, back)

    def blocked(self, x, z):
        i = self.at(x, z)
        return i is None or self.flags[i] & FULL

    def door_open(self, tile, doors=None):
        """Whether the door on tile lets a path through, doors as path takes them."""
        if tile not in self.doors:
            return False
        if doors is True:
            return True
        return tile in (self.open_doors if doors in (None, False) else doors)

    def can_step(self, x, z, dx, dz, doors=None):
        """One step from x, z by dx, dz, each -1, 0 or 1."""
        if dx and dz:
            return (self.can_step(x, z, dx, 0, doors) and self.can_step(x + dx, z, 0, dz, doors)
                    and self.can_step(x, z, 0, dz, doors) and self.can_step(x, z + dz, dx, 0, doors))
        if self.blocked(x + dx, z + dz):
            return False
        for ddx, ddz, bit, back in DIRS:
            if (ddx, ddz) == (dx, dz):
                wall = self.flags[self.at(x, z)] & bit
                if wall and not (self.door_open((x, z), doors) or self.door_open((x + dx, z + dz), doors)):
                    return False
        return True

    def path(self, start, goal, doors=None, limit=200000, nearest=False):
        """Tiles from start to goal inclusive, or None when goal cannot be reached.

        nearest walks to the reachable tile closest to goal instead, as a click on a blocked tile
        does in the client."""
        if self.blocked(*start):
            return None
        if nearest:
            goal = self.closest(start, goal, doors, limit)
        if goal is None or self.blocked(*goal):
            return None
        came = {start: None}
        todo = collections.deque([start])
        while todo and len(came) < limit:
            cur = todo.popleft()
            if cur == goal:
                break
            x, z = cur
            for dx, dz in STEPS:
                nxt = (x + dx, z + dz)
                if nxt not in came and self.can_step(x, z, dx, dz, doors):
                    came[nxt] = cur
                    todo.append(nxt)
        if goal not in came:
            return None
        return self._trace(came, goal)

    def closest(self, start, goal, doors=None, limit=200000):
        """The reachable tile nearest goal, straight-line distance, nearer the start on a tie."""
        came = {start: None}
        todo = collections.deque([start])
        best, best_d = start, None
        while todo and len(came) < limit:
            x, z = todo.popleft()
            d = max(abs(x - goal[0]), abs(z - goal[1]))
            if best_d is None or d < best_d:
                best, best_d = (x, z), d
                if d == 0:
                    break
            for dx, dz in STEPS:
                nxt = (x + dx, z + dz)
                if nxt not in came and self.can_step(x, z, dx, dz, doors):
                    came[nxt] = (x, z)
                    todo.append(nxt)
        return best

    @staticmethod
    def _trace(came, goal):
        out, cur = [], goal
        while cur is not None:
            out.append(cur)
            cur = came[cur]
        return out[::-1]


class _Objects:
    """What an object needs for collision and for naming, off the database, fetched as met.

    morph is the varbit (high 16 bits) or varp (low 16) choosing which loc shows, all ones for unused,
    with the loc for a value at the list's end and past it (see mapnear.shown)."""

    def __init__(self):
        # grids outlive the request thread that built them
        self.local = threading.local()
        self.seen = {}

    def db(self):
        if not hasattr(self.local, "db"):
            self.local.db = sqlite3.connect("file:%s?mode=ro" % common.DB.replace("\\", "/"), uri=True)
        return self.local.db

    def get(self, lid):
        if lid not in self.seen:
            row = self.db().execute("select data from config where type='objects' and id=?", (lid,)).fetchone()
            d = json.loads(row[0]) if row else {}
            kind = 0 if d.get("passable") else (1 if d.get("unk1B") else 2)
            # Open when closed, Close when open
            op = (d.get("option1") or "").lower()
            morph = d.get("morphs") or d.get("morphsWide")
            if morph:
                bit, varp = morph["var"] >> 16, morph["var"] & 0xFFFF
                # the wide form's trailing field is its end
                wide = "morphsWide" in d
                morph = {"varbit": None if bit == 0xFFFF else bit, "varp": None if varp == 0xFFFF else varp,
                         "ids": morph.get("ids") or [],
                         "atEnd": morph.get("unk") if wide else morph.get("fallback"),
                         "pastEnd": morph.get("fallback") if wide else None}
            self.seen[lid] = {"width": d.get("width", 1), "length": d.get("length", 1), "kind": kind,
                              "door": op in ("open", "close"), "open": op == "close", "name": d.get("name"),
                              "options": [d.get("option%d" % i) for i in range(1, 6)], "morph": morph}
        return self.seen[lid]


def walk(start, goal, plane=0, doors=False, margin=1, root=None):
    """The shortest walk between two world tiles, over the mapsquares around them."""
    sx0, sx1 = sorted((start[0] // SIDE, goal[0] // SIDE))
    sz0, sz1 = sorted((start[1] // SIDE, goal[1] // SIDE))
    grid = Grid(sx0 - margin, sz0 - margin, sx1 + margin, sz1 + margin, plane, root)
    tiles = grid.path(tuple(start), tuple(goal), doors)
    if tiles is None:
        return {"found": False, "start": list(start), "goal": list(goal),
                "startBlocked": bool(grid.blocked(*start)), "goalBlocked": bool(grid.blocked(*goal))}
    steps = len(tiles) - 1
    through = [list(t) for t in tiles if t in grid.doors]
    return {"found": True, "steps": steps, "walkTicks": steps, "runTicks": (steps + 1) // 2,
            "doors": through, "tiles": [list(t) for t in tiles]}


def png(x0, z0, x1, z1, out, plane=0, scale=6, root=None):
    """The ground map with blocked tiles dark and walls drawn on their edges."""
    from PIL import Image, ImageDraw
    base = maptiles.png(x0, z0, x1, z1, out, scale=scale, root=root)
    grid = Grid(x0, z0, x1, z1, plane, root)
    img = Image.open(base).convert("RGB")
    draw = ImageDraw.Draw(img)
    for lz in range(grid.h):
        for lx in range(grid.w):
            f = grid.flags[lx + lz * grid.w]
            if not f:
                continue
            px, py = lx * scale, (grid.h - 1 - lz) * scale
            if f & FULL:
                draw.rectangle([px + 1, py + 1, px + scale - 2, py + scale - 2], outline=(200, 40, 40))
            door = (grid.bx + lx, grid.bz + lz) in grid.doors
            colour = (80, 200, 255) if door else (255, 255, 255)
            if f & N:
                draw.line([px, py, px + scale - 1, py], fill=colour)
            if f & S:
                draw.line([px, py + scale - 1, px + scale - 1, py + scale - 1], fill=colour)
            if f & W:
                draw.line([px, py, px, py + scale - 1], fill=colour)
            if f & E:
                draw.line([px + scale - 1, py, px + scale - 1, py + scale - 1], fill=colour)
    img.save(out)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args[:1] == ["path"]:
        nums = list(map(int, args[1:]))
        got = walk(nums[0:2], nums[2:4], nums[4] if len(nums) > 4 else 0, "--doors" in sys.argv)
        got.pop("tiles", None)
        print(json.dumps(got))
    elif args[:1] == ["png"]:
        print(png(*map(int, args[1:5]), out=args[5], plane=int(args[6]) if len(args) > 6 else 0))
    else:
        raise SystemExit(__doc__)
