"""Read the game cache. No rsmv.

Each index is a sqlite db, js5-<major>.jcache. Two tables: `cache` holds a group per row,
`cache_index` describes them. Payload is `ZLB` 01, uncompressed length, then zlib.

Traps:
  the index descriptor is column major, not row major
  a group's file boundaries are a header of offsets, not the older trailer of deltas
  chunk size differs per index. Derived, not written down anywhere

Hands back bytes. Decodes nothing.
"""

import bz2
import io
import lzma
import os
import re
import sqlite3
import struct
import zlib

# where the launcher keeps the cache; RS3MCP_GAME_CACHE for an install somewhere else
GAME = os.environ.get("RS3MCP_GAME_CACHE") or "C:/ProgramData/Jagex/RuneScape"

# The container: magic, uncompressed length, zlib.
MAGIC = b"ZLB\x01"


class Reader:
    """One byte reader over a buffer, big endian, which is what the cache is."""

    def __init__(self, buf):
        self.b = buf
        self.i = 0

    def left(self):
        return len(self.b) - self.i

    def u8(self):
        v = self.b[self.i]
        self.i += 1
        return v

    def u16(self):
        v = struct.unpack_from(">H", self.b, self.i)[0]
        self.i += 2
        return v

    def u32(self):
        v = struct.unpack_from(">I", self.b, self.i)[0]
        self.i += 4
        return v

    def varuint(self):
        """Jagex's own variable width int: the top bit of the first byte says which of two widths
        this is, and the value is what is left after it."""
        first = self.b[self.i]
        if first & 0x80:
            return self.u32() & 0x7FFFFFFF
        return self.u16()

    def skip(self, n):
        self.i += n


def unwrap(blob):
    """The container off a `cache` or `cache_index` payload.

    The stored length is checked rather than trusted, because a truncated group decompresses to
    something shorter without raising and the shortfall would show up much later as a config that
    is simply missing."""
    if blob is None or len(blob) < 9:
        return None
    if blob[:4] == MAGIC:
        want = struct.unpack_from(">I", blob, 4)[0]
        try:
            out = zlib.decompress(blob[8:])
        except zlib.error:
            return None
        return out if len(out) == want else None
    # the js5 container: a compression type, the stored length, and for all but none the
    # uncompressed length. Models are lzma
    kind, size = blob[0], struct.unpack_from(">I", blob, 1)[0]
    if kind == 0:
        return bytes(blob[5:5 + size])
    want = struct.unpack_from(">I", blob, 5)[0]
    body = blob[9:9 + size]
    try:
        if kind == 3:
            # raw lzma after five bytes of properties; the alone format wants the length between
            out = lzma.decompress(bytes(body[:5]) + struct.pack("<Q", want) + bytes(body[5:]),
                                  format=lzma.FORMAT_ALONE)
        elif kind == 2:
            out = zlib.decompress(body, 31)
        elif kind == 1:
            # bzip2 without its BZh1 header
            out = bz2.decompress(b"BZh1" + bytes(body))
        else:
            return None
    except (lzma.LZMAError, zlib.error, OSError, ValueError):
        return None
    return out if len(out) == want else None


def indexes(root=GAME):
    """The major numbers present on disk."""
    out = []
    for f in os.listdir(root):
        m = re.match(r"js5-(\d+)\.jcache$", f)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


_OPEN = {}


def _open(root, major):
    """The index, kept open. Reopening per group cost minutes over seconds."""
    key = (root, major)
    if key in _OPEN:
        return _OPEN[key]
    path = os.path.join(root, "js5-%d.jcache" % major).replace("\\", "/")
    if not os.path.exists(path):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, check_same_thread=False)
    _OPEN[key] = con
    return con


def descriptor(major, root=GAME):
    """What groups this index holds, and the files in each.

    File ids are sparse, so position in the group is not the id.
    """
    con = _open(root, major)
    if con is None:
        return None
    try:
        row = con.execute("select DATA from cache_index where KEY = 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    raw = unwrap(row[0])
    if raw is None:
        return None

    r = Reader(raw)
    fmt = r.u8()
    if fmt >= 6:
        r.u32()                       # timestamp, not used
    flags = r.u8()
    named = bool(flags & 0x1)
    hashed = bool(flags & 0x2)
    sized = bool(flags & 0x4)
    count = r.varuint() if fmt >= 7 else r.u16()

    # Column major from here. Each loop is one whole column.
    minors, acc = [], 0
    for _ in range(count):
        acc += r.u16()
        minors.append(acc)
    # A named index stores a hash of each group's name rather than the name. The hash is worth
    # keeping: a dictionary of known names turns it back into one, and for clientscripts that is
    # the difference between script19 and [proc,magic_runecount].
    hashes = [r.u32() for _ in range(count)] if named else [None] * count
    crcs = [r.u32() for _ in range(count)]
    if sized:
        for _ in range(count):
            r.u32()
        for _ in range(count):
            r.u32()
            r.u32()
    if hashed:
        r.skip(64 * count)
    versions = [r.u32() for _ in range(count)]
    counts = [(r.u32() & 0x7FFFFFFF) if (r.b[r.i] & 0x80) else r.u16() for _ in range(count)]

    files = []
    for n in counts:
        acc, ids = 0, []
        for _ in range(n):
            acc += r.u16()
            ids.append(acc)
        files.append(ids)
    if named:
        for n in counts:
            r.skip(4 * n)

    return {m: {"files": f, "version": v, "crc": c, "nameHash": h}
            for m, f, v, c, h in zip(minors, files, versions, crcs, hashes)}


def chunk(desc):
    """Files per full group, for turning (group, file) back into an id.

    Not the same everywhere and written down nowhere: items 256, npcs 128, structs 32. Taken as
    the highest file id in the index, rounded up to a power of two.
    """
    top = max((max(v["files"]) for v in desc.values() if v["files"]), default=0)
    size = 1
    while size <= top:
        size *= 2
    return size


def absolute(desc):
    """{config id: (group, file)} for a whole index."""
    n = chunk(desc)
    return {minor * n + fid: (minor, fid)
            for minor, v in desc.items() for fid in v["files"]}


def group(major, minor, root=GAME):
    """One group's bytes, decompressed, or None."""
    con = _open(root, major)
    if con is None:
        return None
    try:
        row = con.execute("select DATA from cache where KEY = ?", (minor,)).fetchone()
    except sqlite3.Error:
        return None
    return unwrap(row[0]) if row else None


def split(raw, nfiles):
    """A group cut into its files.

    The sqlite cache writes a header of offsets, not the older trailer of deltas.
    """
    if nfiles <= 1:
        return [raw]
    if not raw or len(raw) < 5 + 4 * nfiles:
        return None

    at = 1                                      # the byte whose meaning is unknown
    start = struct.unpack_from(">I", raw, at)[0]
    at += 4

    out = []
    for _ in range(nfiles):
        end = struct.unpack_from(">I", raw, at)[0]
        at += 4
        if end < start or end > len(raw):
            return None
        out.append(raw[start:end])
        start = end
    return out


def files(major, minor, root=GAME, desc=None):
    """{fileid: bytes} for one group, which is the shape everything downstream wants."""
    d = desc if desc is not None else descriptor(major, root)
    if not d or minor not in d:
        return None
    ids = d[minor]["files"]
    raw = group(major, minor, root)
    if raw is None:
        return None
    parts = split(raw, len(ids))
    if parts is None or len(parts) != len(ids):
        return None
    return dict(zip(ids, parts))
