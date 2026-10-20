"""Where every loc stands: index 5, one group per mapsquare, file 0.

    python -m rs3mcp.maplocs <x> <z>      one mapsquare's placements
    python -m rs3mcp.maplocs verify       every mapsquare, decoded to its last byte

A run of loc id deltas, each followed by a run of placement deltas, both ending on 0. A placement
packs plane, x and y into one number, then a byte of rotation and type, and when that byte's top bit
is set, a flags byte and the extra rotation, offset and scale it names.
"""

import struct
import sys

from . import cachefile

MAJOR = 5
FILE = 0
STRIDE = 128


class Stop(Exception):
    pass


def _varu16(b, i):
    if i >= len(b):
        raise Stop("ran out at %d" % i)
    if b[i] & 0x80:
        if i + 2 > len(b):
            raise Stop("ran out at %d" % i)
        return ((b[i] & 0x7F) << 8) | b[i + 1], i + 2
    return b[i], i + 1


def _tailed(b, i):
    total = 0
    while True:
        v, i = _varu16(b, i)
        total += v
        if v != 0x7FFF:
            return total, i


# the extras a placement's flags byte names
EXTRA = [(1, "rotation", ">4h"), (2, "translateX", ">h"), (4, "translateY", ">h"), (8, "translateZ", ">h"),
         (16, "scale", ">H"), (32, "scaleX", ">H"), (64, "scaleY", ">H"), (128, "scaleZ", ">H")]


def decode(data):
    """[(loc id, [(plane, x, y, type, rotation, extra or None)])] for one mapsquare."""
    out, i, lid = [], 0, -1
    while True:
        delta, i = _tailed(data, i)
        if delta == 0:
            break
        lid += delta
        uses, at = [], 0
        while True:
            d, i = _varu16(data, i)
            if d == 0:
                break
            at += d - 1
            if i >= len(data):
                raise Stop("placement with no data byte")
            info = data[i]
            i += 1
            extra = None
            if info & 0x80:
                flags = data[i]
                i += 1
                extra = {}
                for bit, name, fmt in EXTRA:
                    if flags & bit:
                        size = struct.calcsize(fmt)
                        if i + size > len(data):
                            raise Stop("extras run past the end")
                        got = struct.unpack_from(fmt, data, i)
                        extra[name] = list(got) if len(got) > 1 else got[0]
                        i += size
            uses.append(((at >> 12) & 3, (at >> 6) & 63, at & 63, (info >> 2) & 31, info & 3, extra))
        out.append((lid, uses))
    if i != len(data):
        raise Stop("%d bytes left over" % (len(data) - i))
    return out


def squares(root=None):
    """(x, z, bytes) for every mapsquare that has placements."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root) or {}
    for g in sorted(desc):
        if FILE not in desc[g]["files"]:
            continue
        files = cachefile.files(MAJOR, g, root, desc) or {}
        if FILE in files:
            yield g % STRIDE, g // STRIDE, files[FILE]


def verify(root=None):
    good = bad = 0
    first = None
    for x, z, data in squares(root):
        try:
            decode(data)
            good += 1
        except (Stop, IndexError, struct.error) as e:
            bad += 1
            first = first or (x, z, str(e))
    return {"squares": good + bad, "decoded": good, "firstProblem": first}


if __name__ == "__main__":
    if sys.argv[1:2] == ["verify"]:
        print(verify())
    elif len(sys.argv) == 3:
        desc = cachefile.descriptor(MAJOR) or {}
        g = int(sys.argv[1]) + int(sys.argv[2]) * STRIDE
        files = cachefile.files(MAJOR, g, cachefile.GAME, desc) or {}
        for lid, uses in decode(files.get(FILE, b"")):
            print(lid, uses[:4], "..." if len(uses) > 4 else "")
    else:
        raise SystemExit(__doc__)
