"""
Known-answer tests. Run with: python rs3-mcp.py --test

--selftest proves the tools do not crash. It makes no claim about the numbers, and four measurement
bugs once shipped past it in one day. Every one was found by noticing something odd, which is not a
strategy.

These assert answers, not the absence of exceptions. An add-on adds its own under "tests", run on
the same tally.
"""
import glob
import io
import json
import os
import re
import subprocess
import tempfile
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# the add-on tests import the shared fixtures
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)

_FAILS = []
_RUN = [0]
_SKIPS = []


def check(name, got, want, note=""):
    _RUN[0] += 1
    if got != want:
        _FAILS.append((name, got, want, note))
        return False
    return True


def needs(what, *names):
    """True when this machine has what the checks after it read. Otherwise they are skipped, and the
    run says which and why, so a skip on the machine that should have the data is still seen."""
    from rs3mcp.test import workspace
    gone = workspace.missing(*names)
    if gone:
        _SKIPS.append((what, gone))
    return not gone


def skip(what, reasons):
    _SKIPS.append((what, reasons))


def approx(name, got, want, tol, note=""):
    _RUN[0] += 1
    if got is None or abs(got - want) > tol:
        _FAILS.append((name, got, "%s +/- %s" % (want, tol), note))
        return False
    return True


# --------------------------------------------------------------------------- varbit decode
def test_vars():
    from rs3mcp.vars import tool_var, _width
    check("vars: mask 255 width", _width(255), (8, True))
    check("vars: mask 14336 width", _width(14336), (3, True))
    check("vars: mask 0 width", _width(0), (0, True))
    check("vars: non-contiguous detected", _width(0b1011), (3, False))

    r = tool_var({"id": "-60893"})
    check("vars: shift", r.get("shift"), 11)
    check("vars: bits", r.get("bits"), 3)
    check("vars: range", r.get("range"), [0, 7])

    # an id both domains use answers the VarPlayer
    both = tool_var({"id": "12"})
    check("vars: a shared id gives the VarPlayer", both.get("domain"), "VarPlayer")
    check("vars: and names the other", (both.get("sameIdIn") or {}).get("domain"), "VarClient")
    check("vars: domain picks the other", tool_var({"id": "12", "domain": "varclient"}).get("domain"),
          "VarClient")

    # these names come from the name lists
    if not needs("var names", "name lists"):
        return
    check("vars: name", r.get("name"), "QUEST_EASTER26_BOSS_BATTLE_ROUND")
    check("vars: base name", (r.get("base") or {}).get("name"), "QUEST_EASTER26_PERM")
    check("vars: transmits the base", r.get("transmits"), "QUEST_EASTER26_PERM")

    # the globetrotter layout, pinned once it was asked
    g = tool_var({"name": "TRAIL_OUTFIT_CAPE_CHARGES"})
    check("vars: cape charges shift", g.get("shift"), 16)
    check("vars: cape charges bits", g.get("bits"), 3)
    b = tool_var({"name": "TRAIL_OUTFIT_BOOTS_CHARGE_PROGRESS"})
    check("vars: boots progress is 2 bits not 3", b.get("bits"), 2)


# --------------------------------------------------------------------------- interface ids
def test_iface():
    from rs3mcp.iface import tool_iface, _split, _pack
    check("iface: split 18612224", _split(18612224), (284, 0))
    check("iface: pack 284:0", _pack(284, 0), 18612224)
    check("iface: round trip", _split(_pack(1188, 5)), (1188, 5))

    r = tool_iface({"id": "18612224"})
    check("iface: packed id resolves", r.get("at"), "284:0")
    q = tool_iface({"id": "1188"})
    check("iface: root is not component 0", q.get("root"), 5,
          "251 of 400 interfaces hang off something other than 0")
    if needs("interface names", "name lists"):
        check("iface: name", r.get("name"), "BUFF_BAR")
        check("iface: CHOICE_V2 name", q.get("name"), "CHOICE_V2")

    b = tool_iface({"id": "284"})
    check("iface: BUFF_BAR flagged as runtime-built", b.get("builtAtRuntime"), True)


# --------------------------------------------------------------------------- cache invariants
def test_cache():
    from rs3mcp.common import connect
    from rs3mcp.cache import NONE_CHILD
    check("cache: npc morph sentinel", NONE_CHILD.get("npcs"), 65535)
    check("cache: loc morph sentinel", NONE_CHILD.get("objects"), 32767,
          "using 65535 for both read 32767 as a real child 30,309 times")
    c = connect()
    n = c.execute("select count(*) from varbit").fetchone()[0]
    # player varbits from the cache, not the csv
    approx("cache: varbit rows", n, 51113, 200)
    n = c.execute("select count(distinct id/65536) from config "
                  "where type='interfaces'").fetchone()[0]
    approx("cache: distinct interfaces", n, 1871, 20)


# --------------------------------------------------------------------------- sidecar index
def test_index():
    from rs3mcp.families import _index, ITEM_LINKS
    from rs3mcp.common import connect
    idx = _index()
    if idx is None:
        _FAILS.append(("index: sidecar missing", None, "rs3-index.db",
                       "run python rs3mcp/buildindex.py"))
        return
    c = connect()
    # the index must agree with a direct scan
    target = idx.execute("select target_id from item_link group by target_id "
                         "having count(*) between 2 and 8 limit 1").fetchone()[0]
    from_index = {(f, o) for f, o in idx.execute(
        "select field, owner_id from item_link where target_id=?", (target,)) if o != target}
    from_scan = set()
    for field in ITEM_LINKS:
        # anchor on the suffix, as the fallback does
        for suffix in (",", "}"):
            for oid, data in c.execute(
                    "select id,data from config where type='items' and data like ? limit 400",
                    ('%%"%s":%d%s%%' % (field, target, suffix),)):
                try:
                    if json.loads(data or "{}").get(field) == target and oid != target:
                        from_scan.add((field, oid))
                except Exception:
                    pass
    check("index: agrees with the scan", from_index, from_scan)


# --------------------------------------------------------------------------- invariants
def test_invariants():
    from rs3mcp.iface import _split, _pack, CHILD
    from rs3mcp.vars import _width

    bad = [(i, c) for i in (0, 1, 284, 1188, 1871, 65535)
           for c in (0, 1, 5, 999, CHILD) if _split(_pack(i, c)) != (i, c)]
    check("invariant: iface pack/split round-trips", bad, [])

    # a mask cannot claim more than 32 bits
    over = [m for m in (0, 1, 255, 0xFFFFFFFF, 14336, 268402688) if _width(m)[0] > 32]
    check("invariant: no mask exceeds 32 bits", over, [])

    # width must equal popcount, always
    wrong = [m for m in (0, 1, 3, 255, 14336, 0xFFFFFFFF, 0b1011)
             if _width(m)[0] != bin(m).count("1")]
    check("invariant: width equals popcount", wrong, [])


def test_opcode_specs():
    """Our opcode format: what it reads, what it refuses, and that the specs hold on the live cache."""
    from rs3mcp import ops

    # every value shape, on hand worked bytes
    spec = ops.parse("\n".join([
        "type demo", "index 99",
        "0x01 small      varu16",
        "0x02 wide       varu32 -> models",
        "0x03 name       string",
        "0x04 pair       {u16 from; u16 to -> materials}",
        "0x05 counted    {u8 _n; list[_n]<u16 -> npcs> ids}",
        "0x06 sized      {u16 _size; bytes[_size] body}",
        "0x07 params     params",
        "0x08 on         true",
        "0x09 signed     vari16",
    ]))
    data = bytes([
        0x01, 0x81, 0x02,                       # varu16 in two bytes: 258
        0x02, 0x80, 0x01, 0x86, 0xA0,           # varu32 in four: 100000
        0x03]) + b"Cow\x00" + bytes([
        0x04, 0x00, 0x05, 0x00, 0x06,
        0x05, 0x02, 0x03, 0x1E, 0x03, 0x1F,     # two npc ids, 798 and 799
        0x06, 0x00, 0x03, 0xAA, 0xBB, 0xCC,
        0x07, 0x02, 0x00, 0x00, 0x00, 0x0E, 0x00, 0x00, 0x00, 0x04,
        0x01, 0x00, 0x0B, 0xBA]) + b"hat\x00" + bytes([
        0x08,
        0x09, 0x7F,                             # one byte with bit 6 set: -1
        0x00])
    got, problem = ops.decode(spec, data)
    check("ops: a record decodes to its end", problem, None)
    check("ops: varu16 takes two bytes when the top bit is set", got.get("small"), 258)
    check("ops: varu32 takes four", got.get("wide"), 100000)
    check("ops: a string", got.get("name"), "Cow")
    check("ops: a struct keeps its names", got.get("pair"), {"from": 5, "to": 6})
    check("ops: a count read earlier sizes the list, and _ fields are not kept",
          got.get("counted"), {"ids": [798, 799]})
    check("ops: a size read earlier sizes the bytes", got.get("sized"), {"body": "aabbcc"})
    check("ops: params come back as a map, int and string", got.get("params"), {14: 4, 3002: "hat"})
    check("ops: true reads nothing", got.get("on"), True)
    check("ops: vari16 carries its sign", got.get("signed"), -1)
    check("ops: the spec knows what ids point at", ops.refs(spec),
          {"wide": "models", "pair.to": "materials", "counted.ids[]": "npcs"})

    # what it refuses, and how it says so
    _, p = ops.decode(spec, bytes([0x03]) + b"Cow\x00" + bytes([0x42, 0x01, 0x00]))
    check("ops: an opcode the spec lacks stops there and is named",
          (p or {}).get("kind"), "unknown")
    check("  with its number and where", ((p or {}).get("opcode"), (p or {}).get("at")), (0x42, 5))
    _, p = ops.decode(spec, bytes([0x04, 0x00]))
    check("ops: running out of bytes is broken, not a crash", (p or {}).get("kind"), "broken")
    _, p = ops.decode(spec, bytes([0x08, 0x00, 0x08]))
    check("ops: bytes after the end are reported", (p or {}).get("kind"), "trailing")
    _, p = ops.decode(spec, bytes([0x08]))
    check("ops: a record with no end marker is reported", (p or {}).get("kind"), "no end")
    for why, bad in (("a shape that does not exist", "index 3\n0x01 x nosuch"),
                     ("an unclosed list", "index 3\n0x01 x list<u8"),
                     ("an opcode twice", "index 3\n0x01 a u8\n0x01 b u8"),
                     ("a spec with no index", "0x01 x u8")):
        try:
            ops.parse(bad)
            refused = False
        except ops.SpecError:
            refused = True
        check("ops: refuses %s" % why, refused, True)

    # the shapes that depend on what precedes them
    spec = ops.parse("\n".join([
        "type demo", "index 99",
        "0x01 slot       {u16 kind; leb128 enum if kind==0; leb128 skill if kind==4}",
        "0x02 bits       {u8 flags; u8 low if flags&0x01; u8 high if flags!&0x80; i8 v; u8 old if v==-1; u8 new if v>=6}",
        "0x03 counts     {u8 _n; list[_n-1]<u8> rest; u8 m; list[m&15]<u8> lo; list[m>>4]<u8> hi}",
        "0x04 until      list[end 0xFF]<u8>",
        "0x05 cells      {u8 _n; list[_n]<varu16> types; dbvalues[types] rows}",
        "0x06 label      pstring",
        "0x07 half       f32",
    ]))
    data = bytes([
        0x01, 0x00, 0x00, 0xE1, 0x0B,           # kind 0: an enum, 1505 as leb128
        0x01, 0x00, 0x04, 0x10,                 # kind 4: skill 16, one byte
        0x02, 0x01, 0x07, 0x09, 0xFF, 0x03,     # bit 0 set, bit 7 clear, version -1
        0x03, 0x03, 0x0A, 0x0B, 0x21, 0x01, 0x02, 0x03,
        0x04, 0x05, 0x06, 0xFF,
        0x05, 0x02, 0x24, 0x00, 0x01]) + b"row\x00" + bytes([0x00, 0x00, 0x00, 0x07,
        0x06, 0x00]) + b"Hi\x00" + bytes([
        0x07, 0x3F, 0x00, 0x00, 0x00,
        0x00])
    got, problem = ops.decode(spec, data)
    check("ops: conditional fields decode to the end", problem, None)
    check("ops: leb128 and an if on the kind before it", got.get("slot"), {"kind": 4, "skill": 16})
    check("ops: & and !& test bits, and a negative version compares",
          got.get("bits"), {"flags": 1, "low": 7, "high": 9, "v": -1, "old": 3})
    check("ops: counts less one, and from each nibble",
          got.get("counts"), {"rest": [10, 11], "m": 0x21, "lo": [1], "hi": [2, 3]})
    check("ops: a list that ends on a marker", got.get("until"), [5, 6])
    check("ops: db cells are a string or an int by type", got.get("cells"),
          {"types": [36, 0], "rows": [["row", 7]]})
    check("ops: pstring and f32", (got.get("label"), got.get("half")), ("Hi", 0.5))

    record = ops.parse("type demo\nindex 99\nrecord {\n  u8 n\n  list[n]<u16> ids\n  bytes[rest] tail\n}")
    got, problem = ops.decode(record, bytes([0x02, 0x00, 0x01, 0x00, 0x02, 0xAB]))
    check("ops: a record form is one shape, and bytes[rest] takes what is left",
          (problem, got), (None, {"n": 2, "ids": [1, 2], "tail": "ab"}))

    either = ops.parse("type demo\nindex 99\nrecord {\n  u8 kind\n  u8 op\n  u8 data if op!=0 or kind==13\n}")
    check("ops: or reads a field when any test holds",
          [ops.decode(either, bytes(b))[0].get("data") for b in ((1, 0), (1, 5, 7), (13, 0, 9))],
          [None, 7, 9])

    # the specs against the game's own cache
    from rs3mcp import cachefile
    # every record decodes, live and beta
    known = {}
    if os.path.exists(os.path.join(cachefile.GAME, "js5-22.jcache")):
        for name in sorted(f[:-4] for f in os.listdir(ops.SPECS) if f.endswith(".ops")):
            unused = None
            for root in cachefile.builds():
                got = ops.verify(name, root)
                check("ops: every %s record decodes to its end in %s" % (name, os.path.basename(root)),
                      got["records"] - got["decodedWhole"], known.get(name, 0), str(got["problems"][:2]))
                unused = set(got["neverUsed"]) if unused is None else unused & set(got["neverUsed"])
            check("  and every opcode in the %s spec is used by one of them" % name, sorted(unused or ()), [])

        # the solver finds an untold value shape
        npcs = ops.load("npcs")
        blind = dict(npcs, ops={k: v for k, v in npcs["ops"].items() if k != 0x5F})
        got = ops.solve("npcs", 0x5F, sample=150, spec=blind)
        top = (got.get("shapes") or [{}])[0]
        check("ops: the solver ranks a two byte shape first for combat level",
              top.get("shape") in ("u16", "i16", "varu32", "vari32", "bytes[2]"), True, str(top)[:90])
        check("  and it fits every record it tried", top.get("fits"), got.get("records"))


def test_sprites():
    """Both sprite layouts, the canvas, the png, and a few of the live cache's awkward ones."""
    import struct
    import zlib
    from rs3mcp import cachefile, sprites

    def pixels(b):
        return [tuple(b[i:i + 4]) for i in range(0, len(b), 4)]
    red, blue, clear = (255, 0, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0)

    # paletted: two frames, stored column by column
    pal = bytes([1, 1, 0, 2, 1, 255, 0, 0, 0, 0, 255]) + struct.pack(">HHBHHHHH", 3, 2, 2, 1, 0, 2, 2, 1)
    w, h, frames = sprites.decode(pal)
    check("sprites: the canvas size", (w, h), (3, 2))
    check("sprites: columns turned back into rows, index 0 see-through", pixels(frames[0][4]),
          [red, blue, clear, red])
    check("sprites: the frame lands at its offset", pixels(sprites.canvas(w, h, frames[0])),
          [clear, red, blue, clear, clear, red])
    check("sprites: index 0 keeps the frame's own alpha",
          pixels(sprites.decode(bytes([2, 0, 255]) + struct.pack(">HHBHHHHH", 1, 1, 0, 0, 0, 1, 1, 1))[2][0][4]),
          [(0, 0, 0, 255)])
    # an empty frame still has its flags byte
    empty = bytes([0, 0, 1, 0, 255, 0]) + struct.pack(">HHBHHHHHHHHH", 1, 1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 2)
    check("sprites: an empty frame keeps its flags byte", pixels(sprites.decode(empty)[2][1][4]), [(0, 255, 0, 255)])

    # true colour: one header, two frames with alpha
    true = bytes([0, 1]) + struct.pack(">HH", 1, 2) + bytes([1, 2, 3, 4, 5, 6, 128, 255]) \
        + bytes([7, 8, 9, 10, 11, 12, 0, 16]) + struct.pack(">H", 2 | 0x8000)
    w, h, frames = sprites.decode(true)
    check("sprites: true colour frames share one header", [(f[2], f[3]) for f in frames], [(1, 2), (1, 2)])
    check("sprites: and each frame's alpha follows its colours", pixels(frames[1][4]), [(7, 8, 9, 0), (10, 11, 12, 16)])
    try:
        sprites.decode(true[:-2] + b"\x00" + true[-2:])
        check("sprites: a stray byte is refused", "decoded", "Stop")
    except sprites.Stop:
        check("sprites: a stray byte is refused", "Stop", "Stop")

    body = sprites.png(3, 2, sprites.canvas(3, 2, sprites.decode(pal)[2][0]))
    check("sprites: a png signature", body[:8], b"\x89PNG\r\n\x1a\n")
    idat = body.index(b"IDAT")
    size = struct.unpack(">I", body[idat - 4:idat])[0]
    raw = zlib.decompress(body[idat + 4:idat + 4 + size])
    check("sprites: rows each start with filter 0", (len(raw), raw[0], raw[13]), (26, 0, 0))

    if os.path.exists(os.path.join(cachefile.GAME, "js5-8.jcache")):
        desc = cachefile.descriptor(8)
        for g, want in ((0, (48, 48, 1)), (1243, None), (1962, (66, 22, 4))):
            files = cachefile.files(8, g, cachefile.GAME, desc)
            w, h, frames = sprites.decode(files[min(files)])
            if want:
                check("sprites: live sprite %s decodes" % g, (w, h, len(frames)), want)
            else:
                check("sprites: a live sprite opening on an empty frame still draws",
                      ((frames[0][2], frames[0][3]), sprites._group_png(files[min(files)])[:4]),
                      ((0, 0), b"\x89PNG"))


def test_maptiles():
    """Map ground: a flags byte per tile, the fields it names in order, the rest kept, and live squares."""
    import struct
    from rs3mcp import cachefile, maptiles

    # tile 0 has all four fields, tile 1 settings only, the rest nothing
    first = bytes([0x0F, (3 << 2) | 1, 0x81, 0x10, 0x01, 0x81, 0x41]) + struct.pack(">H", 35)
    body = b"jagx\x01" + first + bytes([0x02, 0x05]) + bytes(4 * 64 * 64 - 2) + b"tail"
    tiles, rest = maptiles.decode(body)
    check("maptiles: overlay shape, rotation and a two byte id, settings, underlay, height",
          tiles[0][0][0], {"shape": 3, "rotation": 1, "overlay": 272, "settings": 1, "underlay": 321, "height": 35})
    check("maptiles: z runs inside x", tiles[0][0][1], {"settings": 5})
    check("maptiles: what follows the tiles is kept", rest, b"tail")
    try:
        maptiles.decode(b"jagx\x01" + bytes([0x10]) + bytes(4 * 64 * 64))
        check("maptiles: a flag bit past 0x08 is refused", "decoded", "Stop")
    except maptiles.Stop:
        check("maptiles: a flag bit past 0x08 is refused", "Stop", "Stop")

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        bad = []
        for k, (x, z, data) in enumerate(maptiles.squares()):
            if k % 25:
                continue
            try:
                maptiles.decode(data)
            except (maptiles.Stop, IndexError, struct.error) as e:
                bad.append((x, z, str(e)))
        check("maptiles: every 25th live square decodes to its last tile", bad[:3], [])
        got = maptiles.square(50, 50)
        ground = [t for column in got[0][0] for t in column]
        check("maptiles: Lumbridge's square has blocked ground and overlays",
              (sum(1 for t in ground if t.get("settings", 0) & maptiles.BLOCKED) > 100,
               sum(1 for t in ground if "overlay" in t) > 100), (True, True))


def test_mapwalk():
    """Walking: a wall stops a step and a diagonal past it, and Lumbridge's river is crossed at its bridge."""
    from rs3mcp import cachefile, mapwalk

    g = mapwalk.Grid.__new__(mapwalk.Grid)
    g.plane, g.bx, g.bz, g.w, g.h, g.doors = 0, 0, 0, 4, 4, {}
    g.flags = bytearray(16)
    g._wall(1, 1, mapwalk.E)
    check("mapwalk: a wall blocks the step across it, both ways",
          (g.can_step(1, 1, 1, 0), g.can_step(2, 1, -1, 0)), (False, False))
    check("mapwalk: and the diagonal past it", g.can_step(1, 1, 1, 1), False)
    check("mapwalk: the way round is still open", len(g.path((1, 1), (2, 1)) or []) > 2, True)
    g.doors[(1, 1)] = 1
    g.open_doors = set()
    check("mapwalk: with doors on, a door is walked through", g.path((1, 1), (2, 1), doors=True), [(1, 1), (2, 1)])
    check("mapwalk: a shut door is walked round, one a world opened is walked through",
          (len(g.path((1, 1), (2, 1)) or []) > 2, g.path((1, 1), (2, 1), doors={(1, 1)})), (True, [(1, 1), (2, 1)]))

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        grid = mapwalk.Grid(50, 50, 50, 50)
        tiles = grid.path((3222, 3218), (3259, 3230)) or []
        crossing = sorted({z for x, z in tiles if 3242 <= x <= 3252})
        check("mapwalk: Lumbridge to the far bank crosses the river on the bridge",
              bool(crossing) and 3223 <= crossing[0] and crossing[-1] <= 3227, True, str(crossing))
        start, beside = (3222, 3218), (3224, 3219)
        check("mapwalk: of equally short walks the straight step comes first",
              (grid.path(start, beside) or [None, None])[1], (start[0] + 1, start[1]))

        import threading
        shut, opened = 1506, 1507
        objs = mapwalk._Objects()
        met = [(objs.get(opened)["door"], objs.get(opened)["open"])]
        # a cached grid serves the next request's thread
        other = threading.Thread(target=lambda: met.append(objs.get(shut)["door"]))
        other.start()
        other.join()
        check("mapwalk: an open door is the loc offering Close, and objects read on one thread read on another",
              met, [(True, True), True])
        desc = cachefile.descriptor(5, cachefile.GAME)
        check("cachefile: an index descriptor is parsed once and kept", cachefile.descriptor(5, cachefile.GAME) is desc,
              True)


def test_mapnear():
    """What is around: a morph picks its loc by var, and Lumbridge's Large door opens for one world."""
    from rs3mcp import cachefile, mapnear

    class Objects:
        def get(self, lid):
            wide = {"varbit": 5, "varp": None, "ids": [10, mapnear.NONE], "atEnd": 30, "pastEnd": 40}
            return {"morph": dict(wide, pastEnd=None) if lid == 1 else wide if lid == 3 else None}
    objs = Objects()
    check("mapnear: a morph shows ids[value], nothing for NONE, its end at the list's length, nothing past it",
          [mapnear.shown(objs, 1, lambda kind, vid: v) for v in (0, 1, 2, 3)], [10, None, 30, None])
    check("mapnear: the wide form shows its other field past the end",
          mapnear.shown(objs, 3, lambda kind, vid: 3), 40)
    check("mapnear: a loc without a morph shows itself", mapnear.shown(objs, 2, lambda kind, vid: 0), 2)

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        from rs3mcp import mapwalk
        grid = mapwalk.Grid(50, 50, 50, 50)

        def door(doors):
            rows = [r for r in mapnear.around(grid, 3222, 3218, 12, doors)
                    if (r["x"], r["z"]) == (3213, 3221) and r["name"] == "Large door"]
            return (rows[0]["door"], rows[0]["options"]) if rows else None
        check("mapnear: Lumbridge's Large door starts shut, and offers Close once a world opens it",
              (door(None), door({(3213, 3221)})), (("closed", ["Open"]), ("open", ["Close"])))


def test_mapscene():
    """The 3D scene: hsl colours, offsets pointing down, and a small build that is a valid glb."""
    import json as _json
    import struct
    from rs3mcp import cachefile, mapscene
    check("mapscene: hsl lightness 0 is black, full lightness near white",
          (mapscene.hsl_rgb(0), min(mapscene.hsl_rgb(127)) > 240), ((0, 0, 0), True))

    class Scene:
        def __init__(self):
            self.nodes = []
            self.glb = self

        def node(self, n):
            self.nodes.append(n)

        def mesh(self, mids, recolour, retexture):
            return 0
    sc = Scene()
    flat = {p: {(x, z): 0 for x in range(9) for z in range(9)} for p in range(4)}
    mapscene._place(sc, {"models": [{"shape": 10, "models": [1]}]}, 0, 4, 4, 10, 0, {"translateY": -425}, flat)
    check("mapscene: a negative translateY lifts the loc", sc.nodes[0]["matrix"][13], 425)
    sc.nodes = []
    mapscene._place(sc, {"models": [{"shape": 0, "models": [1]}], "length": 3}, 0, 4, 4, 0, 0, {}, flat)
    check("mapscene: a wall three tiles long is centred on its run, not its first tile",
          sc.nodes[0]["matrix"][14], 5.5 * mapscene.TILE)
    sc.nodes = []
    mapscene._place(sc, {"models": [{"shape": 2, "models": [1]}]}, 0, 4, 4, 2, 0, {}, flat)
    check("mapscene: an L corner is two pieces, the first mirrored, the second a quarter turn on",
          [(round(n["matrix"][0]), round(n["matrix"][10])) for n in sc.nodes], [(1, -1), (0, 0)])

    import numpy as np
    dark = np.full((3, 3, 3), 200, np.uint8)
    dark[1, 1] = 0
    check("mapscene: a world map edge line takes its neighbours' colour",
          tuple(mapscene._no_outlines(dark)[1, 1]), (200, 200, 200))

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        from rs3mcp import mapground
        img = mapground.texture(3240, 3220, 3249, 3229)
        wet = mapground.water(3240, 3220, 3249, 3229, mapscene.HEIGHT)
        check("mapground: ten tiles of ground texture, and the River Lum's surface above its bed",
              (img.size, wet is not None and min(wet["corners"].values()) > 0),
              ((10 * mapground.PX, 10 * mapground.PX), True))

    if os.path.exists(os.path.join(cachefile.GAME, "js5-47.jcache")):
        out = os.path.join(tempfile.mkdtemp(), "lumbridge.glb")
        got = mapscene.build(3222, 3218, out, 0, 4)
        with open(out, "rb") as fh:
            magic, version, total = struct.unpack("<III", fh.read(12))
            size, kind = struct.unpack("<II", fh.read(8))
            doc = _json.loads(fh.read(size))
        check("mapscene: Lumbridge's courtyard builds a glTF 2 binary with its locs placed, each with a model",
              (magic, version, total == os.path.getsize(out), got["locs"] > 10, len(doc["nodes"]) > 10, got["noModel"]),
              (0x46546C67, 2, True, True, True, 0))
        # a leaf material, layout v1
        sc, leaf = mapscene._Scene(), 5463
        used = sc.material(leaf)
        check("mapscene: a v1 material's texture is read, and a cut-out one is drawn single sided",
              (used is not None, used is not None and sc.glb.gltf["materials"][used].get("doubleSided")), (True, False))


def test_worldaudit():
    """The audit on a scan made from the cache itself: every loc matches, the height scale planted
    is the one fitted, and a route walked by mapwalk agrees with itself."""
    from rs3mcp import cachefile, mapnear, maplocs, maptiles, mapwalk, worldaudit
    if not os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        return
    tiles = maptiles.square(50, 50)[0]
    objs = mapwalk._Objects()
    locs = []
    for lid, uses in maplocs.decode(cachefile.files(5, 50 | 50 << 7)[0]):
        for p, x, z, shape, rot, _ in uses:
            if p == 0 and 16 <= x < 32 and 16 <= z < 32:
                stored = tiles[0][x][z].get("height", 0)
                shown = mapnear.shown(objs, lid, lambda k, v: 0)
                # nothing shown reports the parent
                locs.append([lid, lid if shown is None else shown, 3200 + x, 3200 + z, 0, shape, rot,
                             1, 1, 40 * stored + 7, 0, 0, 0, 0, 0, 0, False])
    grid = mapwalk.Grid(49, 49, 51, 51)
    walked = grid.path((3222, 3218), (3226, 3222), nearest=True)
    # client-style: diagonals as two tiles
    path = [walked[0]]
    for a, b in zip(walked, walked[1:]):
        if a[0] != b[0] and a[1] != b[1]:
            path.append((b[0], a[1]))
        path.append(b)
    columns = ["parent", "id", "x", "z", "level", "shape", "rotation", "width", "length", "y",
               "minX", "minY", "minZ", "maxX", "maxY", "maxZ", "hidden"]
    rec = os.path.join(tempfile.mkdtemp(), "rec-world.jsonl")
    with open(rec, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "locs", "t": 100, "player": [3222, 3218, 0], "columns": columns, "rows": locs}) + "\n")
        fh.write(json.dumps({"kind": "routes", "t": 150, "columns": ["tick", "x", "z", "level", "toX", "toZ",
                             "toLevel", "route"], "rows": [[1, 3222, 3218, 0, 3226, 3222, 0, [list(t) + [0] for t in path[1:]]]]}) + "\n")
    got = worldaudit.audit(rec)
    counts = got["locs"]["counts"]
    check("worldaudit: a scan of the cache's own locs matches every one, none left over in the scanned tiles",
          (counts.get("matched"), counts.get("clientOnly", 0), counts.get("morphDiffers", 0)), (len(locs), 0, 0))
    check("worldaudit: the height scale planted in the scan is the one fitted back",
          (got["heights"]["slope"], got["heights"]["offset"]), (40, 7))
    check("worldaudit: a route mapwalk planned agrees with mapwalk", got["routes"]["agree"], 1)
    var = worldaudit.var_reader({12519: 1, 12518: 3 << 27, 555: 9})
    check("worldaudit: a scan's bases give a varbit its bits, low and high, and a varp its value",
          (var("varbit", 60228), var("varbit", 60227), var("varp", 555), var("varp", 556)), (1, 3, 9, 0))
    check("worldaudit: a scan without vars gives no reader", worldaudit.var_reader({}), None)


def test_agent_view():
    """A watcher's note reaches the agent's next reply once, and the frame shows it read."""
    import collections
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import host
    w = host.World.__new__(host.World)
    w.id, w.notes, w.history, w.perceived, w.focus = "w9", [], collections.deque(maxlen=5), None, None
    w.note = lambda by, what: {"logic": 1}
    host.HOST.worlds["w9"] = w
    try:
        host.HOST.note({"world": "w9", "text": "go north"})
        verb = host.with_notes(lambda args: {"world": "w9"})
        first, second = verb({}), verb({})
    finally:
        del host.HOST.worlds["w9"]
    check("agent view: a note rides the next reply, not the one after, and is marked read",
          ([n["text"] for n in first.get("notesFromWatcher", [])], "notesFromWatcher" in second, w.notes[0]["read"]),
          (["go north"], False, True))


def test_dev_loop():
    """An edit restarts the plugin where the player stood; a broken edit keeps the world until fixed."""
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import host
    preset = json.load(open(os.path.join(host.PRESETS, "lumbridge.json"), encoding="utf-8"))
    plugin = tempfile.mkdtemp()
    main = os.path.join(plugin, "main.lua")
    with open(main, "w", encoding="utf-8") as fh:
        fh.write('print("up")\n')
    conf = dict(preset, plugin=plugin)
    w = host.World("w9", conf, "dev", "test", "step", None, False)
    tile = (3225, 3218)
    try:
        w.send("place %d %d 0" % tile)
        with open(main, "w", encoding="utf-8") as fh:
            fh.write("local = \n")
        w.reload(["main.lua"])
        broken = bool(w.broken) and "main.lua:1:" in w.broken
        with open(main, "w", encoding="utf-8") as fh:
            fh.write('print("back")\n')
        w.reload(["main.lua"])
        where = w.ask("where")
        # stderr drains on its own thread
        for _ in range(20):
            if "back" in w.log:
                break
            time.sleep(0.05)
    finally:
        w.close()
    check("dev loop: a syntax error marks the world broken with file:line, the fix brings the player back",
          (broken, w.broken, (where["x"], where["z"]), [r["ok"] for r in w.reloads], "back" in w.log),
          (True, None, tile, [False, True], True))


def test_zone():
    """The dev zone: diffs come from disk, a claim names the editor once, an answer reaches its sender once."""
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import zone
    kept = os.path.join(tempfile.mkdtemp(), "items.jsonl")
    before = zone.Zone(store=kept)
    asked = before.show({"title": "Pick", "body": "# Which", "ask": ["A", "B"], "by": "agent"})["seq"]
    before.answer({"seq": asked, "choice": "B"})
    after = zone.Zone(store=kept)
    again = after.show({"title": "Next", "body": "# More", "by": "agent"})["seq"]
    check("zone: a restart keeps what was shown and answered, and numbering carries on",
          ([i["title"] for i in after.items], after.items[0].get("answer", {}).get("choices"), again > asked),
          (["Pick", "Next"], ["B"], True))

    z = zone.Zone()
    folder = tempfile.mkdtemp()
    watched, other = os.path.join(folder, "a.lua"), os.path.join(tempfile.mkdtemp(), "b.py")

    def write(path, text):
        was = os.stat(path).st_mtime_ns if os.path.exists(path) else 0
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        # later than the last write's mtime
        now = max(time.time_ns(), was + 1000000)
        os.utime(path, ns=(now, now))

    write(watched, "local a = 1\nreturn a\n")
    write(other, "x = 1\n")
    z.watch(folder)
    write(watched, "local a = 2\nreturn a\n")
    z.scan()
    z.claim({"file": other, "by": "Claude e4db", "when": "pre"})
    write(other, "x = 1\ny = 2\n")
    z.claim({"file": other, "by": "Claude e4db", "when": "post"})
    z.claim({"file": watched, "by": "worker", "when": "pre"})
    write(watched, "local a = 3\nreturn a\n")
    z.scan()
    z.claim({"file": watched, "by": "worker", "when": "post"})
    z.scan()
    code = [(os.path.basename(i["source"]), i["by"], i["added"], i["removed"]) for i in z.since({})["items"]]
    check("zone: an unclaimed change is on disk, a claimed one named, and the watcher does not count it twice",
          code, [("a.lua", None, 1, 1), ("b.py", "Claude e4db", 1, 0), ("a.lua", "worker", 1, 1)])

    seq = z.show({"kind": "sketch", "title": "t", "body": "<svg/>", "ask": ["A", "B"], "by": "agent"})["seq"]
    try:
        z.answer({"seq": seq, "choice": "C"})
        wrong = False
    except ValueError:
        wrong = True
    z.answer({"seq": seq, "choice": "B"})
    check("zone: a choice not offered is refused, the pick reaches the sender once",
          (wrong, [a["choices"] for a in z.take("agent")], z.take("agent")), (True, [["B"]], []))

    shown = z.show({"body": "# Plan", "by": "agent", "open": True})["seq"]
    z.steer({"tab": "code", "by": "agent"})
    try:
        z.steer({"item": 999999, "by": "agent"})
        missing = False
    except ValueError:
        missing = True
    moves = z.since({"steerSince": 0})["steers"]
    check("zone: open and steer move the viewer, newer only, a missing item refused",
          ([(m.get("item"), m.get("tab")) for m in moves], z.since({"steerSince": moves[0]["seq"]})["steers"][0]["tab"], missing),
          ([(shown, None), (None, "code")], "code", True))

    import host
    refused = []
    for origin in (None, host.VIEWER, "https://example.com", "null"):
        h = host.Handler.__new__(host.Handler)
        h.headers = {} if origin is None else {"Origin": origin}
        h._reply = lambda code, body: None
        refused.append(h._foreign())
    check("host: a page other than the viewer is refused before any verb runs, scripts are not",
          refused, [False, False, True, True])
    # DNS rebinding, caught by the Host header
    rebound = []
    for hosted in (None, "127.0.0.1:%d" % host.PORT, "localhost:%d" % host.PORT, "evil.test:%d" % host.PORT,
                   "127.0.0.1.nip.io:%d" % host.PORT):
        h = host.Handler.__new__(host.Handler)
        h.headers = {} if hosted is None else {"Host": hosted}
        h._reply = lambda code, body: None
        rebound.append(h._foreign())
    check("host: a request naming any host but this one is refused, so a rebound name reaches nothing",
          rebound, [False, False, False, True, True])


def test_chat():
    """The Chat tab: a transcript read into events and followed, and a message reaching the listener once."""
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import chat
    path = os.path.join(tempfile.mkdtemp(), "t.jsonl")
    rows = [
        {"type": "user", "message": {"content": "fix the stairs <system-reminder>hidden</system-reminder>"}},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Looking."},
                                                       {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "no match\nmore"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "an agent's"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "AskUserQuestion",
                                                       "input": {"questions": [{"question": "Which?"}]}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "picked A"}]}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    s = chat.Session("-".join("0" * n for n in (8, 4, 4, 4, 12)), path)
    s.follow()
    got = [(e["who"], e.get("text") or e.get("line"), e.get("failed"), e.get("answer")) for e in s.events]
    check("chat: reminders and agents' lines left out, a failed edit marked, a question carries its answer", got,
          [("you", "fix the stairs", None, None), ("claude", "Looking.", None, None), ("tool", "Edited a.py", "no match", None),
           ("claude", None, None, "picked A")])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}) + "\n")
        fh.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "half')
    s.follow()
    check("chat: following reads only what is new, and waits for a line still being written",
          [e.get("text") for e in list(s.events)[4:]], ["Done."])

    import threading
    before = len(s.events)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n")
        for k in range(200):
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "line %d" % k}]}}) + "\n")
    readers = [threading.Thread(target=s.follow) for _ in range(4)]
    for t in readers:
        t.start()
    for t in readers:
        t.join()
    check("chat: four requests following at once add each new line once", len(s.events) - before, 200)

    c = chat.Chat()
    c.sessions[s.id] = s
    c.send({"session": s.id, "text": "hello", "by": "test"})
    first, second = c.wait({"session": s.id, "timeout": 0.1}), c.wait({"session": s.id, "timeout": 0.1})
    check("chat: a sent message reaches the listener once, with its sender",
          ([(m["text"], m["by"]) for m in first["messages"]], second["messages"]), ([("hello", "test")], []))


def test_runner():
    """The runner reads Claude Code's stream as measured: partial text, a permission request, the turn's end;
    and answers in the shapes the CLI took."""
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import collections
    import threading
    import runner

    class Proc:
        def __init__(self, lines):
            self.stdout, self.stdin, self.written = iter(lines), self, []

        def poll(self):
            return None

        def write(self, s):
            self.written.append(json.loads(s))

        def flush(self):
            pass

    lines = [json.dumps(r) + "\n" for r in [
        {"type": "system", "subtype": "init", "model": "claude-haiku"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "control_request", "request_id": "r1", "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                                                                     "input": {"command": "mkdir x"},
                                                                     "permission_suggestions": [{"type": "addRules"}]}},
    ]]
    run = runner.Run.__new__(runner.Run)
    run.proc, run.lock, run.working, run.partial = Proc(lines), threading.Lock(), True, ""
    run.permissions, run.last, run.model, run.errors = collections.OrderedDict(), None, None, collections.deque()
    run._read()
    mid = (run.partial, list(run.permissions), run.model)
    run.permit("r1", "allow", always=True)
    run.proc.stdout = iter([json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.01}) + "\n"])
    run._read()
    run.stop()
    sent = run.proc.written
    check("runner: text streams, a permission waits, allow-always carries the suggestion, the result ends the turn, "
          "stop is an interrupt", (mid, sent[0]["response"]["response"], run.working, run.last["total_cost_usd"],
                                   sent[1]["request"]["subtype"]),
          (("Hello", ["r1"], "claude-haiku"),
           {"behavior": "allow", "updatedInput": {"command": "mkdir x"}, "updatedPermissions": [{"type": "addRules"}]},
           False, 0.01, "interrupt"))


def test_context():
    """The Context tab reads /context's report and judges by Claude Code's own rules: keep what is used,
    say when data is thin, and price a deferred server at nothing."""
    sys.path.insert(0, os.path.join(ROOT, "sandbox"))
    import context
    text = "\n".join([
        "## Context Usage", "", "**Model:** claude-opus-5-5  ", "**Tokens:** 10.5k / 1m (1%)", "",
        "### Estimated usage by category", "", "| Category | Tokens | Percentage |", "|---|---|---|",
        "| System tools | 8k | 0.8% |", "| MCP tools (deferred) | 3.3k | 0.3% |", "| Skills | 2.5k | 0.2% |", "",
        "### MCP Tools", "", "| Tool | Server | Tokens |", "|---|---|---|",
        "| mcp__alpha__a | alpha | 1.1k |", "| mcp__alpha__b | alpha | 0.9k |", "| mcp__beta__c | beta | ~0.2k |", "",
        "### Skills", "", "| Skill | Source | Tokens |", "|---|---|---|",
        "| big:one | Plugin (big) | 2k |", "| small:two | Plugin (small) | < 20 |"])
    m = context.parse(text)
    check("context: /context parsed: model, total, servers summed, deferred noticed",
          (m["model"], m["perTurn"], [(s["server"], s["tools"], s["tokens"]) for s in m["servers"]], m["mcpDeferred"], m["mcpLoaded"]),
          ("claude-opus-5-5", 10500, [("alpha", 2, 2000), ("beta", 1, 200)], 3300, 0))
    cwd = tempfile.mkdtemp()
    run = lambda **used: {"cwd": cwd, "servers": used.get("servers", {}), "skills": used.get("skills", {}), "agents": {}}
    runs = [run(servers={"alpha": 3}, skills={"small:two": 1})] + [run() for _ in range(5)]
    r = context.report(cwd, m, runs)
    plugins = {p["plugin"]: p["verdict"] for p in r["plugins"]}
    servers = {s["server"]: (s["verdict"], s["perTurn"], s["perHundred"]) for s in r["servers"]}
    thin = context.report(cwd, m, runs[:2])
    check("context: a used plugin is kept, an unused one flagged, a deferred server priced at nothing, a thin "
          "window says so", (plugins, servers["alpha"][0], servers["beta"][1:], {p["verdict"] for p in thin["plugins"] if p["plugin"] == "big"}),
          ({"big": "off", "small": "keep"}, "keep", (0, 0.0), {"unsure"}))


def test_judge():
    """The local classifier: TypeSafe's confidence formula, nearest text found first, a trained head beating
    its majority baseline on held-out rows, and similarity answers marked uncalibrated."""
    from rs3mcp import judge
    check("judge: confidence is (n * top - 1) / (n - 1), clipped",
          [round(judge.confidence(p), 3) for p in ([1, 0, 0], [1 / 3] * 3, [0.85, 0.15, 0.0], [0.5, 0.5])],
          [1.0, 0.0, 0.775, 0.0])
    if not os.path.exists(os.path.join(judge.MODEL_DIR, "model.onnx")):
        return
    near = judge.nearest("the hook prints to stdout and nobody reads it",
                         ["a price table for rune items", "hook output on stdout is decoration only", "quest step order"])
    check("judge: the nearest text is the one that means the same", near[0][0], 1)
    good = ["the fix works and the tests pass", "all checks green after the change", "verified in the client, it loads",
            "ran the suite, nothing failed", "confirmed the output matches", "the build succeeded and I checked it"] * 3
    bad = ["I think it should work now", "this probably fixes it", "should be fine, not run yet",
           "untested but looks right", "I assume the tests pass", "likely correct, did not check"] * 3
    old = judge.HEADS
    judge.HEADS = tempfile.mkdtemp()
    try:
        m = judge.train("claims", good + bad, ["checked"] * len(good) + ["claimed"] * len(bad))
        got = judge.judge({"state": "haven't run it but it ought to work", "questions": {
            "q": {"type": "choice", "head": "claims"}, "z": {"type": "choice", "criteria": {"a": "apples", "b": "tests"}}}})
    finally:
        judge.HEADS = old
    check("judge: a trained head beats its baseline held out; its answers are calibrated, similarity's are not",
          (m["accuracy"] > m["baseline"], got["answers"]["q"]["choice"], got["answers"]["q"]["calibrated"],
           got["answers"]["z"]["calibrated"], got["answers"]["z"]["engine"]), (True, "claimed", True, False, "similarity"))
    if judge.nli() is None:
        return
    said = "Ran the selftest: every check passed."
    unsaid = "Changed the file; it should work now, nothing was run."
    nouls = [judge.judge({"state": s, "questions": {"q": {"type": "noul", "instructions": "The tests were run."}}})
             ["answers"]["q"]["noul"] for s in (said, unsaid)]
    check("judge: NLI says a run was claimed when the text shows one, and not when it does not",
          (nouls[0] > 0.8, nouls[1] < 0.2), (True, True), str(nouls))


def test_unseen_numbers_and_files():
    """A claim's numbers and files the quote never shows are listed; ones it carries are not."""
    from rs3mcp.judge import unseen
    check("unseen: a count the quote lacks", unseen("it took 12 ms", "it took 40 ms")["numbers"], ["40"])
    check("unseen: rounding at the claim's precision", unseen("took 24.6 ms", "took about 25 ms")["numbers"], [])
    check("unseen: a percentage of quoted counts", unseen("5 of 10 passed", "50% passed")["numbers"], [])
    check("unseen: k read as thousands", unseen("4000 steps", "4k steps")["numbers"], [])
    check("unseen: a place is not a count", unseen("no", "see main.lua:9")["numbers"], [])
    check("unseen: a version the quote lacks", unseen("tree-sitter v0.25", "tree-sitter v0.27")["numbers"],
          ["v0.27"])
    check("unseen: a file it says it ran", unseen("all passed", "run.lua passed")["files"], ["run.lua"])
    check("unseen: a file the quote names", unseen("run.lua: all passed", "run.lua passed")["files"], [])


def test_claim_head():
    """The claim-against-quote head scores five saved pairs as session B's classifier did, alone or
    beside other pairs, and a kept finding carries its score."""
    from rs3mcp import judge, research
    if judge.nli() is None:
        return
    cases = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim_holds_cases.json"),
                           encoding="utf-8"))
    answers = [judge.holds(c["quote"], c["claim"]) for c in cases]
    got = [a["holds"] for a in answers]
    check("claim head: five saved pairs score as the classifier printed them",
          [round(g, 3) for g in got], [round(c["holds"], 3) for c in cases])
    check("claim head: not called calibrated, since it wasn't on later rulings",
          {a["calibrated"] for a in answers}, {False})
    # the first pair again
    again = judge.holds(cases[0]["quote"], cases[0]["claim"])["holds"]
    check("claim head: a pair scores the same whatever was scored before it", again, got[0])
    with _ResearchStore():
        page = b"<html><body><p>Zed is licensed under the GPL, version 3 or later.</p></body></html>"
        research.fetch("https://zed.dev/about", "test", get=lambda u: page)
        kept = research.add({"claim": "Zed is GPL-3.0 or later", "url": "https://zed.dev/about",
                             "quote": "licensed under the GPL, version 3 or later"}, "test")
    check("claim head: a kept finding carries its holds score", 0 < kept.get("holds", -1) <= 1, True, str(kept))


def test_second_opinion():
    """The gate's second opinion: a claim the agent's own tool output shows scores high, one nothing
    shows scores low, and it only ever logs."""
    from rs3mcp import handback, judge
    if judge.nli() is None:
        return
    path = os.path.join(tempfile.mkdtemp(), "agent.jsonl")
    rows = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                           "input": {"command": "python -m pytest tests/test_prices.py"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": "tests/test_prices.py ...... all passed, no failures"}]}}]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    message = ("The price tests pass now.\n## Handback\nstatus: done\nran: python -m pytest tests/test_prices.py\n"
               "claim: the nightly cache rebuild ran and passed\n"
               "claim: prices.lua reads the cache before the market, prices.lua:12")
    got = handback.second_opinion(message, path)
    support = {c["claim"][:20]: c["support"] for c in got["claims"]}
    check("second opinion: a run the tool output shows scores high, one nothing shows scores low, a claim about "
          "code is left to the rules", (support.get("The price tests pass", 0) > 0.8,
                                         support.get("the nightly cache re", 1) < 0.2, "prices.lua reads the " in support),
          (True, True, False), str(support))
    empty = os.path.join(tempfile.mkdtemp(), "agent.jsonl")
    with open(empty, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "edited"}]}}) + "\n")
    none_run = handback.second_opinion("The plugin is complete and compiles cleanly.\n## Handback\nstatus: done", empty)
    check("second opinion: a claim of a run with nothing run at all scores zero",
          [(c["support"], c["from"]) for c in none_run["claims"]], [(0.0, "nothing was run")])


def test_labels():
    """The labelling pool's helpers: suggestions from plain rules only where the output says so, and the
    precision table counts flags against labels."""
    from rs3mcp import labels
    out = lambda text: [{"cmd": "python -m pytest", "text": text}]
    check("labels: suggestions follow the output, and stay silent when it says nothing plain",
          [(labels.suggest(c, o) or {}).get("value") for c, o in [
              ("the tests pass", out("12 passed in 0.4s")), ("the tests pass", out("10 passed, 2 failed")),
              ("the tests pass", out("Traceback (most recent call last):\n  ...")), ("it compiles", []),
              ("the cache rebuild works", out("done"))]],
          ["supported", "unsupported", "unsupported", "unsupported", None])
    fixed = [{"cmd": "lua54 test.lua", "text": "1 passed, 2 failed"}, {"cmd": "edit", "text": "ok"},
             {"cmd": "lua54 test.lua", "text": "3 passed, 0 failed"}, {"cmd": "cat test.lua", "text": "eq(a, b)"}]
    check("labels: the failing run before a fix does not outvote the passing run after it",
          [(labels.suggest("the check now passes", o) or {}).get("value") for o in (fixed, fixed[:1])],
          ["supported", "unsupported"])
    items = [{"nli": 0.02, "label": "unsupported"}, {"nli": 0.05, "label": "supported"}, {"nli": 0.3, "label": "unsupported"},
             {"nli": 0.9, "label": "supported"}, {"nli": 0.01, "label": "unclear"}, {"nli": 0.5, "label": None}]
    m = labels.measure(items)
    row = next(r for r in m["rows"] if r["threshold"] == 0.1)
    check("labels: below 0.1, two flags, one right; half the unsupported caught; unclear and unlabelled left out",
          (m["labelled"], row["flagged"], row["right"], row["precision"], row["recall"]), (4, 2, 1, 0.5, 0.5))


def test_all_registered():
    """A test_ function left out of TESTS passes by never running."""
    names = [n for n, v in globals().items() if n.startswith("test_") and callable(v)]
    check("every test_ function in this file is in TESTS", [n for n in names if globals()[n] not in TESTS], [])


# paraphrased tasks and their tools
FIND_TASKS = [
    ("does my lua file compile", ("rs3_lua_check",)), ("what drops does a monster have", ("rs3_wiki",)),
    ("which scripts set this varbit", ("rs3_script_refs",)), ("item id for rune platebody", ("rs3_lookup",)),
    ("mistakes we already made with pcall", ("rs3_lessons",)), ("show a markdown doc to the user", ("rs3_zone_show",)),
    ("open the sandbox and click a button", ("rs3_world_open", "rs3_world_act")),
    ("what changed in the latest game update", ("rs3_cache_diff",)), ("run sql on the cache", ("rs3_query",)),
    ("write a commit message", ("rs3_handover",)), ("find an interface by the text on it", ("rs3_iface",)),
    ("quest progress variable", ("rs3_quest_var",)), ("does this comment sound like an ai wrote it", ("rs3_comment_style",)),
    ("prettyui panel button layout", ("rs3_prettyui",)), ("hand a task to an agent", ("rs3_brief",)),
    ("lint plugin json before release", ("rs3_plugin_lint",)),
    ("decompiled clientscript source", ("rs3_script", "rs3_script_read")),
    ("type errors from the language server", ("rs3_lsp",)), ("record a settled decision", ("rs3_library_add",)),
    ("npcs and locs a quest uses", ("rs3_quest_entities",)),
]


def test_zone_ui():
    """The dev zone page: the hidden attribute holds against a class's display, and a proof's answer stays shown."""
    sandbox = os.path.join(ROOT, "sandbox")
    js = open(os.path.join(sandbox, "zone.js"), encoding="utf-8").read()
    css = open(os.path.join(sandbox, "zone.css"), encoding="utf-8").read()
    hidden = set(re.findall(r'class="([\w-]+)"[^>]*?\bhidden\b', js)) | set(
        re.findall(r'querySelector\("\.([\w-]+)"\)\.hidden\s*=', js))
    everywhere = re.search(r"(?m)^\[hidden\]\s*\{[^}]*display\s*:\s*none", css)
    beaten = sorted(c for c in hidden
                    if re.search(r"(?m)^\.%s\s*\{[^}]*\bdisplay\s*:\s*(?!none)" % re.escape(c), css)
                    and not everywhere
                    and not re.search(r"\.%s\[hidden\]\s*\{[^}]*display\s*:\s*none" % re.escape(c), css))
    check("zone: an element hidden by attribute stays hidden whatever display its class sets", beaten, [])
    prove = js[js.index('act === "prove"'):]
    prove = prove[:prove.index("} else if")]
    check("zone: a proof's answer is put back after the lesson reopens, which clears it",
          bool(re.search(r"await openLesson\(les\.id\);\s*les\.check = check;\s*drawEditor\(\);", prove)), True)


def _public_resolve(host, port, **kw):
    """Every host resolves to one public address, so the network guard lets a test fetch through."""
    return [(2, 1, 6, "", ("93.184.215.14", port))]


class _ResearchStore:
    """A throwaway research store for one test, RS3_RESEARCH pointed at it and put back after."""

    def __enter__(self):
        self.folder = tempfile.mkdtemp()
        self.was = os.environ.get("RS3_RESEARCH")
        os.environ["RS3_RESEARCH"] = self.folder
        return self.folder

    def __exit__(self, *exc):
        import shutil
        if self.was is None:
            del os.environ["RS3_RESEARCH"]
        else:
            os.environ["RS3_RESEARCH"] = self.was
        shutil.rmtree(self.folder, ignore_errors=True)


def test_research_store():
    """A page is fetched once: a second fetch of the same url, however written, reads the store; a
    refresh with a reason fetches again and says why."""
    from rs3mcp import research
    calls = []

    def get(url):
        calls.append(url)
        return b"<html><body><p>Zed renders with GPUI.</p></body></html>"
    with _ResearchStore():
        first = research.fetch("https://Zed.dev/docs/#top", "test", get=get)
        again = research.fetch("https://zed.dev/docs", "test", get=get)
        fresh = research.fetch("https://zed.dev/docs", "test", refresh="release page moved", get=get)
        try:
            research.fetch("https://zed.dev/x", "test", refresh=" ", get=get)
            empty_reason = "fetched"
        except ValueError:
            empty_reason = "refused"
    check("research: a url fetches once, its other spellings read the store, a refresh needs a reason",
          (len(calls), first["reused"], again["reused"], fresh["reused"], fresh.get("refresh"), empty_reason),
          (2, False, True, False, "release page moved", "refused"))
    check("research: urls normalise scheme and host case, fragment and trailing slash",
          research.normal("HTTPS://Zed.dev/docs/#top"), "https://zed.dev/docs")


def test_citation_check():
    """A finding is kept when its quote is on the stored page, or the NLI finds the claim there; a claim
    the page does not carry is refused."""
    from rs3mcp import judge, research
    page = (b"<html><head><script>var x = 1;</script></head><body><h1>Zed</h1><p>Zed draws its "
            b"interface with GPUI, a GPU&#8209;accelerated UI framework written in Rust.</p>"
            b"<p>It is licensed under the GPL, version 3 or later.</p></body></html>")
    with _ResearchStore():
        research.fetch("https://zed.dev/about", "test", get=lambda u: page)
        quoted = research.add({"claim": "Zed is GPL-3.0 or later", "url": "https://zed.dev/about",
                               "quote": "licensed under the GPL, version 3 or later"}, "test")
        wrong = research.check_finding({"claim": "Zed is MIT licensed", "url": "https://zed.dev/about",
                                        "quote": "licensed under the MIT license"})
        unfetched = None
        try:
            research.add({"claim": "x", "url": "https://nowhere.test/", "quote": "x"}, "test")
        except ValueError as e:
            unfetched = str(e)
        search = "https://api.github.com/search/issues?q=repo:microsoft/vscode+CRLF&per_page=50"
        research.fetch(search, "test", get=lambda u: page)
        from_search = None
        try:
            research.add({"claim": "Zed is GPL-3.0 or later", "url": search,
                          "quote": "licensed under the GPL, version 3 or later"}, "test")
        except ValueError as e:
            from_search = str(e)
        para = None
        if judge.nli() is not None:
            para = research.check_finding({"claim": "Zed's interface is drawn using the GPU",
                                           "url": "https://zed.dev/about",
                                           "quote": "renders everything on the graphics card"})["verdict"]
        paraphrase_kept = None
        try:
            research.add({"claim": "Zed's interface is drawn using the GPU", "url": "https://zed.dev/about",
                          "quote": "renders everything on the graphics card"}, "test")
            paraphrase_kept = "kept"
        except ValueError as e:
            paraphrase_kept = str(e)
    check("research: a quote on the page is quoted, script text is not page text",
          (quoted["verdict"], "var x" in research.page_text(page)), ("quoted", False))
    check("research: a claim the page does not carry is unsupported", wrong["verdict"], "unsupported")
    check("research: a finding about a page never fetched is refused", bool(unfetched and "fetch" in unfetched), True)
    check("research: a finding quoted from a search-results page is refused",
          bool(from_search and "search-results page" in from_search), True)
    if para is not None:
        check("research: a paraphrase the NLI finds on the page is supported", para, "supported")
    check("research: a finding whose quote is not word for word on the page is refused, however the NLI scores it",
          "word for word" in (paraphrase_kept or ""), True)


def test_research_find():
    """What is already known comes back first, for a question in other words, with what reuse saved."""
    from rs3mcp import research
    page = b"<p>The Language Server Protocol sends textDocument/didChange on every edit.</p>"
    with _ResearchStore():
        research.fetch("https://lsp.dev/spec", "test", get=lambda u: page)
        research.fetch("https://lsp.dev/spec", "test", get=lambda u: page)
        research.add({"claim": "LSP sends didChange on each edit", "url": "https://lsp.dev/spec",
                      "quote": "sends textDocument/didChange on every edit"}, "test")
        got = research.find("which LSP message tells the server a file was edited")
    check("research: a finding comes back for a question in other words, and reuse is counted",
          (got["known"][0]["claim"] if got["known"] else None, got["costs"]["reused"]),
          ("LSP sends didChange on each edit", 1))


def test_research_network():
    """Fetching is https only and public only, hop by hop: a redirect home, to plain http or round in
    circles is refused; a busy server is retried; a refresh of an unchanged page costs a 304; a stored
    copy changed on disk is refused."""
    from rs3mcp import research
    public = {"a.test": "93.184.215.14", "b.test": "93.184.215.15", "home.test": "127.0.0.1",
              "meta.test": "169.254.169.254", "lan.test": "192.168.1.9"}

    def resolve(host, port, **kw):
        ip = public.get(host, host)
        return [(2, 1, 6, "", (ip, port))]

    def refused(url):
        try:
            research.vet(url, resolve)
            return False
        except ValueError:
            return True
    check("research net: only https to public addresses",
          [refused(u) for u in ("https://a.test/x", "http://a.test/x", "https://home.test/", "https://meta.test/",
                                "https://lan.test/", "https://127.0.0.1/", "https://[::1]/", "ftp://a.test/")],
          [False, True, True, True, True, True, True, True])

    pages, asked = {}, []

    def transport(url, ip, headers):
        asked.append((url, ip, dict(headers)))
        step = pages[url]
        if callable(step):
            return step(headers)
        return step

    def download(url, **kw):
        return research.download(url, kw.get("headers"), transport=transport, resolve=resolve, sleep=lambda s: None)

    def outcome(url):
        try:
            got = download(url)
            return (got["status"], got["final"])
        except ValueError as e:
            return str(e).split(":")[0][:40]
    pages.update({
        "https://a.test/start": (302, {"location": "/moved"}, b""),
        "https://a.test/moved": (301, {"location": "https://b.test/page"}, b""),
        "https://b.test/page": (200, {"content-type": "text/html", "etag": '"v1"'}, b"<p>hi</p>"),
        "https://a.test/home": (302, {"location": "https://home.test/admin"}, b""),
        "https://a.test/plain": (302, {"location": "http://b.test/page"}, b""),
        "https://a.test/loop": (302, {"location": "https://a.test/loop"}, b""),
        "https://a.test/gone": (404, {}, b"no"),
    })
    got = [outcome(u) for u in ("https://a.test/start", "https://a.test/home", "https://a.test/plain",
                                "https://a.test/loop", "https://a.test/gone")]
    hop_ips = [ip for u, ip, h in asked if u in ("https://a.test/start", "https://b.test/page")]
    check("research net: redirects are followed and each hop vetted; home, plain http, loops and errors refused",
          (got[0], [isinstance(g, str) for g in got[1:]]), ((200, "https://b.test/page"), [True, True, True, True]))
    check("research net: the vetted address is the one connected to", hop_ips[:2], ["93.184.215.14", "93.184.215.15"])

    tries = []

    def busy(headers):
        tries.append(1)
        return (503, {"retry-after": "1"}, b"") if len(tries) < 3 else (200, {}, b"<p>ok</p>")
    pages["https://a.test/busy"] = busy
    check("research net: a busy server is asked again, then answers", (outcome("https://a.test/busy"), len(tries)),
          ((200, "https://a.test/busy"), 3))

    with _ResearchStore():
        pages["https://b.test/doc"] = (200, {"etag": '"v1"', "last-modified": "Sat, 26 Sep 2026 10:00:00 GMT"},
                                       b"<p>Zed is licensed under the GPL.</p>")
        first = research.fetch("https://b.test/doc", "test", transport=transport, resolve=resolve, sleep=lambda s: None)
        pages["https://b.test/doc"] = lambda h: ((304, {}, b"") if h.get("If-None-Match") == '"v1"' else
                                                 (200, {}, b"<p>changed</p>"))
        again = research.fetch("https://b.test/doc", "test", refresh="checking for a new version",
                               transport=transport, resolve=resolve, sleep=lambda s: None)
        before = research.costs()
        kept = research.check_finding({"claim": "Zed is GPL", "url": "https://b.test/doc",
                                       "quote": "licensed under the GPL"})["verdict"]
        with io.open(first["path"], "ab") as fh:
            fh.write(b"<p>and it is MIT too</p>")
        try:
            research.check_finding({"claim": "Zed is MIT", "url": "https://b.test/doc", "quote": "MIT"})
            tampered = "read"
        except ValueError as e:
            tampered = "refused" if "changed" in str(e) else str(e)
    check("research net: a refresh of an unchanged page is a 304 that keeps the stored copy",
          (again.get("unchanged"), again["sha1"] == first["sha1"], before["fetched"], before["unchanged"], kept),
          (True, True, 1, 1, "quoted"))
    check("research net: a stored copy changed on disk is refused, not quoted from", tampered, "refused")

    class Done:
        def __init__(self, code, err):
            self.returncode, self.stderr, self.stdout = code, err, b""
    real = research.subprocess.run
    kinds = []
    try:
        for code, err in ((35, b"curl: (35) schannel: SEC_E_CERT_EXPIRED (0x80090328) - The received certificate has expired."),
                          (60, b"curl: (60) schannel: SEC_E_UNTRUSTED_ROOT"),
                          (35, b"curl: (35) schannel: failed to receive handshake, SSL/TLS connection failed")):
            research.subprocess.run = lambda *a, **k: Done(code, err)
            try:
                research._curl_once("https://a.test/", "93.184.215.14", {})
            except research._Transient:
                kinds.append("retried")
            except ValueError:
                kinds.append("refused")
    finally:
        research.subprocess.run = real
    check("research net: a certificate that fails is refused at once, a dropped handshake is retried",
          kinds, ["refused", "refused", "retried"])


def test_research_cli():
    """The store works from a shell: add keeps a quoted finding and prints its id, refuses one the page
    does not carry, and find brings the kept one back."""
    from rs3mcp import research
    with _ResearchStore() as d:
        research.fetch("https://zed.dev/about", "test", get=lambda u: b"<p>Zed is licensed under the GPL.</p>")
        env = dict(os.environ, RS3_RESEARCH=d, RS3_WHO="test")

        def cli(*args):
            done = subprocess.run([sys.executable, "-m", "rs3mcp.research"] + list(args), cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=120)
            return done.returncode, done.stdout.strip()
        kept = cli("add", "--claim", "Zed is GPL", "--url", "https://zed.dev/about", "--quote", "licensed under the GPL")
        wrong = cli("add", "--claim", "Zed is MIT", "--url", "https://zed.dev/about", "--quote", "licensed under MIT")
        found = cli("find", "what licence does zed use")
    ident = json.loads(kept[1])["id"] if kept[0] == 0 else None
    check("research cli: add prints the kept finding's id, refuses an unsupported one, find returns it",
          (kept[0], wrong[0], wrong[1].startswith("refused"), bool(ident) and ident in found[1]),
          (0, 1, True, True))


def test_research_review():
    """A reviewed finding holds only while its latest review says so: an overclaim is sent back by the
    gate, pointing at the better finding, and the better finding passes; code quotes match whatever
    the spacing, and inline tags do not split words."""
    from rs3mcp import handback, research
    page = (b"<html><body><p>The sync kinds are <code><span>None</span>=<span>0</span></code>, and "
            b"<code><span>Full</span>=<span>1</span></code>.</p><p>Full means the client always sends the "
            b"full content of the document.</p></body></html>")
    with _ResearchStore():
        research.fetch("https://lsp.test/spec", "test", get=lambda u: page)
        spaced = research.check_finding({"claim": "None is 0", "url": "https://lsp.test/spec", "quote": "None = 0"})
        narrow = research.add({"claim": "Full is 1, and means the client sends the whole document every time",
                               "url": "https://lsp.test/spec", "quote": "Full=1"}, "researcher")
        better = research.add({"claim": "Full means the client always sends the whole document",
                               "url": "https://lsp.test/spec",
                               "quote": "Full means the client always sends the full content of the document"}, "researcher")
        shown = research.show(narrow["id"])
        research.review(narrow["id"], "overclaims", "the quote gives the value, not what Full means", "checker",
                        better=better["id"])
        research.review(better["id"], "holds", "the quote says it outright", "checker")
        refusals = []
        for bad in (("nosuchid", "holds"), (narrow["id"], "maybe")):
            try:
                research.review(bad[0], bad[1], "x", "checker")
            except ValueError:
                refusals.append(bad[1])
        sent_back = research.gate(handback.parse("## Handback\nstatus: done\nfinding: %s\n" % narrow["id"]))
        passes = research.gate(handback.parse("## Handback\nstatus: done\nfinding: %s\n" % better["id"]))
        checker = [research.gate(handback.parse("## Handback\nstatus: done\nreviewed: %s\n" % line))
                   for line in ("%s overclaims" % narrow["id"], "%s holds" % narrow["id"], "nosuchid holds")]
    check("research review: code quotes match whatever the spacing, and inline tags do not split words",
          (spaced["verdict"], "None=0" in research.page_text(page)), ("quoted", True))
    check("research review: show gives the claim, the quote and the page round it",
          (shown["claim"] == narrow["claim"], "Full=1" in shown["context"]), (True, True))
    check("research review: an unknown finding or verdict is refused", refusals, ["holds", "maybe"])
    check("research review: an overclaim is sent back naming the better finding, which passes",
          (len(sent_back), better["id"] in (sent_back[0] if sent_back else ""), passes), (1, True, []))
    check("research review: a checker's reviewed lines must match the store's rulings",
          [len(c) for c in checker], [0, 1, 1])

    with _ResearchStore():
        research.fetch("https://lsp.test/spec", "test", get=lambda u: page)
        first = research.add({"claim": "None is 0", "url": "https://lsp.test/spec", "quote": "None=0"}, "researcher")
        same = research.add({"claim": "None is 0", "url": "https://lsp.test/spec", "quote": "None=0"}, "researcher")
        try:
            research.add({"claim": "None is 0", "url": "https://lsp.test/spec", "quote": "sync kinds are None=0"}, "checker")
            clash = "kept"
        except ValueError:
            clash = "refused"
        rows = [r for r in research._rows(research._findings()) if r["id"] == first["id"]]
    check("research review: the same finding again is the one already kept; a new quote on the same claim is "
          "refused rather than replacing it", (same["id"] == first["id"], clash, len(rows), rows[-1]["quote"]),
          (True, "refused", 1, "None=0"))


def test_research_source_is_not_html():
    """A source file that builds html strings is still source: text/plain is read as it is, so code after
    a generic or a comparison is not stripped as markup, and a real page is still read as html."""
    from rs3mcp import research
    ts = (b"const html = '<div class=\"view-line\">';\nfunction f(a: Map<number, string>) {\n"
          b"  if (a.size < 3 && b > 2) {\n    // no need to do anything, we have the same render input\n  }\n}\n")
    page = b"<!doctype html><html><body><p>Hello <b>there</b></p><script>x()</script></body></html>"

    def serve(body, kind):
        return lambda url, ip, headers: (200, {"content-type": kind}, body)

    resolve = _public_resolve
    with _ResearchStore():
        research.fetch("https://raw.test/viewLine.ts", "test", transport=serve(ts, "text/plain; charset=utf-8"),
                       resolve=resolve, sleep=lambda s: None)
        research.fetch("https://page.test/", "test", transport=serve(page, "text/html"), resolve=resolve,
                       sleep=lambda s: None)
        code = research.check_finding({"claim": "the line is reused when nothing changed", "url": "https://raw.test/viewLine.ts",
                                       "quote": "no need to do anything, we have the same render input"})["verdict"]
        generic = research.check_finding({"claim": "f takes a map", "url": "https://raw.test/viewLine.ts",
                                          "quote": "f(a: Map<number, string>)"})["verdict"]
        html_page = research.page_text(research._body(research.stored("https://page.test/")),
                                       research.stored("https://page.test/").get("type"))
    check("research: source text is read as text, markup-looking strings and generics included",
          (code, generic), ("quoted", "quoted"))
    check("research: a real page is still read as html", html_page, "Hello there")


def test_research_json_reads_as_text():
    """An API answer is read for its strings: a quote with double quotes or across a paragraph break
    matches as a reader sees it, and one copied with the JSON's backslashes still matches."""
    from rs3mcp import research
    body = json.dumps({"number": 1, "body": 'The lexer "looks ahead" less.\r\n\r\nSo a token is kept.'}).encode()
    url = "https://api.test/issues/one"
    with _ResearchStore():
        research.fetch(url, "test", resolve=_public_resolve, sleep=lambda s: None,
                       transport=lambda u, ip, headers: (200, {"content-type": "application/json; charset=utf-8"}, body))
        plain = research.check_finding({"claim": "c", "url": url, "quote": 'The lexer "looks ahead" less. So a token'})
        escaped = research.check_finding({"claim": "c", "url": url, "quote": 'The lexer \\"looks ahead\\" less.'})
    check("research: a JSON page is read for its strings, and old escaped quotes still match",
          (plain["verdict"], escaped["verdict"]), ("quoted", "quoted"))


def test_research_gate():
    """A researcher's handback holds only when every finding id is kept and quoted or supported, and it
    makes no claim outside a finding."""
    from rs3mcp import handback, research
    with _ResearchStore():
        research.fetch("https://zed.dev/about", "test", get=lambda u: b"<p>Zed is licensed under the GPL.</p>")
        kept = research.add({"claim": "Zed is GPL", "url": "https://zed.dev/about",
                             "quote": "licensed under the GPL"}, "test")
        good = handback.parse("## Handback\nstatus: done\nfinding: %s\nfetched: https://zed.dev/about/\n" % kept["id"])
        bad = handback.parse("## Handback\nstatus: done\nfinding: nosuchid\nclaim: Zed is fast, zed.dev\n"
                             "fetched: https://zed.dev/never\n")
        got = (research.gate(good), research.gate(bad))
    check("research gate: kept findings and stored fetches pass; an unknown id, a bare claim and a fetch "
          "the store never made are sent back", (got[0], len(got[1])), ([], 3))
    check("research gate: a fetched line is read as a fetch, never as a command under ran",
          (good["fetched"], good["ran"]), (["https://zed.dev/about/"], []))
    bare = handback.parse("## Handback\nstatus: done\nreviewed: %s holds\n0123456789\nabcdef0123\n" % kept["id"])
    with _ResearchStore():
        unkeyed = research.gate(bare)
    check("research gate: an id on a line with no key is sent back, not silently skipped",
          (bare["unkeyed"], len(unkeyed)), (["0123456789", "abcdef0123"], 3))
    with _ResearchStore():
        nothing = [research.gate(handback.parse("## Handback\nstatus: done\nfetched: %s\n" % v))
                   for v in ("none", "None.", "-", "nothing fetched")]
    check("research gate: saying nothing was fetched is not a url to look for", nothing, [[], [], [], []])


def test_gateway():
    """Compact mode: three tools listed small, find routes real tasks, run dispatches as a direct call does."""
    from rs3mcp import gateway, howto
    from rs3mcp.common import _run_server
    srv = _run_server()
    was = os.environ.get("RS3_TOOLSETS")
    os.environ["RS3_TOOLSETS"] = "compact"
    try:
        listed = srv["listing"]()
        size = len(json.dumps({"tools": listed}, separators=(",", ":")))
        brief = srv["handle"]({"method": "initialize", "params": {}})["instructions"]
    finally:
        if was is None:
            del os.environ["RS3_TOOLSETS"]
        else:
            os.environ["RS3_TOOLSETS"] = was
    listing_max, brief_max = 1500, 2000
    check("gateway: compact lists find, how and run, listing and instructions each under their cap",
          ([t["name"] for t in listed], size < listing_max, len(brief) < brief_max),
          (["rs3_find", "rs3_how", "rs3_run"], True, True), "%d, %d" % (size, len(brief)))

    top = lambda task: [t["tool"] for t in gateway.tool_find({"task": task})["tools"]]
    missed = [flow[0] for flow in howto._all_flows()
              if next((t for t, _ in flow[1] if t), None) not in top(flow[0]) and any(t for t, _ in flow[1])]
    check("gateway: every flow's task finds its first tool in the top five", missed, [])
    check("gateway: tasks in other words find their tool in the top five",
          [task for task, want in FIND_TASKS if not set(want) & set(top(task))], [])

    call = lambda name, args: srv["handle"]({"method": "tools/call", "params": {"name": name, "arguments": args}})
    direct = call("rs3_how", {"tool": "rs3_lookup", "pick": "inputSchema.required"})
    ran = call("rs3_run", {"tool": "rs3_how", "args": {"tool": "rs3_lookup", "pick": "inputSchema.required"}})
    check("gateway: rs3_run answers as the direct call, pick included",
          (ran, "isError" in ran, '"value"' in ran["content"][0]["text"]), (direct, False, True))


def test_memo():
    """The selftest's memory skips a test while what it ran is unchanged, and only then."""
    import time
    from rs3mcp.test import memo
    src = os.path.join(tempfile.mkdtemp(), "subject.py")
    body = "LIMIT = 3\n\ndef used():\n    return LIMIT\n\ndef other():\n    return 1\n"
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(body)
    shape = memo._shape(src)
    saved, argv = memo._memo, list(sys.argv)
    sys.argv[:] = [a for a in sys.argv if a != "--full"]
    try:
        memo._memo = {"t": {"passed": True, "checks": 4, "files": {src: {
            "stamp": memo._stamp(src), "funcs": {"used": shape["funcs"]["used"]},
            "names": {"LIMIT": shape["names"]["LIMIT"]}}}}}
        results = [memo.carried("t")]
        for edit in (("return 1", "return 2"), ("return LIMIT", "return LIMIT + 1"), ("LIMIT = 3", "LIMIT = 4")):
            time.sleep(0.01)
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(body.replace(*edit))
            results.append(memo.carried("t"))
        memo._memo["t"]["passed"] = False
        results.append(memo.carried("t"))
    finally:
        memo._memo, sys.argv[:] = saved, argv
    check("memo: unchanged is carried, an unused function's edit too; a used function, a constant it "
          "reads, or a failure runs it again", results, [4, 4, None, None, None])

    import types
    fake = types.ModuleType("memo_fake")
    fake.__file__ = src
    sys.modules["memo_fake"] = fake
    reader = os.path.join(os.path.dirname(src), "reader.py")
    with open(reader, "w", encoding="utf-8") as fh:
        fh.write("def read():\n    return memo_fake.LIMIT\n")
    scope = {}
    with open(reader, encoding="utf-8") as fh:
        exec(compile(fh.read(), reader, "exec"), scope)
    try:
        # what stop() keeps for code that ran
        seen = memo._files(set(), [scope["read"].__code__], kept=lambda p: True).get(os.path.abspath(src))
    finally:
        del sys.modules["memo_fake"]
    check("memo: a module never run or opened, whose constant the test read by name, is still watched",
          sorted((seen or {}).get("names", {})), ["LIMIT"])

    nested = os.path.join(os.path.dirname(src), "nested.py")
    with open(nested, "w", encoding="utf-8") as fh:
        fh.write("def outer():\n    def inner():\n        return 1\n    return inner()\n\ndef other():\n    return 2\n")
    scope = {}
    with open(nested, encoding="utf-8") as fh:
        exec(compile(fh.read(), nested, "exec"), scope)
    outer = scope["outer"].__code__
    inner = next(c for c in outer.co_consts if isinstance(c, types.CodeType))
    kept = memo._files(set(), [outer, inner], kept=lambda p: True).get(os.path.abspath(nested)) or {}
    check("memo: a def nested in a test is the test's, so its file is kept by function, not whole",
          sorted(kept.get("funcs") or {}), ["outer"])


def test_maprender():
    """The minimap drawing: the shape tables hold together, and a square draws at 4 pixels a tile."""
    from rs3mcp import cachefile, maprender
    rot = maprender.ROTATIONS
    check("maprender: every quarter turn moves each pixel somewhere once",
          [sorted(r) == list(range(16)) for r in rot], [True] * 4)
    once = rot[1]
    check("maprender: four quarter turns come back to the start",
          [once[once[once[once[k]]]] for k in range(16)], list(range(16)))
    check("maprender: shape 0 covers the whole tile at every rotation",
          [all(maprender.SHAPES[0][r[k]] for k in range(16)) for r in rot], [True] * 4)
    check("maprender: twelve shapes of sixteen pixels", [len(s) for s in maprender.SHAPES], [16] * 12)

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        from PIL import Image
        out = os.path.join(tempfile.mkdtemp(), "lumbridge.png")
        maprender.render(50, 50, 50, 50, out, zoom=1)
        img = Image.open(out)
        colours = len(set(img.getdata()))
        check("maprender: Lumbridge's square draws four pixels a tile, in more than flat colours",
              (img.size, colours > 500), ((256, 256), True))
        cfg = maprender._Configs()
        check("maprender: tile floor ids count from 1: the keep's dining hall is the wood brown of the "
              "config one below its tile id, and the water cut draws nothing",
              (cfg.colour("overlays", 273), cfg.colour("overlays", 42)), ((102, 70, 46), None))
        check("maprender: a Lumbridge swamp pool is teal, the River Lum grey-blue",
              (cfg.water(3206, 3182), cfg.water(3239, 3238)), (maprender.SWAMP, maprender.WATER))


def test_maplocs():
    """Map placements: id deltas, packed positions, the extras a flags byte names, and live squares."""
    import struct
    from rs3mcp import cachefile, maplocs

    # a tailed loc id, then a bare one
    body = bytes([0xFF, 0xFF, 0x00])
    at = (1 << 12) | (5 << 6) | 9
    body += struct.pack(">H", 0x8000 | (at + 1)) + bytes([(10 << 2) | 3])
    body += bytes([2]) + bytes([0x80 | (4 << 2) | 1, 8]) + struct.pack(">h", -22)
    body += bytes([0])
    body += bytes([1]) + bytes([1, 0, 0]) + bytes([0])
    got = maplocs.decode(body)
    check("maplocs: a tailed id adds its chunks", [lid for lid, _ in got], [32766, 32767])
    check("maplocs: plane, x and y unpack, and a placement moves on from the last",
          [u[:5] for u in got[0][1]], [(1, 5, 9, 10, 3), (1, 5, 10, 4, 1)])
    check("maplocs: the flags byte names the extras", got[0][1][1][5], {"translateZ": -22})
    try:
        maplocs.decode(body + b"\x00")
        check("maplocs: a stray byte is refused", "decoded", "Stop")
    except maplocs.Stop:
        check("maplocs: a stray byte is refused", "Stop", "Stop")

    if os.path.exists(os.path.join(cachefile.GAME, "js5-5.jcache")):
        bad = []
        for k, (x, z, data) in enumerate(maplocs.squares()):
            if k % 25:
                continue
            try:
                maplocs.decode(data)
            except (maplocs.Stop, IndexError, struct.error) as e:
                bad.append((x, z, str(e)))
        check("maplocs: live squares decode to their last byte", bad[:3], [])


def test_big_model():
    """A model too big for short indices: they are big endian ints, and its counts wrap."""
    from rs3mcp import cachefile, models
    if not os.path.exists(os.path.join(cachefile.GAME, "js5-47.jcache")):
        return
    m = models.mesh(129744)
    check("models: the one with nine wrapped meshes decodes", m is not None, True)
    if m:
        check("models: and every index names one of its vertices",
              max(max(t[:3]) for t in m["tris"]) < len(m["verts"]), True)


def test_sprite_names():
    """A sprite's name hash is Java's String.hashCode, and families are swept from one name."""
    from rs3mcp import spritenames as N
    check("sprite names: hashCode, as the sprite index keeps it", N.name_hash("hitsplat"), 2992884335)
    family = [(1, N.name_hash("rune_7_icon")), (2, N.name_hash("rune_12_icon")), (3, N.name_hash("rune_12_icon_2"))]
    got = N.crack(family, ["RUNE_7_ICON"])
    check("sprite names: a word matches in any case, and its family is swept",
          got, {1: "rune_7_icon", 2: "rune_12_icon", 3: "rune_12_icon_2"})


def test_textures():
    """The dds copy of a texture decodes close to the raw bmp copy, where both are on disk."""
    from rs3mcp import cachefile, textures
    if not os.path.exists(os.path.join(cachefile.GAME, "js5-52.jcache")):
        return
    con = cachefile._open(cachefile.GAME, textures.BMP)
    stored = sorted(r[0] for r in con.execute("select KEY from cache")) if con else []
    normals = textures.normal_maps()
    # a normal map's dds copy needs rebuilding
    wanted = {True: 3 if normals else 0, False: 3}
    for tex in stored[::97]:
        kind = tex in normals
        if not wanted[kind]:
            continue
        a, b = textures.bmp(tex), textures.dds(tex)
        if not a or not b or a[:2] != b[:2]:
            continue
        pa, pb = a[2], b[2]
        err = sum(abs(pa[i] - pb[i]) for i in range(len(pa)) if i % 4 != 3) / (0.75 * len(pa))
        check("textures: dds %d decodes close to its bmp%s" % (tex, " (normal map)" if kind else ""),
              err < 12, True, "%.1f" % err)
        wanted[kind] -= 1
        if not any(wanted.values()):
            break
    check("textures: normal maps were compared", wanted[True], 0)


def test_docs():
    """Every page is in the index with its navigation current, and every link lands.

    The tool index linked #cache-diff for a heading whose anchor is cache_diff, and a tool named like
    its group to the group rather than the tool, and nothing said so.
    """
    from rs3mcp import docs
    folder = docs.FOLDER
    if not os.path.isdir(folder):
        return
    order = docs.pages(folder)
    listed = {p[0] for p in order}
    on_disk = {f for f in os.listdir(folder) if f.endswith(".md") and f != docs.INDEX}
    check("docs: every page is in the index", sorted(on_disk - listed), [])
    anchors = {}
    for f in on_disk | {docs.INDEX}:
        with io.open(os.path.join(folder, f), encoding="utf-8") as fh:
            anchors[f] = fh.read()
    stale = [f for f in sorted(on_disk) if docs.with_nav(f, anchors[f], order) != anchors[f]]
    check("docs: navigation is current (rs3 docs writes it)", stale, [])
    broken = []
    for f, text in sorted(anchors.items()):
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        for target, frag in re.findall(r"\]\(([\w-]+\.md)?(?:#([\w-]+))?\)", prose):
            page = target or f
            if page not in anchors:
                broken.append("%s -> %s" % (f, page))
            elif frag and frag not in {h[2] for h in docs.headings(anchors[page])}:
                broken.append("%s -> %s#%s" % (f, page, frag))
    check("docs: every link lands", broken[:8], [])

    # a script anyone can run is documented
    reached = {"rs3.py": "the launcher behind rs3",
               "correctness.py": "run by rs3-mcp.py --test", "golden.py": "run by rs3-mcp.py --golden and --diff"}
    # an add-on's scripts are documented with it
    from rs3mcp import addons
    for offered in addons.every("scripts"):
        reached.update(offered)
    commands = anchors.get("commands.md", "")
    unlisted = []
    for folder in (docs.BASE, os.path.join(docs.BASE, "rs3mcp"), os.path.join(docs.BASE, "rs3mcp", "test")):
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".py") or f in reached:
                continue
            with io.open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                if '__name__ == "__main__"' not in fh.read():
                    continue
            stem = f[:-3]
            if f not in commands and "rs3mcp." + stem not in commands:
                unlisted.append(f)
    check("docs: every runnable script is on the commands page", unlisted, [])


def test_wiki_offline():
    """A wiki that cannot be reached is not a page that does not exist.

    A fresh Windows failed its first fetch, the page was stored as missing, and every ask after
    that said "no such page" with the network fine.
    """
    import shutil
    import tempfile
    from rs3mcp import wiki
    tmp = tempfile.mkdtemp()
    saved = wiki.STORE, wiki._get, dict(wiki._DB)
    try:
        wiki.STORE = os.path.join(tmp, "wiki.db")
        wiki._DB.clear()

        def down(params):
            wiki._ERR[0] = "certificate verify failed"
            return None
        wiki._get = down
        got = wiki.tool_wiki({"page": "Abyssal demon"})
        check("wiki: unreachable says so", got.get("wikiUnreachable"), "certificate verify failed")
        check("wiki: and stores nothing", wiki._db().execute("select count(*) from page").fetchone()[0], 0)

        def up(params):
            wiki._ERR[0] = None
            return {"query": {"pages": {"1": {"revisions": [{"slots": {"main": {"*": "hello"}}}]}}}}
        wiki._get = up
        check("wiki: the next ask gets the page", wiki.tool_wiki({"page": "Abyssal demon"}).get("chars"), 5)
    finally:
        if "conn" in wiki._DB:
            wiki._DB["conn"].close()
        wiki.STORE, wiki._get = saved[0], saved[1]
        wiki._DB.clear()
        wiki._DB.update(saved[2])
        shutil.rmtree(tmp, ignore_errors=True)


def test_dds_rounding():
    """DXT1's in-between colours round the way Microsoft's DXT1 page gives them, (2a + b + 1) / 3.

    Floored, a channel where 2a + b leaves 2 over a multiple of 3 came out a shade dark."""
    from rs3mcp import textures
    from rs3mcp.test import dds_colour
    real = textures._file
    try:
        # red on every color_2, then on color_3
        for block, want, which in (("00f80008aaaaaaaa", 173, "color_2"), ("00f80010ffffffff", 96, "color_3")):
            textures._file = lambda major, tex, root, b=bytes.fromhex(block): dds_colour.dds_file(b)
            got = textures.dds(0)
            check("dds rounding: %s rounds to nearest" % which, got and got[2][0], want)
    finally:
        textures._file = real


def test_kept_numbers():
    """An update that kept its numbers is saved, and a shuffled one is not filled from the old map.

    The first real update matched all 1,241 ops it placed and still went unsaved over two scripts."""
    from rs3mcp import cscalibrate as C
    current = {str(n): {"op": 1000 + n, "imm": "byte"} for n in range(10)}
    kept = {n: (1000 + n, "byte") for n in range(8)}
    check("kept numbers: all placed agree, so the rest come from the map", C.kept_numbers(kept, current), True)
    check("kept numbers: and the two it could not place are filled", sorted(kept), list(range(10)))
    moved = {n: (1000 + n, "byte") for n in range(8)}
    moved[3] = (1004, "byte")
    check("kept numbers: one op in a new place means a shuffle", C.kept_numbers(moved, current), False)
    check("kept numbers: and nothing is filled", len(moved), 8)
    check("kept numbers: too few placed to judge", C.kept_numbers({0: (1000, "byte")}, current), False)


def test_script_names():
    """A script name is cracked from its neighbours only when one candidate fits and its kind agrees."""
    from rs3mcp import scriptnames as N
    check("script names: the hash splits at any point",
          N.jhash("_offercount_10]", N.jhash("[clientscript,stock")), N.jhash("[clientscript,stock_offercount_10]"))
    s = N.Scripts.__new__(N.Scripts)
    s.calls, s.ifaces, s.iface_name = {}, {}, {}
    s.called, s.hooked = {12}, {11, 13}
    names = {10: "[clientscript,stock_offercount_1]", 14: "[clientscript,stock_offerprice_market]"}
    table = N._suffixes(["offercount_10", "offerprice", "10", "search"])
    want = {11: N.jhash("[clientscript,stock_offercount_10]"), 12: N.jhash("[proc,stock_search]"),
            13: 12345}
    got = N.crack(want, names, s, table)
    check("script names: cracked from the stems of its neighbours",
          (got.get(11), got.get(12)), ("[clientscript,stock_offercount_10]", "[proc,stock_search]"))
    check("script names: a hash nothing fits gets no name", 13 in got, False)
    check("script names: a proc name on a hooked script is contradicted",
          s.kind_fits(11, "[proc,stock_offercount_10]"), False)
    check("script names: and a script neither called nor hooked is not judged",
          s.kind_fits(99, "[proc,x]"), None)

    import shutil
    from rs3mcp import addons
    tmp = tempfile.mkdtemp()
    real = addons.first
    try:
        path = os.path.join(tmp, "a-scripts.txt")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write("[proc,compare_int]\nnot a name\n[clientscript,gone_from_this_build]\n")
        addons.first = lambda name: [path] if name == "script_name_lists" else real(name)
        got = N.listed_names({463: N.jhash("[proc,compare_int]"), 5: 77})
        check("script names: a list names a script by its hash, and nothing else", got, {463: "[proc,compare_int]"})
    finally:
        addons.first = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_likeliest():
    """Two readings that both simulate: the one whose own op pairs the cache uses wins."""
    import collections
    from rs3mcp import cscalibrate as C
    new = C.NEW_OP + 1
    common = [(1, 0), (new, 0), (2, 0), (3, 0)]
    odd = [(1, 0), (new, 0), (9, 0), (3, 0)]
    seen = collections.Counter({(1, 2): 0, (2, 3): 500, (9, 3): 4})
    got = C._likeliest([({1: "byte"}, common), ({1: "int"}, odd)], seen)
    check("likeliest: the reading with the common pairs wins", [g for g, _c in got], [{1: "byte"}])
    seen[(9, 3)] = 100
    check("likeliest: too close to call keeps both",
          len(C._likeliest([({1: "byte"}, common), ({1: "int"}, odd)], seen)), 2)
    check("likeliest: nothing of their own keeps both",
          len(C._likeliest([({1: "byte"}, common), ({1: "int"}, list(common))], seen)), 2)
    from rs3mcp import csstack as st
    check("likeliest: the effect search reaches five int pops, which a byte reading of a beta op needs",
          max(sum(1 for t in pops if t == st.I) for pops, _ in C._effects()), 5)


def test_stack_context_sets_aside_unread():
    """A rebuild after the 2026-10-21 update died in calibration: its trial map left 47 scripts
    unread, and the stack check read every script with it. The trial sets those aside; a finished
    map still has to read them all."""
    import collections
    import json as js
    import shutil
    from rs3mcp import clientscript as cs, csstack as st

    raw = js.load(io.open(cs.OPMAP, encoding="utf-8"))
    opmap = {int(k): (v["op"], v["imm"]) for k, v in raw["ops"].items()}
    used = collections.Counter()
    for _sid, data in cs.scripts():
        try:
            used.update(op for op, _ in cs.read(data, opmap)["code"])
        except cs.Bad:
            pass
    by_name = {v[0]: k for k, v in opmap.items()}
    rare = next((op for op, n in sorted(used.items(), key=lambda x: x[1]) if 3 <= n <= 40 and op in by_name), None)
    if rare is None:
        check("stack context: the cache has an opcode few scripts use", None, "one")
        return
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "opmap.json")
    js.dump(dict(raw, ops={k: v for k, v in raw["ops"].items() if int(k) != by_name[rare]}),
            io.open(path, "w", encoding="utf-8"))
    try:
        st.Context(path)
        raised = False
    except cs.Bad:
        raised = True
    check("stack context: a finished map that misses an opcode still raises", raised, True)
    ctx = st.Context(path, skip_unread=True)
    check("stack context: a trial map sets those scripts aside", (len(ctx.unread) > 0, len(ctx.code) > 20000), (True, True),
          "%d aside, %d read" % (len(ctx.unread), len(ctx.code)))
    shutil.rmtree(folder, ignore_errors=True)


def test_stack_ties_two_unknowns():
    """The second crash after the 2026-10-21 update: a script with two ops the map lacked reached
    the stack check with a two-unknown guess, and its loop unpacked None. Two rare ops that share a
    script, taken out of this cache's map, have to come back with their widths."""
    import collections
    from rs3mcp import clientscript as cs, cscalibrate as C

    full = cs.load_map()
    new = dict(cs.scripts())
    uses, together = collections.Counter(), collections.defaultdict(set)
    for sid, raw in new.items():
        nums = {(raw[p] << 8) | raw[p + 1] for p in C._opcode_fields(raw, full) or []}
        uses.update(nums)
        for n in nums:
            together[n].add(sid)
    rare = sorted((n for n, k in uses.items() if 2 <= k <= 6), key=lambda n: uses[n])
    pair = next(((a, b) for i, a in enumerate(rare) for b in rare[i + 1:] if together[a] & together[b]), None)
    if pair is None:
        check("stack ties: the cache has two rare ops in one script", None, "a pair")
        return
    known = {n: v for n, v in full.items() if n not in pair}
    C.stack_ties(known, {n: n for n in known}, new, say=lambda msg: None)
    check("stack ties: two unknowns in one script come back with their widths",
          {n: (known.get(n) or (None, None))[1] for n in pair}, {n: full[n][1] for n in pair})


def test_calibration():
    """An update's opcode numbers, got back from a scrambled copy of this cache.

    A third of the scripts, every number moved, some scripts edited or gone, two ops the map never
    had. Everything has to come back exactly, and the saved map has to keep each op's effects."""
    import shutil
    from rs3mcp import cachefile, clientscript as cs, cscalibrate as C

    ref_ops = cs.load_map()
    head, ops, blobs = C.load_reference()
    sample = sorted(blobs)[::97]
    check("calibration: the reference reads with its own numbers",
          sum(1 for sid in sample if C._opcode_fields(blobs[sid], ops) is None), 0)
    if not os.path.exists(os.path.join(cachefile.GAME, "js5-12.jcache")) or ops != ref_ops:
        return
    ref = {sid: blobs[sid] for sid in sorted(blobs)[::3]}
    new, truth, did = C._scramble(ref_ops, ref, seed=11, changed=0.03, dropped=5)
    known, pairs, report = C.calibrate(ref_ops, ref, new, say=lambda msg: None)
    used = set()
    for raw in new.values():
        used.update((raw[p] << 8) | raw[p + 1] for p in C._opcode_fields(raw, truth) or [])
    check("calibration: every number in use comes back", sorted(n for n in used if known.get(n) != truth[n]), [])
    check("calibration: and every script reads with them", report["unread"], 0)
    check("calibration: the two new ops are found, with their widths",
          sorted(v for n, v in known.items() if v[0] >= C.NEW_OP),
          sorted(v for v in truth.values() if v[0] >= C.NEW_OP))

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "opmap.json")
        shutil.copyfile(cs.OPMAP, path)
        C.save_map(known, pairs, path=path)
        was = json.load(io.open(cs.OPMAP, encoding="utf-8"))["ops"]
        now = json.load(io.open(path, encoding="utf-8"))["ops"]
        kept = [n for n, old in pairs.items() if old is not None and now[str(n)] != was[str(old)]]
        check("calibration: a saved op keeps its effects and types", kept, [])
        check("calibration: a new op is saved with its effects unknown",
              sorted({(now[str(n)]["in"], now[str(n)]["out"]) for n, old in pairs.items() if old is None}),
              [(None, None)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- cross-tool
def test_cross_tool():
    from rs3mcp.vars import tool_var
    from rs3mcp.cache import tool_lookup
    from rs3mcp.iface import tool_iface

    # a varbit's base must resolve on its own
    v = tool_var({"id": "-49770"})
    base = (v.get("base") or {}).get("id")
    check("cross: capsule cooldown base id", base, 10064)
    b = tool_var({"id": str(base)})
    check("cross: and it packs the varbit back", any(s["varbit"] == -49770 for s in (b.get("packs") or [])), True)
    if needs("var names across tools", "name lists"):
        check("cross: that base resolves by id", b.get("name"), "EGWD_REWARDS_VARP2")

    # a packed component id resolves to itself
    c = tool_iface({"id": "1188:5"})
    packed = c.get("packedID")
    again = tool_iface({"id": str(packed)})
    check("cross: packed id round-trips through the tool", again.get("at"), "1188:5")

    # a looked up item carries the cache's id
    it = tool_lookup({"kind": "item", "name": "Abyssal whip"})
    ids = [r["id"] for r in (it.get("results") or it.get("matches") or [])] \
        if isinstance(it, dict) else []
    check("cross: abyssal whip resolves", 4151 in ids or it.get("id") == 4151, True)


def test_regressions():
    # every tool the startup instructions name exists
    from rs3mcp import howto
    from rs3mcp.common import registry
    known = set(registry())
    dangling = [t for t in howto._tools_named() if t not in known]
    check("startup instructions name only real tools", dangling, [])
    check("  and cover the checks that gate a claim",
          all(w in howto.instructions() for w in ("--selftest", "rs3_drift", "soak.py")), True)

    # the part Claude Code keeps
    text = howto.instructions()
    head = text[:howto.CLIENT_CUT]
    check("instructions: the index fits the client's cut", howto.DETAILS in head, True)
    check("instructions: and names every task",
          [f[0] for f in howto._all_flows() if f[0] not in head], [])
    check("instructions: and says where the rest is", "rs3://guide/tools" in head, True)

    # no source file carries a mangled escape
    files = glob.glob(os.path.join(ROOT, "rs3mcp", "*.py")) \
        + glob.glob(os.path.join(ROOT, "rs3mcp", "test", "*.py")) \
        + glob.glob(os.path.join(ROOT, "rs3-mcp.py"))
    check("regress: the escape scan covers the tests too", len(files) > 20, True)
    ctrl = []
    for f in files:
        t = io.open(f, encoding="utf-8").read()
        if any(ord(ch) < 9 or 13 < ord(ch) < 32 for ch in t):
            ctrl.append(os.path.basename(f))
    check("regress: no mangled escapes in source", ctrl, [])


# --------------------------------------------------------------------------- script token index
def test_script_index():
    import rs3mcp.script as S

    def full_scan(pattern):
        q = pattern.lower().encode()
        out = set()
        for f in S._files():
            raw = io.open(os.path.join(S.CLIENTSCRIPT, f), "rb").read()
            if q in raw.lower():
                out.add(f)
        return out

    # SETHIDE lives inside IF_SETHIDE, so substrings count
    for pat in ("SETHIDE", "INV_GETVAR", "OC_NAME"):
        cand = S._candidates(pat, False)
        real = full_scan(pat)
        check("script index: %s finds every file" % pat,
              cand is None or real.issubset(cand), True,
              "an exact-token lookup misses substrings")
        if cand is not None:
            check("script index: %s is not over-broad" % pat, len(cand), len(real))

    # a token that is nowhere costs nothing
    check("script index: absent token returns no candidates",
          S._candidates("ZZZ_NOT_A_REAL_TOKEN", False), set())
    r = S.tool_script({"pattern": "ZZZ_NOT_A_REAL_TOKEN", "limit": 5})
    check("script index: and the tool opens no file", r.get("filesScanned"), 0)
    # alternation without regex=true is a literal string
    r = S.tool_script({"pattern": "inv_getvar|oc_name", "limit": 5})
    check("script index: a regex-looking pattern searched literally says so",
          (r.get("found"), "regex=true" in (r.get("note") or "")), (0, True))
    r = S.tool_script({"pattern": "inv_getvar|oc_name", "regex": True, "limit": 5})
    check("script index: and finds hits once regex=true", r.get("found", 0) > 0, True)

    # the narrowed set holds every hit
    for pat in ("SETHIDE(", "script18930(", "SCALE(300,", "comp(1786,"):
        cand = S._candidates(pat, False)
        q = pat.lower().encode()
        truth = {f for f in S._files()
                 if q in io.open(os.path.join(S.CLIENTSCRIPT, f), "rb").read().lower()}
        check("script index: %s narrows without losing a hit" % pat,
              cand is None or not (truth - cand), True)

    # a pattern with no token run cannot narrow
    check("script index: a bare operator cannot be narrowed", S._candidates("== -1)", False), None)
    check("script index: a regex falls back to the scan",
          S._candidates("SETHIDE", True), None)


def test_scan_cache():
    """A cached scan must equal what it caches, and must notice the tree changing."""
    from rs3mcp.common import tree_fingerprint, cached_scan

    calls = []

    def build():
        calls.append(1)
        return {"n": len(calls)}

    stamp = "fixed-for-this-test"
    a = cached_scan("selftest-scan", stamp, build)
    b = cached_scan("selftest-scan", stamp, build)
    check("scan cache: the second call does not rebuild", len(calls), 1)
    check("scan cache: and returns the same value", a, b)
    c = cached_scan("selftest-scan", stamp + "-moved", build)
    check("scan cache: a new fingerprint rebuilds", c, {"n": 2})

    here = os.path.dirname(os.path.abspath(__file__))
    check("scan cache: a fingerprint is stable",
          tree_fingerprint([here], ".py"), tree_fingerprint([here], ".py"))
    check("scan cache: a missing tree still fingerprints",
          len(tree_fingerprint(["Z:/nope"])), 40)


def test_claims():
    """Numbers written into the source, against the data they describe. See claims.py for why the
    ones that are not checked are not checked."""
    from rs3mcp.test import claims
    claims.run(check, skip, repin_numbers=True)


def test_drift_runs():
    """rs3_drift has to survive its own claim list.

    A claim with the wrong signature took the tool down while every check here passed, because
    nothing ran it.
    """
    from rs3mcp.drift import tool_drift

    got = tool_drift({})
    check("drift runs at all", "error" in got, False, str(got)[:90])
    check("drift checked something", got.get("checked", 0) > 0, True)
    check("drift agrees with itself", got.get("disagreeing"), 0)

    # a rebuild rewrites these itself now
    import shutil
    import tempfile
    from rs3mcp.test import claims
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "said.py")
    io.open(path, "w", encoding="utf-8", newline="").write(
        '"""Search the 1,234\n    decompiled things. All 99 hashes. Still 7 left."""\n')
    moved = claims.rewrite([(path, r"Search the ([\d,]+) decompiled things", lambda: 5678),
                            (path, r"All (\d+) hashes", lambda: 12345),
                            (path, r"Still (\d+) left", lambda: 7)])
    text = io.open(path, encoding="utf-8", newline="").read()
    shutil.rmtree(folder, ignore_errors=True)
    check("claims rewrite: a wrapped sentence gets the new count, commas kept",
          "Search the 5,678\n    decompiled things" in text, True, text)
    check("claims rewrite: and one written without commas stays without", "All 12345 hashes" in text, True)
    check("claims rewrite: a count that still agrees is left alone", len(moved), 2)


def test_escape_guard():
    """A backslash inside code written through the shell is refused, not warned about.

    The rule was written down, then filed as a lesson, and still broke four times: a -c string that
    recycled every path under cache-tools, an octal escape, and "return {}\\n" in a heredoc twice in
    one afternoon. The guard itself was then deleted with the tree and nothing noticed, which is
    why it is checked here rather than only living in the hook.
    """
    import subprocess
    from rs3api.escapes import refuse

    bs, nl = chr(92), chr(10)
    broke = ("python - <<'PY'" + nl +
             'io.open(p, "w", newline="' + bs + bs + 'n").write(t)' + nl + "PY" + nl)
    check("escapes: a heredoc writing a backslash escape is refused", bool(refuse(broke)), True)
    check("escapes: a -c string with doubled backslashes is refused",
          bool(refuse('python -c "' + bs + bs + 'n"')), True)
    # a guard that refuses ordinary shell gets disabled
    check("escapes: a windows path is left alone",
          refuse('ls "C:' + bs + 'Users' + bs + 'Techp"'), None)
    check("escapes: find -exec is left alone",
          refuse("find . -name '*.tmp' -exec rm {} " + bs + ";"), None)
    check("escapes: a heredoc with no backslash is left alone",
          refuse("python - <<'PY'" + nl + "print(1)" + nl + "PY" + nl), None)

    # it has to deny through the hook
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": broke}})
    p = subprocess.run([sys.executable, os.path.join(ROOT, "rs3api", "hook.py")],
                       input=event, capture_output=True, text=True, timeout=120)
    decision = None
    try:
        decision = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        pass
    check("escapes: the hook denies it", decision, "deny", (p.stdout or "")[:80])


def test_brief_features_logged():
    """Every handback records which brief the agent was given, so a change to it is measurable.

    The brief section naming the tools, the ToolSearch line in the agent files, the two lines
    about failing checks: all shipped on reasoning alone. The one that was measured did nothing,
    and nothing in the log said which agents had it.
    """
    import shutil
    import tempfile
    from rs3api import brief
    from rs3mcp import handback

    root = tempfile.mkdtemp()
    try:
        # the tools section only appears with lua
        io.open(os.path.join(root, "thing.lua"), "w", encoding="utf-8").write("return {}" + chr(10))
        text = brief.as_text(brief.build("a goal", "lua54 test/x.lua", ["src"],
                                         files=["thing.lua"], plugin="drop-log", root=root))
        path = os.path.join(root, "agent-a.jsonl")
        io.open(path, "w", encoding="utf-8", newline=chr(10)).write(json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}}) + chr(10))
        got = handback.brief_features(path)
        check("brief features: the lines meant to change behaviour are named",
              "tools" in (got or []), True, str(got))

        # no brief differs from a brief with none
        check("brief features: no transcript records nothing", handback.brief_features(None), None)
        check("brief features: a missing file records nothing",
              handback.brief_features(os.path.join(root, "gone.jsonl")), None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_everything_compiles():
    """Every file in the package parses, including the ones nothing imports.

    hook.py is run by Claude Code, never imported by the server, so a broken string in it sat for a
    day with every test green and the hook failing on each call.
    """
    bad = []
    for f in glob.glob(os.path.join(ROOT, "rs3mcp", "**", "*.py"), recursive=True):
        try:
            compile(io.open(f, encoding="utf-8").read(), f, "exec")
        except SyntaxError as e:
            bad.append("%s:%s" % (os.path.relpath(f, ROOT), e.lineno))
    check("every python file in the package compiles", bad, [])

    # compiling is not importing, so it imports
    import subprocess
    broken = []
    for f in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        code = ("import importlib.util as u; s = u.spec_from_file_location('probe', r'%s'); "
                "m = u.module_from_spec(s); s.loader.exec_module(m)" % f)
        try:
            p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               timeout=90, cwd=ROOT)
        except subprocess.TimeoutExpired:
            broken.append("%s: did not finish importing" % os.path.basename(f))
            continue
        if p.returncode != 0:
            last = (p.stderr or "").strip().splitlines()
            broken.append("%s: %s" % (os.path.basename(f), last[-1][:90] if last else "failed"))
    check("every script at the top level imports, not just parses", broken, [])


def test_agent_budget():
    """An agent past its time or its tool calls is told to hand back, and one within them is not.

    Two agents ran 30 and 43 minutes with nothing to say stop. The soft word comes every tenth call,
    so it is not wallpaper; the hard one every call.
    """
    import datetime
    import shutil
    import tempfile
    from rs3mcp import handback

    root = tempfile.mkdtemp()
    try:
        start = datetime.datetime(2026, 9, 21, 12, 0, 0)

        def agent(calls):
            tr = os.path.join(root, "agent-%d.jsonl" % calls)
            rows = [{"type": "user", "timestamp": start.isoformat() + "Z", "message": {"content": "go"}}]
            rows += [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t%d" % i, "name": "Bash", "input": {"command": "ls"}}]}}
                for i in range(calls)]
            io.open(tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows))
            return {"agent_id": "x", "agent_transcript_path": tr, "hook_event_name": "PostToolUse"}

        def said(event, minutes):
            got = handback.budget(event, start + datetime.timedelta(minutes=minutes))
            return got and got["hookSpecificOutput"]["additionalContext"]

        check("budget: a young agent is left alone", said(agent(20), 5), None)
        check("budget: past the soft time, the tenth call says so",
              "hand back partial" in (said(agent(20), 16) or ""), True)
        check("budget: and the calls between stay quiet", said(agent(21), 16), None)
        check("budget: past the soft call count too", "hand back partial" in (said(agent(60), 3) or ""), True)
        check("budget: past the hard limit it says stop, every call",
              [("Stop here" in (said(agent(n), 31) or "")) for n in (41, 42)], [True, True])
        check("budget: the parent session is never told", handback.budget(
            {"agent_transcript_path": agent(90)["agent_transcript_path"]}, start), None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_handback_replay():
    """Every real handback the gate judged, judged again by today's gate, gives the recorded verdict.

    The gate's false positives only showed when a live agent tripped over one, a round lost each
    time. A change to the gate that moves any recorded verdict fails here instead, before an agent
    pays for it. Read what moved, then python -m rs3mcp.replay --record.
    """
    from rs3mcp import replay

    if not os.path.exists(replay.RECORDED):
        skip("the handback replay", ["kept handbacks (python -m rs3mcp.replay --record)"])
        return
    moved = replay.changes()
    check("replay: every kept handback gets its recorded verdict",
          ["%s %s -> %s" % (k, a, b) for k, a, b, _ in moved], [],
          "; ".join(first[:80] for _, _, _, first in moved[:3]))
    check("replay: the set is not empty", len(replay.attempts()) > 0, True)


def test_learned_lines():
    """What an agent says it learned comes back as a candidate lesson, if it shows its evidence.

    Of 215 subagent runs since 2026-09-01 none filed a lesson; every one came from the parent.
    """
    import shutil
    import tempfile
    from rs3mcp import handback

    root = tempfile.mkdtemp()
    was = {k: os.environ.get(k) for k in ("RS3_LEARNED", "RS3_GATE_LOG")}

    def _lines(path):
        return sum(1 for _ in io.open(path, encoding="utf-8")) if os.path.exists(path) else 0
    real_gate = handback.gate_log()
    gate_before = _lines(real_gate)
    try:
        good = os.path.join(root, "good.py")
        io.open(good, "w", encoding="utf-8").write("x = 1\ny = 2\n")
        tr = os.path.join(root, "agent.jsonl")
        rows = [{"type": "user", "message": {"content": "goal: the learned probe\nscope: here"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python -m pytest tests"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed", "is_error": False}]}}]
        io.open(tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows))
        base = "## Handback\nstatus: done\nran: python -m pytest tests\n"

        def says(extra):
            return handback.problems(base + extra, tr, root)

        check("learned: parsed off the block", (handback.parse(base + "learned: a, good.py:2") or {}).get("learned"),
              ["a, good.py:2"])
        check("learned: a file:line that exists holds", says("learned: y is set late, good.py:2"), [])
        # learned lines that repeat what is known
        handed = ("goal: the learned probe\nscope: here\n\n" + handback.HANDOVER + "\n"
                  "- measured crashes on client build b: ui.Grid#cellHeight read, ui.Grid#cellWidth read\n"
                  "- when there is more than one grid, store each size instead of reading it back\n"
                  "- calls are read against the typings: a member declared this: void or static takes a dot "
                  "(Event.Logic.Subscribe), a method takes a colon (grid:SetCellSize)\n")
        echo_tr = os.path.join(root, "echo.jsonl")
        io.open(echo_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in
                                                            [{"type": "user", "message": {"content": handed}}] + rows[1:]))
        echo = handback.problems(base + "learned: reading ui.Grid#cellWidth and cellHeight crashes the client, good.py:2",
                                 echo_tr, root)
        check("learned: a line repeating the hand-over is sent back", any("you were handed" in p for p in echo), True, echo)
        plural = handback.problems(base + "learned: EventChannel methods Subscribe/Unsubscribe use dot notation when "
                                   "declared this:void, good.py:2", echo_tr, root)
        check("learned: and in its plurals", any("you were handed" in p for p in plural), True, plural)
        fresh = handback.problems(base + "learned: the suite needs lua54 on the path, good.py:2", echo_tr, root)
        check("learned: a line the hand-over does not hold is not", fresh, [])
        near = handback.problems(base + "learned: a ui.Grid cellWidth read crashes the client only when the grid is "
                                 "hidden, and SetCellSize takes padding as its third argument, good.py:2", echo_tr, root)
        check("learned: naming the same things to say something new is not", near, [])
        filler = handback.problems(base + "learned: when there is more than one timer, keep each deadline instead "
                                   "of polling it, good.py:2", echo_tr, root)
        check("learned: sharing only filler words is not", filler, [])
        from rs3mcp import addons
        if addons.first("known_lessons"):
            dup = handback.problems(base + "learned: reading ui.Grid cellWidth crashes the client, keep sizes yourself, "
                                    "good.py:2", tr, root)
            check("learned: a line repeating a lesson on file names it",
                  any("already a lesson" in p for p in dup), True, dup)
            padded = handback.problems(base + "learned: reading ui.Grid.cellWidth and ui.Grid.cellHeight crashes the "
                                       "client; store cell dimensions separately, not via property reads, good.py:2",
                                       tr, root)
            check("learned: and with its words in other forms",
                  any("already a lesson" in p for p in padded), True, padded)
        else:
            skip("learned: repeats of lessons on file", ["the rs3api add-on, which offers known_lessons"])
        check("learned: none, or the template left as it is, is no line",
              [says("learned: none"), says(handback.TEMPLATE.splitlines()[-1]),
               (handback.parse(base + "learned: n/a") or {}).get("learned")], [[], [], []])
        check("learned: a command that ran holds", says("learned: the suite is fast, `python -m pytest tests`"), [])
        check("learned: a line past the end is sent back",
              any("has 2 lines" in p for p in says("learned: y is set late, good.py:9")), True)
        check("learned: no evidence is sent back",
              any("needs what shows it" in p for p in says("learned: something is odd somewhere")), True)
        check("learned: a command never run is sent back",
              any("needs what shows it" in p for p in says("learned: it is slow, `npm run build`")), True)

        log = os.path.join(root, "learned.jsonl")
        os.environ["RS3_LEARNED"] = log
        os.environ["RS3_GATE_LOG"] = os.path.join(root, "handbacks.jsonl")
        event = {"hook_event_name": "SubagentStop", "agent_id": "probe", "agent_type": "worker",
                 "agent_transcript_path": tr, "cwd": root, "session_id": "s",
                 "last_assistant_message": base + "learned: y is set late, good.py:2"}
        check("learned: a passing handback is let through", handback.hook(event), None)
        got = [json.loads(l) for l in io.open(log, encoding="utf-8")] if os.path.exists(log) else []
        check("learned: and its line is a candidate", [(g["agent"], g["text"], g["status"]) for g in got],
              [("probe", "y is set late, good.py:2", "candidate")])
        bad = dict(event, agent_id="probe2", last_assistant_message=base + "learned: odd, good.py:99")
        check("learned: a failing one is sent back", (handback.hook(bad) or {}).get("decision"), "block")
        got = [json.loads(l) for l in io.open(log, encoding="utf-8")] if os.path.exists(log) else []
        check("learned: and records nothing", len(got), 1)
        check("learned: the real gate log is untouched", _lines(real_gate), gate_before)
        gate = [json.loads(l) for l in io.open(os.environ["RS3_GATE_LOG"], encoding="utf-8")]
        check("learned: the gate logs each agent's brief key", gate[0].get("briefHead"),
              handback.head("goal: the learned probe\nscope: here"))
        check("learned: two briefs sharing an opening get different keys",
              handback.head("x" * 130 + " a") != handback.head("x" * 130 + " b"), True)
        check("learned: and a hand-over appended leaves the key alone",
              handback.head("goal: g\n\n" + handback.HANDOVER + "\n- a lesson"), handback.head("goal: g"))
    finally:
        for name, value in was.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if os.path.dirname(root) == os.path.normpath(tempfile.gettempdir()):
            shutil.rmtree(root, ignore_errors=True)


def test_handback():
    """An agent's "done" has to survive its own transcript.

    Of 57 past agents that said verified or build clean, 34 never ran a build. The parent then
    checked everything again, which is the cost this removes.
    """
    import shutil
    import tempfile
    from rs3mcp import handback

    root = tempfile.mkdtemp()
    try:
        good = os.path.join(root, "good.py")
        io.open(good, "w", encoding="utf-8").write("x = 1\n")
        broken = os.path.join(root, "broken.json")
        io.open(broken, "w", encoding="utf-8").write("{nope")
        tr = os.path.join(root, "agent.jsonl")

        def use(i, name, **inp):
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t%d" % i, "name": name, "input": inp}]}}

        def res(i, text, err=False):
            return {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t%d" % i, "content": text, "is_error": err}]}}

        rows = [use(1, "Bash", command="python -m pytest tests"), res(1, "3 passed"),
                use(2, "Bash", command="npm run build"), res(2, "Exit code 1\ncannot find name", True),
                use(3, "Edit", file_path=good)]
        io.open(tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows))

        def says(msg):
            return handback.problems(msg, tr, root)

        ok = "All good.\n## Handback\nstatus: done\nfile: good.py\nran: python -m pytest tests\nclaim: x is set, good.py:1"
        check("handback: a true one holds", says(ok), [])
        check("handback: none at all is sent back", len(says("Done, all verified.")), 1)
        check("handback: a check never run",
              any("never run" in p for p in says(ok.replace("pytest tests", "pytest other"))), True)
        check("handback: a check that failed",
              any("failed" in p for p in says(ok + "\nran: npm run build")), True)
        # doing what the message says has to work
        told = [p for p in says(ok + "\nran: npm run build") if "failed" in p][0]
        followed = ok.replace("status: done", "status: partial") + "\nquestion: npm run build failed"
        check("handback: the advice for a failed check is advice the gate accepts",
              ("from ran: to question:" in told, says(followed)), (True, []))
        check("handback: a file that does not parse",
              any("not valid json" in p for p in says(ok + "\nfile: broken.json")), True)
        check("handback: an edit left off the list",
              any("did not list" in p for p in says(ok.replace("file: good.py\n", ""))), True)
        check("handback: a claim past the end of the file",
              any("has 1 lines" in p for p in says(ok.replace("good.py:1", "good.py:40"))), True)
        check("handback: verified with nothing run",
              any("list nothing under ran" in p
                  for p in says("Build is clean.\n## Handback\nstatus: done\nfile: good.py")), True)
        check("handback: blocked is an honest answer",
              says("## Handback\nstatus: blocked\nfile: good.py\nquestion: no shell here"), [])

        # a bare file name with two candidates
        proj = os.path.join(root, "proj")
        for d in ("core", "engine", "test"):
            os.makedirs(os.path.join(proj, d))
        io.open(os.path.join(proj, "plugin.json"), "w", encoding="utf-8").write("{}\n")
        io.open(os.path.join(proj, "core", "only.lua"), "w", encoding="utf-8").write("a = 1\nb = 2\n")
        # both outside the folder looked in first
        io.open(os.path.join(proj, "core", "twice.lua"), "w", encoding="utf-8").write("a = 1\n")
        io.open(os.path.join(proj, "engine", "twice.lua"), "w", encoding="utf-8").write("a = 1\n")
        mine = os.path.join(proj, "test", "mine.lua")
        io.open(mine, "w", encoding="utf-8").write("x = 1\n")
        bare_tr = os.path.join(root, "bare.jsonl")
        io.open(bare_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in [
            use(1, "Bash", command="lua test/mine.lua"), res(1, "1 passed, 0 failed"),
            use(2, "Write", file_path=mine), res(2, "ok")]))
        base = "## Handback\nstatus: done\nfile: %s\nran: lua test/mine.lua\n" % mine
        check("handback: a bare name that is one file is found",
              handback.problems(base + "claim: the cause, only.lua:2", bare_tr, root), [])
        check("handback: a bare name that is two files asks for the path",
              any("give its path" in p for p in handback.problems(base + "claim: x, twice.lua:1", bare_tr, root)), True)
        # a debugger that adds nothing has no folder
        elsewhere = os.path.join(root, "elsewhere")
        os.makedirs(elsewhere)
        scope_tr = os.path.join(root, "scope.jsonl")
        io.open(scope_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in [
            {"type": "user", "message": {"content": "Task folder: %s\nscope: %s" % (proj, proj)}},
            use(1, "Bash", command="lua run.lua"), res(1, "counted: 3")]))
        check("handback: a claim is read against the brief's scope folder",
              handback.problems("## Handback\nstatus: blocked\nran: lua run.lua\nclaim: ruled out, core/only.lua:2\n"
                                "question: it does not reproduce", scope_tr, elsewhere), [])
        # lua writes ./core/only.lua, and the dot stays
        check("handback: a ./ path quoted from a lua error resolves",
              handback.problems(base + "claim: the check fails, ./core/only.lua:2: attempt to index a nil value",
                                bare_tr, root), [])

        # a refused write is not an edit
        already = os.path.join(root, "already.lua")
        io.open(already, "w", encoding="utf-8").write("return 1\n")
        refused_tr = os.path.join(root, "refused.jsonl")
        io.open(refused_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows + [
            use(9, "Write", file_path=already), res(9, "outside this task's scope", True)]))
        check("handback: a refused write is not an edit", handback.problems(ok, refused_tr, root), [])
        check("handback: and a write that went through still is", good in handback.activity(refused_tr)[1], True)

        # a check run through MCP still counts
        mcp_tr = os.path.join(root, "mcp.jsonl")
        io.open(mcp_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in [
            use(1, "mcp__rs3cache__rs3_lua_check", path="C:/x/core/a.lua"), res(1, "compiles 1"),
            use(2, "Write", file_path=mine), res(2, "ok")]))
        head = "## Handback\nstatus: done\nfile: %s\n" % mine
        check("handback: an MCP call under ran is one that ran",
              handback.problems(head + "ran: mcp__rs3cache__rs3_lua_check path=C:/x/core/a.lua\n", mcp_tr, root), [])
        check("handback: and so is its short name",
              handback.problems(head + "ran: rs3_lua_check path=C:/x/core/a.lua\n", mcp_tr, root), [])
        # what a shell command writes, not Edit
        w = handback.shell_writes
        task = "C:/t/task"
        check("shell writes: a redirect", w("python gen.py > C:/elsewhere/x.lua", task), [("C:/elsewhere/x.lua", task)])
        check("shell writes: cd then a relative redirect", w('cd "C:/t/task" && lua a.lua > out.txt', "C:/x"),
              [("out.txt", "C:/t/task")])
        check("shell writes: a line break ends a command too", w("cd C:/t/task\nlua a.lua >> log.txt", "C:/x"),
              [("log.txt", "C:/t/task")])
        check("shell writes: a > inside quotes is not a redirect", w("lua -e 'print(2 > 1)' 2>&1", task), [])
        check("shell writes: a heredoc body is data", w("cat > edge.lua <<'EOF'\nif a > b then end\nEOF", task),
              [("edge.lua", task)])
        check("shell writes: nowhere is nowhere", w("lua t.lua > /dev/null 2>&1", task), [])
        check("shell writes: tee, sed -i, cp, rm", w("echo x | tee -a a.txt; sed -i 's/a/b/' b.lua; cp s d; rm -f e", task),
              [("a.txt", task), ("b.lua", task), ("d", task), ("e", task)])
        check("shell writes: reading writes nothing", w("cat SPEC.md && ls && grep -n x *.lua | head", task), [])
        other = os.path.join(root, "other.lua")
        io.open(other, "w", encoding="utf-8").write("y = 1\n")
        two_tr = os.path.join(root, "two.jsonl")
        io.open(two_tr, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in [
            use(1, "Bash", command="lua test/mine.lua"), res(1, "1 passed, 0 failed"),
            use(2, "Write", file_path=mine), res(2, "ok"), use(3, "Write", file_path=other), res(3, "ok")]))
        check("handback: two files on one line with a comma are two files",
              handback.problems("## Handback\nstatus: done\nfile: %s, %s\nran: lua test/mine.lua\n" % (mine, other),
                                two_tr, root), [])
        check("handback: a tool it never called is still never run",
              any("never run" in p for p in handback.problems(head + "ran: rs3_verify path=C:/x/core/a.lua\n", mcp_tr, root)), True)

        # a path with spaces behind prose with spaces
        check("handback: words before a path are not part of it",
              says(ok.replace("claim: x is set, good.py:1",
                              "claim: x is set, good.py:1 but it is made at good.py:1")), [])
        check("handback: a path that really does not exist is still caught",
              any("does not exist" in p for p in says(ok.replace("good.py:1", "nowhere/at all.py:1"))), True)

        # the found status skips the failing check rule
        found = ("## Handback\nstatus: found\nfile: good.py\nran: npm run build\n"
                 "claim: the cause, good.py:1")
        check("handback: a check that fails on purpose is the point of found", says(found), [])
        check("handback: and the same failure under done is still sent back",
              any("failed" in p for p in says(found.replace("status: found", "status: done"))), True)
        # or found means nothing was checked
        check("handback: found without the check having run",
              any("under ran" in p for p in says(found.replace("ran: npm run build\n", ""))), True)
        check("handback: found without the cause",
              any("under claim" in p for p in says(found.replace("claim: the cause, good.py:1", ""))), True)

        # a search when the transcript path cannot exist
        here = os.path.join(root, "projects", "a-project", "a-session", "subagents")
        os.makedirs(here)
        io.open(os.path.join(here, "agent-abc123.jsonl"), "w", encoding="utf-8").write("{}\n")
        real, handback.PROJECTS = handback.PROJECTS, os.path.join(root, "projects")
        try:
            byboth = handback._agent_transcript(
                {"agent_id": "abc123",
                 "transcript_path": os.path.join(root, "projects", "a-project", "a-session.jsonl")})
            alone = handback._agent_transcript({"agent_id": "abc123"})
            missing = handback._agent_transcript({"agent_id": "notanagent"})
        finally:
            handback.PROJECTS = real
        # a workflow keeps its agents a level down
        deep = os.path.join(root, "projects", "a-project", "a-session", "subagents", "workflows", "wf_1")
        os.makedirs(deep)
        io.open(os.path.join(deep, "agent-wf9.jsonl"), "w", encoding="utf-8").write("{}\n")
        real2, handback.PROJECTS = handback.PROJECTS, os.path.join(root, "projects")
        try:
            inwf = handback._agent_transcript({"agent_id": "wf9"})
        finally:
            handback.PROJECTS = real2
        check("handback: a workflow's agent is found too", handback.readable(inwf), True, str(inwf))

        # three things share the log, so split them
        check("handback: a judgement with evidence is the only kind a rate uses",
              [handback.kind(r) for r in (
                  {"evidence": True, "status": "done"},
                  {"evidence": False, "status": None},
                  {"status": None},
                  {"status": "done"},
                  {"evidence": False, "status": "done"})],
              ["judged", "not a handback", "not a handback", "judged blind", "no transcript"])
        check("handback: an agent id with a transcript path is found", handback.readable(byboth), True)
        check("handback: and an agent id on its own is found too", handback.readable(alone), True,
              str(alone))
        # or an id finds somebody else's newest transcript
        check("handback: an id with no transcript stays unreadable", handback.readable(missing), False)

        # the scope lines sit indented under the sentence
        import importlib.util
        if importlib.util.find_spec("rs3api") is not None:
            from rs3api import brief as _brief
            one = _brief.as_text(_brief.build("x", None, ["Desktop/_Misc_Projects/advent-2025/day01"]))
        else:
            skip("scope from a real rs3_brief", ["the rs3api add-on"])
            one = "scope: the folders this may change\n  Desktop/_Misc_Projects/advent-2025/day01\n\nchecks"
        check("scope: the folders under the sentence are the folders",
              handback.folders(one), ["Desktop/_Misc_Projects/advent-2025/day01"])
        check("scope: a folder on the line itself still counts",
              handback.folders("scope: plugins/drop-log\nnext"), ["plugins/drop-log"])
        check("scope: several, and the list ends where the indent does",
              handback.folders("scope: only these\n  a/b\n  c/d\n\nchecks\n  lua test/x.lua"),
              ["a/b", "c/d"])
        # no folder means anywhere, not refuse everything
        check("scope: a sentence with no folder in it gives none",
              handback.folders("scope: use your judgement\nand carry on"), [])
        # the brief that blocked the wrong kill expert
        check("scope: a path inside a sentence is the path, not the sentence",
              handback.folders("Scope: you may edit core/controller.lua only. Do not edit any file under test/."),
              ["core/controller.lua"])
        with tempfile.TemporaryDirectory(prefix="my plugins ") as plugin:
            os.makedirs(os.path.join(plugin, "core"))
            io.open(os.path.join(plugin, "core", "controller.lua"), "w").close()
            text = "Plugin folder: %s\nScope: you may edit core/controller.lua only." % plugin.replace("\\", "/")
            here = [handback._norm_path(s, None, handback.bases(text)) for s in handback.folders(text)]
            check("scope: a relative scope is read against the folder the brief names",
                  here, [os.path.normcase(os.path.join(plugin, "core", "controller.lua"))])
        check("scope: an absolute path with spaces in it stays whole",
              handback.folders("scope: C:/Users/someone/My Projects/plugins/drop-log"),
              ["C:/Users/someone/My Projects/plugins/drop-log"])

        home = os.path.expanduser("~")
        allowed = os.path.join(home, "Desktop", "_Misc_Projects", "advent-2025", "day01")
        def tries(path, scope="Desktop/_Misc_Projects/advent-2025/day01"):
            saved = handback.scopes
            handback.scopes = lambda _t: [scope]
            try:
                return handback.guard({"agent_id": "x", "transcript_path": "t",
                                       "tool_input": {"file_path": path}}) is None
            finally:
                handback.scopes = saved
        check("scope: a file in the folder is allowed", tries(os.path.join(allowed, "solve.py")), True)
        import tempfile as _tf
        pad = os.path.join(_tf.gettempdir(), "claude", "a-project", "a-session", "scratchpad", "probe.lua")
        check("scope: the session scratchpad is always allowed", tries(pad), True)
        # the temp folder at large is not one
        check("scope: the rest of the temp folder is not",
              tries(os.path.join(_tf.gettempdir(), "claude", "a-project", "notes.txt")), False)
        check("scope: nor a scratchpad folder outside it",
              tries(os.path.join(home, "Desktop", "scratchpad", "x.lua")), False)
        check("scope: and below it", tries(os.path.join(allowed, "lib", "h.py")), True)
        check("scope: a sibling folder is not", tries(os.path.join(home, "Desktop", "x.py")), False)
        # day01-notes starts with day01, so separators count
        check("scope: nor one whose name merely starts the same",
              tries(allowed + "-notes" + os.sep + "x.py"), False)

        # an unknown status rejects every handback quietly
        agents = os.path.join(os.path.expanduser("~"), ".claude", "agents")
        if os.path.isdir(agents):
            unknown = []
            for name in sorted(os.listdir(agents)):
                if not name.endswith(".md"):
                    continue
                text = io.open(os.path.join(agents, name), encoding="utf-8", errors="replace").read()
                for line in re.findall(r"^status:\s*(.+)$", text, re.M):
                    for word in re.split(r"\s*\|\s*", line.strip()):
                        w = word.strip().strip("<>")
                        if w and w not in handback.STATUSES:
                            unknown.append("%s: %s" % (name, w))
            check("handback: every agent file uses a status the gate knows", unknown, [])
            # the rule that ended the summary bounces
            unruled = [name for name in sorted(os.listdir(agents)) if name.endswith(".md")
                       and "\nran:" in io.open(os.path.join(agents, name), encoding="utf-8").read()
                       and "copied exactly as you ran it" not in io.open(os.path.join(agents, name), encoding="utf-8").read()]
            check("handback: every agent file with a ran: line says what ran: means", unruled, [])

        # the four reasons never run was claimed
        check("handback: a command with its result beside it still counts",
              says(ok.replace("ran: python -m pytest tests",
                              "ran: python -m pytest tests (3 passed)")), [])
        check("handback: a shortened long command still counts",
              says(ok.replace("ran: python -m pytest tests", "ran: python ... tests")), [])
        check("handback: prose under ran is named as prose, not as a lie",
              [p for p in says(ok.replace("ran: python -m pytest tests", "ran: none in this context"))
               if "is not a command" in p] != [], True)
        # no transcript is no evidence, not lying
        gone = os.path.join(root, "not-here.jsonl")
        check("handback: an unreadable transcript does not accuse",
              [p for p in handback.problems(ok, gone, root) if "never run" in p], [])
        check("handback: and the rest of it is still checked",
              any("does not exist" in p for p in
                  handback.problems(ok + "\nfile: missing.py", gone, root)), True)
        # a path from another project the agent used
        other = os.path.join(root, "elsewhere")
        os.makedirs(other, exist_ok=True)
        io.open(os.path.join(other, "far.py"), "w", encoding="utf-8").write("y = 2\n")
        rows2 = rows + [use(4, "Edit", file_path=os.path.join(other, "far.py")),
                        use(5, "Bash", command="python -m pytest tests"), res(5, "3 passed")]
        tr2 = os.path.join(root, "agent2.jsonl")
        io.open(tr2, "w", encoding="utf-8").write("".join(json.dumps(r) + "\n" for r in rows2))
        # a path with a space, below every root
        deep = os.path.join(root, "pkg", "src")
        os.makedirs(deep, exist_ok=True)
        io.open(os.path.join(deep, "deep.py"), "w", encoding="utf-8").write("z = 3\n")
        check("handback: a file deeper than the root is still found",
              [p for p in says(ok + "\nfile: src/deep.py") if "does not exist" in p], [])
        spaced = os.path.join(root, "a folder", "x.py")
        os.makedirs(os.path.dirname(spaced), exist_ok=True)
        io.open(spaced, "w", encoding="utf-8").write("w = 4\n")
        check("handback: a claim path with a space in it is read whole",
              [p for p in says(ok + "\nclaim: it is set, " + spaced.replace("\\", "/") + ":1")
               if "does not exist" in p], [])
        check("handback: no transcript, no verdict, not even about the block",
              handback.problems("Done, all verified.", os.path.join(root, "no-such.jsonl"), root), [])
        check("handback: with a transcript, a missing block is still sent back",
              len(says("Done, all verified.")), 1)
        check("handback: file: none means nothing was changed",
              [p for p in says(ok.replace("file: good.py", "file: none")) if "does not exist" in p], [])
        check("handback: a file under another root it worked in is found",
              [p for p in handback.problems(ok + "\nfile: far.py", tr2, root) if "does not exist" in p],
              [])

        # the brief's scope lines fence the agent's edits
        sess = os.path.join(root, "s1")
        os.makedirs(os.path.join(sess, "subagents"))
        io.open(os.path.join(sess, "subagents", "agent-a1.jsonl"), "w", encoding="utf-8").write(
            json.dumps({"type": "user", "message": {"content": "Fix it.\nscope: %s/api\n" % root}}) + "\n")
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "agent_id": "a1",
              "transcript_path": sess + ".jsonl"}
        inside = handback.guard(dict(ev, tool_input={"file_path": os.path.join(root, "api", "x.ts")}))
        outside = handback.guard(dict(ev, tool_input={"file_path": os.path.join(root, "api-wt", "x.ts")}))
        check("handback: an edit in scope goes ahead", inside, None)
        check("handback: a look-alike folder is out of scope",
              (outside or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        check("handback: the parent's own edits are never fenced",
              handback.guard(dict(ev, agent_id=None, tool_input={"file_path": "C:/anywhere.py"})), None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bypass_advice():
    """A shell command that reads the cache raw should name the tool that answers it.

    Not tidiness. A tool nobody calls is a tool nobody debugs, and this session answered dozens of
    cache questions with sqlite while the tools went unexercised.

    The silences matter more than the findings. Building the server means querying it directly, so
    a hook that fires on the test suite would be off within the hour.
    """
    from rs3mcp.bypass import advise

    speaks = [
        ("select id,name from config where type='npcs'", "rs3_lookup kind=npc"),
        ("select * from config where type='objects'", "rs3_lookup kind=loc"),
        ("sqlite3 rs3-cache.db \"select * from varbit where base_id=2797\"", "rs3_var"),
        ("from rs3mcp.cache import _index  # morph_edge", "rs3_family"),
        ("from rs3mcp.common import connect  # param_use", "rs3_trace"),
    ]
    for cmd, want in speaks:
        got = advise("python -c \"from rs3mcp.common import connect\" " + cmd) or ""
        check("bypass: routes to %s" % want, want in got, True, got[:70])

    quiet = ["python rs3-mcp.py --test",
             "python rs3-mcp.py --golden",
             "python rs3mcp/test/soak.py",
             "python rs3mcp/buildindex.py",
             "python rs3-mcp.py rs3_lookup kind=npc id=798",
             "git status --short",
             "python docs_write.py"]
    for cmd in quiet:
        check("bypass: silent on %s" % cmd.split()[-1][:26], advise(cmd), None)


def test_scrying_pool():
    """The monitor reads transcripts right, tails them, and never touches the MCP wire."""
    import contextlib
    import shutil
    import socket
    import tempfile
    import urllib.request
    from rs3mcp import journal, poollog
    from rs3mcp.bypass import advise

    root = tempfile.mkdtemp()
    # other apps log here too, so filter
    old_pool, poollog.POOL = poollog.POOL, os.path.join(root, "pool")
    try:
        proj = os.path.join(root, "proj")
        sub = os.path.join(proj, "s1", "subagents")
        os.makedirs(sub)
        stamp = "2026-09-12T10:00:%02d.000Z"

        def call(n, name, args):
            return {"type": "assistant", "timestamp": stamp % n, "cwd": "C:\\x\\cache work",
                    "requestId": "r%d" % n,
                    "message": {"model": "m", "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000,
                                                        "output_tokens": 7},
                                "content": [{"type": "tool_use", "id": "t%d" % n, "name": name,
                                             "input": args}]}}

        def result(n, text, err=False):
            return {"type": "user", "timestamp": stamp % (n + 1),
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % n,
                                             "content": text, "is_error": err}]}}

        lines = [
            {"type": "custom-title", "customTitle": "Starsign"},
            {"type": "user", "timestamp": stamp % 0, "message": {"content": "check STARSIGN"}},
            {"type": "user", "isMeta": True, "timestamp": stamp % 0, "message": {"content": "skill text"}},
            call(1, "mcp__rs3cache__rs3_var", {"base": 2797}), result(1, "x" * 400),
            call(3, "Bash", {"command": "sqlite3 rs3-cache.db \"select * from varbit\""}), result(3, "no", True),
            call(5, "Bash", {"command": "python rs3-mcp.py rs3_lookup kind=npc id=1"}), result(5, "ok"),
            call(7, "Edit", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2\ny = 3"}), result(7, "done"),
            call(9, "mcp__github__get_issue", {"issue": 1}),
        ]
        path = os.path.join(proj, "s1.jsonl")
        io.open(path, "w", encoding="utf-8").write("".join(json.dumps(l) + "\n" for l in lines))
        io.open(os.path.join(sub, "agent-a1.jsonl"), "w", encoding="utf-8").write(
            json.dumps(call(11, "Grep", {"pattern": "STARSIGN"})) + "\n")
        io.open(os.path.join(sub, "agent-a1.meta.json"), "w", encoding="utf-8").write(
            json.dumps({"agentType": "Explore", "description": "find the var"}))

        sets = {"rs3_var": "game", "rs3_lookup": "cache"}
        j = journal.Journal(sets, root, advise)
        got = j.poll()
        calls = {e["id"]: e for e in got["events"] if e["kind"] == "call"}
        prompts = [e["text"] for e in got["events"] if e["kind"] == "prompt"]
        results = {e["of"]: e for e in got["events"] if e["kind"] == "result"}
        check("pool: one prompt, meta lines skipped", prompts, ["check STARSIGN"])
        check("pool: our tool is a place under its set",
              (calls["t1"]["place"], calls["t1"]["act"], calls["t1"]["rs3"]), ("rs3cache/game/rs3_var", "ask", "rs3_var"))
        check("pool: a raw cache read is a shortcut", (calls["t3"]["place"], calls["t3"].get("bypass")), ("C:/x/cache work/rs3-cache.db", True))
        check("pool: the cli is the tool, not a shortcut",
              (calls["t5"]["place"], calls["t5"]["act"], calls["t5"].get("bypass")), ("rs3cache/cache/rs3_lookup", "ask", None))
        check("pool: an edit is a visit to the file, with its size",
              (calls["t7"]["place"], calls["t7"]["act"], calls["t7"]["detail"]), ("C:/x/cache work/a.py", "edit", "+2 -1"))
        check("pool: other servers are named", calls["t9"]["place"], "mcp/github/get_issue")

        home = journal.HOME
        shell = journal.shell_place
        check("pool: cd then run a script is that script",
              shell('cd "%s/Desktop" && python rs3mcp/test/soak.py' % home)[:2], ("Desktop/rs3mcp/test/soak.py", "run"))
        check("pool: a tilde is home", shell("cd ~/notes && sed -n '1,9p' todo.md")[:2], ("notes/todo.md", "read"))
        check("pool: git bash drives", journal.path_place("/c/Users/nobody/x.py"), "C:/Users/nobody/x.py")
        check("pool: a grep pattern is not a folder",
              shell('cd "%s" && grep -rn "render[/]shapes" nowhere-at-all' % home)[:2], ("./(searched)", "search"))
        check("pool: curl is the host", shell("curl -s https://runescape.wiki/api.php")[:2], ("web/runescape.wiki", "fetch"))
        check("pool: an inline script is the file it opens",
              shell("python - <<'PY'\nio.open(\"notes/a.md\").read()\nPY", home)[0], "notes/a.md")
        check("pool: anything else is the program", shell("git status --short")[:2], ("shell/git", "run"))
        check("pool: a failed result says so", results["t3"]["err"], True)
        check("pool: result size is carried", results["t1"]["chars"], 400)
        s = got["sessions"]
        check("pool: session title and project",
              (s["s1"]["title"], s["s1"]["project"], s["s1"]["ctx"]), ("Starsign", "cache work", 1005))
        check("pool: a subagent knows its parent",
              (s["agent-a1"]["parent"], s["agent-a1"]["title"], s["agent-a1"]["kind"]), ("s1", "find the var", "Explore"))
        check("pool: private fields stay home", any(k.startswith("_") for k in s["s1"]), False)

        with io.open(path, "a", encoding="utf-8") as h:
            h.write(json.dumps(result(9, "late")) + "\n" + json.dumps(call(12, "Read", {"file_path": "b"}))[:30])
        j.globbed = 0
        more = j.poll(got["next"])
        check("pool: only new lines come back, a half written one waits",
              [e["kind"] for e in more["events"]], ["result"])

        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            th = journal.serve([{"name": "rs3_var", "set": "game"}], advise, port=busy, root=root, retry=0)
            th.join(3)
            check("pool: a taken port gives up quietly", (th.is_alive(), th.run_ref.httpd), (False, None))
            taken.close()

            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
            probe.close()
            th = journal.serve([{"name": "rs3_var", "set": "game"}], advise, port=free, root=root, retry=0)
            for _ in range(50):
                if th.run_ref.httpd:
                    break
                time.sleep(.05)
            base = "http://127.0.0.1:%d" % free
            page = urllib.request.urlopen(base + "/").read().decode("utf-8")
            check("pool: serves the page", "<title>rs3cache console</title>" in page, True)
            older = urllib.request.urlopen(base + "/map").read().decode("utf-8")
            check("pool: the old map is still reachable", "<title>Scrying Pool</title>" in older, True)
            # the page draws the last hour first
            now = int(time.time() * 1000)
            recent = json.loads(urllib.request.urlopen(base + "/events?since=%d" % (now + 60000)).read())
            check("pool: a since window keeps only what is inside it", recent["events"], [])
            ev = json.loads(urllib.request.urlopen(base + "/events?after=0").read())
            check("pool: serves the events", len([e for e in ev["events"] if e["kind"] == "call"]), 6)
            meta = json.loads(urllib.request.urlopen(base + "/meta").read())
            check("pool: serves the tool list", meta["tools"][0]["name"], "rs3_var")
            one = json.loads(urllib.request.urlopen(base + "/call?id=t3").read())
            check("pool: one call reads back in full",
                  (one["input"]["command"][:7], one["result"], one["err"]), ("sqlite3", "no", True))
            try:
                code = urllib.request.urlopen(base + "/call?id=nope").status
            except urllib.error.HTTPError as e:
                code = e.code
            check("pool: an unknown call is a 404, not a path", code, 404)
            req = urllib.request.Request(base + "/events", headers={"Host": "evil.example:%d" % free})
            try:
                code = urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                code = e.code
            check("pool: another hostname is refused", code, 403)
            th.run_ref.httpd.shutdown()
            th.run_ref.httpd.server_close()
        check("pool: nothing reached stdout", out.getvalue(), "")
    finally:
        poollog.POOL = old_pool
        shutil.rmtree(root, ignore_errors=True)


def test_toolset_file():
    """The sets come from a file beside the server, because they change per task."""
    import importlib.util as iu

    path = os.path.join(ROOT, ".rs3-toolsets")
    existed = os.path.exists(path)
    if existed:
        keep = io.open(path, encoding="utf-8").read()

    def load():
        spec = iu.spec_from_file_location("srv_sets_file", os.path.join(ROOT, "rs3-mcp.py"))
        mod = iu.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        return mod

    try:
        io.open(path, "w", encoding="utf-8").write("# a comment is ignored\nscripts game\n")
        m = load()
        picked = {t["name"] for t in m.listing()}
        check("toolsets: a file narrows the listing", len(picked) < len(m.TOOLS), True)
        check("toolsets: to the sets it names", "rs3_script" in picked and "rs3_var" in picked, True)
        check("toolsets: and nothing else", "rs3_wiki" in picked, False)
    finally:
        os.remove(path)
        if existed:
            io.open(path, "w", encoding="utf-8").write(keep)

    if not existed:
        check("toolsets: removing it restores everything", len(load().listing()), len(load().TOOLS))


def test_toolsets():
    """Every tool belongs to a named set, and selecting one lists only that set.

    After github-mcp-server, which groups its tools so a client lists what it needs. The listing is
    paid per session whether a tool is called or not.

    The set comes from the module, so the check that matters is that no module falls through to a
    default: a new family would join `meta` silently and nobody would see it.
    """
    import importlib.util as iu

    spec = iu.spec_from_file_location("srv_sets", os.path.join(ROOT, "rs3-mcp.py"))
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass

    modules = sorted({(t["fn"].__module__ or "").rsplit(".", 1)[-1] for t in mod.TOOLS})
    named = {m for mods in mod.SETS.values() for m in mods}
    check("toolsets: every module that registers a tool is placed", sorted(set(modules) - named), [])

    check("toolsets: unset lists everything", len(mod.listing()), len(mod.TOOLS))

    os.environ["RS3_TOOLSETS"] = "scripts"
    try:
        only = mod.listing()
        check("toolsets: a selection lists less", len(only) < len(mod.TOOLS), True)
        check("toolsets: and only what was asked for",
              sorted({t["name"] for t in only} - {"rs3_script", "rs3_script_read", "rs3_script_refs"}), [])
        # an unlisted tool still answers a call
        got = mod.handle({"method": "tools/call",
                          "params": {"name": "rs3_var", "arguments": {"name": "ITGRONIGEN"}}})
        check("toolsets: an unlisted tool still answers", bool(got.get("content")), True)
    finally:
        del os.environ["RS3_TOOLSETS"]

    check("toolsets: and the default is restored", len(mod.listing()), len(mod.TOOLS))


def test_spill_and_brief():
    """The two ways to ask for less: write it to a file, or ask for names.

    Both are borrowed. Artifact's read_db takes an out_dir and returns the files instead of the
    documents; Grep takes an output_mode so the caller says how much they want.

    outDir is handled in the dispatcher, so it has to work on a tool that never heard of it.
    """
    import importlib.util as iu
    import shutil
    import tempfile

    spec = iu.spec_from_file_location("srv_spill", os.path.join(ROOT, "rs3-mcp.py"))
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass

    def call(name, args):
        got = mod.handle({"method": "tools/call",
                          "params": {"name": name, "arguments": args}})
        return (got.get("content") or [{}])[0].get("text") or ""

    where = tempfile.mkdtemp(prefix="rs3spill")
    try:
        full = call("rs3_iface", {"id": "1188"})
        spilled = call("rs3_iface", {"id": "1188", "outDir": where})
        check("spill: the receipt is far smaller", len(spilled) * 4 < len(full), True,
              "%d against %d" % (len(spilled), len(full)))

        rec = json.loads(spilled)
        check("spill: it names the file", os.path.exists(rec.get("writtenTo") or ""), True)
        check("spill: and says what is in it", "lengths" in rec or "keys" in rec, True)
        on_disk = json.load(io.open(rec["writtenTo"], encoding="utf-8"))
        check("spill: the file holds the whole answer",
              sorted(on_disk), sorted(json.loads(full)))

        # a tool with no idea outDir exists
        plain = call("rs3_family", {"kind": "npc", "id": "798", "outDir": where})
        check("spill: works on a tool that never heard of it",
              "writtenTo" in json.loads(plain), True)
    finally:
        shutil.rmtree(where, ignore_errors=True)

    # pick, from Grafana: a corner of the document
    one = call("rs3_iface", {"id": "1188", "pick": "root"})
    check("pick: a value instead of the document", len(one) < 60, True, "%d chars" % len(one))
    check("pick: and it is the right value", json.loads(one).get("value"), json.loads(full)["root"])
    whole = json.loads(full)
    lists = [k for k, v in whole.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
    if lists:
        key = lists[0]
        inner = sorted(whole[key][0])[0]
        deep = call("rs3_iface", {"id": "1188", "pick": "%s.0.%s" % (key, inner)})
        check("pick: walks into lists", json.loads(deep).get("value"), whole[key][0][inner])
    miss = json.loads(call("rs3_iface", {"id": "1188", "pick": "nope"}))
    check("pick: a miss says what is there", "thereIs" in miss, True)
    check("pick: and does not pretend to have a value", "value" in miss, False)


def test_answer_size():
    """No answer costs more than the session that asked for it.

    A list tool answered bare with about sixteen thousand tokens, for a question usually asked to
    find one title. Nothing measured it, so nothing said.

    A ceiling on the default answer, not on every answer. Asking for the whole of something big is
    allowed; getting it without asking is not.
    """
    import importlib.util as iu
    from rs3mcp.common import registry

    spec = iu.spec_from_file_location("srv_size", os.path.join(ROOT, "rs3-mcp.py"))
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass

    CEILING = 11000
    worst, who, n = 0, None, 0
    for name, t in registry().items():
        try:
            got = t["fn"]({})
        except Exception:
            continue          # a tool that needs an argument is not in question here
        n += 1
        size = len(json.dumps(mod.lean(got), separators=(",", ":"), default=str))
        if size > worst:
            worst, who = size, name

    check("size: some tools answer bare", n >= 3, True, "%d did" % n)
    check("size: no bare answer over %d characters" % CEILING, worst <= CEILING, True,
          "%s at %d, about %d tokens" % (who, worst, worst // 3))


def test_resources():
    """The second primitive, driven through the dispatcher.

    Tools are called; resources are attached. A uri that reads a file is also a way out of the
    directory, so traversal is asserted rather than assumed.
    """
    import importlib.util as iu
    from rs3mcp import resources

    spec = iu.spec_from_file_location("srv_res", os.path.join(ROOT, "rs3-mcp.py"))
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass

    caps = mod.handle({"method": "initialize", "params": {}})["capabilities"]
    check("resources: declared at initialize", "resources" in caps, True)

    listed = mod.handle({"method": "resources/list"})["resources"]
    check("resources: the guides are listed", len(listed), len(resources._static()))
    for r in listed:
        if not (r.get("uri") and r.get("name") and r.get("mimeType")):
            _FAILS.append(("resources: every entry is complete", r, "uri, name and mimeType", ""))
            break

    temps = mod.handle({"method": "resources/templates/list"})["resourceTemplates"]
    check("resources: the big spaces are templates", len(temps), len(resources._templates()))

    # one of each kind actually reads
    for uri in ("rs3://guide/tools", "rs3://schema/cache"):
        got = mod.handle({"method": "resources/read", "params": {"uri": uri}})
        body = (got.get("contents") or [{}])[0].get("text") or ""
        check("resources: %s has content" % uri, len(body) > 200, True, "%d chars" % len(body))

    # a uri is not a path
    for bad in ("rs3://script/../../../etc/passwd", r"rs3://script/..\..\secrets",
                "rs3://script/", "rs3://script/sub/dir"):
        check("resources: refuses %s" % bad, resources.read(bad), None)

    # and the right error, not the missing method
    try:
        mod.handle({"method": "resources/read", "params": {"uri": "rs3://nothing/here"}})
        _FAILS.append(("resources: a missing one raises", "no error", "NoSuchResource", ""))
    except mod.NoSuchResource:
        pass
    except Exception as e:
        _FAILS.append(("resources: a missing one raises", type(e).__name__, "NoSuchResource", ""))


def test_continuation():
    """A paged answer has to reach the end, and reach it once.

    The cap it replaced named what it was hiding and gave no way to get it. A page that loses a
    row or repeats one is worse than the cap was.
    """
    from rs3mcp.common import page

    src = list(range(37))
    seen, start, rounds = [], 0, 0
    while True:
        win, meta = page(src, start, 10)
        seen += win
        rounds += 1
        if "nextStart" not in meta or rounds > 20:
            break
        start = meta["nextStart"]
    check("page: walks the whole list", seen, src)
    check("page: in the expected number of calls", rounds, 4)
    check("page: past the end is empty, not an error", page(src, 999, 10)[0], [])


def test_protocol():
    """What the server puts on the wire, driven through the real dispatcher.

    The listing and the budget check built the payload separately once and could disagree.
    """
    from rs3mcp.common import registry, server

    import importlib.util as iu
    spec = iu.spec_from_file_location("srv_probe", os.path.join(ROOT, "rs3-mcp.py"))
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass

    init = mod.handle({"method": "initialize", "params": {}})
    check("protocol: version is current", init.get("protocolVersion"), "2025-11-25")
    check("protocol: instructions are sent", bool(init.get("instructions")), True)

    # version negotiation, known and unknown
    asked = mod.handle({"method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    check("protocol: a version we speak comes back", asked.get("protocolVersion"), "2025-03-26")
    asked = mod.handle({"method": "initialize", "params": {"protocolVersion": "1900-01-01"}})
    check("protocol: an unknown version gets ours", asked.get("protocolVersion"), "2025-11-25")

    tools = mod.handle({"method": "tools/list"})["tools"]
    check("protocol: every tool is listed", len(tools), len(registry()))
    check("protocol: listing order never changes", [t["name"] for t in tools],
          [t["name"] for t in mod.handle({"method": "tools/list"})["tools"]])
    names = [t["name"] for t in tools]
    check("protocol: tool names are unique", len(set(names)), len(names))
    check("protocol: tool names keep the spec charset",
          [n for n in names if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n)], [])
    check("protocol: the budget measures the sent shape", len(tools), len(server().listing()))

    missing = [t["name"] for t in tools if not t.get("annotations")]
    check("protocol: every tool carries annotations", missing, [])

    # a wrong readOnlyHint is a false safety claim
    from rs3mcp import addons
    theirs = {}
    for extra in addons.every("side_effects"):
        theirs.update(extra)
    writes = sorted(t["name"] for t in tools
                    if not t["annotations"].get("readOnlyHint", False))
    check("protocol: only the known writers are not read only", writes,
          sorted(["rs3_research"] + [n for n, a in theirs.items() if not a.get("readOnlyHint", False)]))
    outward = sorted(t["name"] for t in tools if t["annotations"].get("openWorldHint"))
    check("protocol: only the ones that fetch are open world", outward,
          sorted(["rs3_wiki", "rs3_research"] + [n for n, a in theirs.items() if a.get("openWorldHint")]))

    call = mod.handle({"method": "tools/call",
                       "params": {"name": "rs3_var", "arguments": {"id": "-60893"}}})
    text = (call.get("content") or [{}])[0].get("text") or ""
    check("protocol: a call returns text", bool(text), True)
    check("protocol: which parses", json.loads(text).get("shift"), 11)

    # structuredContent is optional and doubles the bytes
    check("protocol: and nothing sent twice", "structuredContent" in call, False)

    # compact, and no key whose value is null
    check("protocol: the wire is compact", len(text),
          len(json.dumps(json.loads(text), separators=(",", ":"))))
    check("protocol: and carries no empty keys",
          any(v is None for v in json.loads(text).values()), False)

    # error codes on the real stdio loop
    sent = ["{not json",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "rs3_nothing", "arguments": {}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                        "params": {"uri": "rs3://nothing"}})]
    env = dict(os.environ, RS3MCP_ADDONS="none", RS3_MONITOR="off")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "rs3-mcp.py")], input="\n".join(sent) + "\n",
                       capture_output=True, text=True, timeout=120, env=env)
    got = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    codes = [(r.get("id"), (r.get("error") or {}).get("code")) for r in got]
    check("protocol: bad json is a parse error, notification unanswered", codes[:1], [(None, -32700)])
    check("protocol: discover falls back, unknown tool is invalid params",
          codes[1:], [(1, -32601), (2, -32602), (3, -32002)])

    # stray prints and child output stay off stdout
    noisy = "; ".join([
        "import importlib.util as u, subprocess, sys",
        "s = u.spec_from_file_location('srv', %r)" % os.path.join(ROOT, "rs3-mcp.py"),
        "m = u.module_from_spec(s); s.loader.exec_module(m)",
        "t = {'name': 'rs3_noisy', 'inputSchema': {'type': 'object'}, 'description': 'x', "
        "'fn': lambda a: (print('stray'), subprocess.run([sys.executable, '-c', 'print(1)']), {'ok': 1})[2]}",
        "m.TOOLS.append(t); m.BY_NAME['rs3_noisy'] = t",
        "sys.argv = sys.argv[:1]; m.main()"])
    call = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "rs3_noisy", "arguments": {}}})
    p = subprocess.run([sys.executable, "-c", noisy], input=call + "\n", capture_output=True, text=True,
                       timeout=120, env=env)
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    check("protocol: stdout carries only json-rpc", [line[:40] for line in lines if not line.startswith("{")], [])
    check("protocol: and the noisy call still answers",
          [json.loads(line).get("id") for line in lines if line.startswith("{")], [9])


def test_front_door():
    """rs3 for people, rs3 setup for apps, and the pool log for apps that are not Claude Code."""
    import shutil
    import tempfile
    from rs3mcp import cli, clients, journal, poollog

    lookup = {"name": "rs3_lookup", "inputSchema": {"required": ["kind"], "properties": {
        "kind": {"type": "string"}, "name": {"type": "string"}, "id": {"type": "string"}}}}
    check("rs3: a kind then a name", cli.parse(lookup, ["npc", "Wolf"])[0], {"kind": "npc", "name": "Wolf"})
    check("rs3: a kind then a bare number is the id", cli.parse(lookup, ["npc", "95"])[0], {"kind": "npc", "id": "95"})
    check("rs3: a name of several words stays whole",
          cli.parse(lookup, ["obj", "Bronze", "dagger"])[0], {"kind": "obj", "name": "Bronze dagger"})
    config = {"name": "rs3_config", "inputSchema": {"properties": {
        "type": {"type": "string"}, "id": {"type": "string"}, "search": {"type": "string"}}}}
    check("rs3: config takes its kind first, like lookup", cli.parse(config, ["npc", "81"])[0], {"type": "npc", "id": "81"})
    check("rs3: config kind then words is a search", cli.parse(config, ["obj", "Abyssal", "whip"])[0],
          {"type": "obj", "search": "Abyssal whip"})
    quest = {"name": "rs3_example", "inputSchema": {"required": ["name"], "properties": {"name": {"type": "string"},
                                                                                         "limit": {"type": "integer"}}}}
    check("rs3: a required name takes every word", cli.parse(quest, ["Cook's", "Assistant", "--limit", "3", "--json"]),
          ({"name": "Cook's Assistant", "limit": 3}, True))
    check("rs3: key=value still works", cli.parse(quest, ["name=Druidic Ritual"])[0], {"name": "Druidic Ritual"})
    table = cli.render({"rows": [{"id": 1, "name": "a"}, {"id": 22, "name": None}]}).splitlines()
    check("rs3: uniform rows print as a table", [l.split() for l in table[1:]],
          [["id", "name"], ["1", "a"], ["22", "-"]])

    home = tempfile.mkdtemp()
    try:
        path = os.path.join(home, "mcp.json")
        io.open(path, "w", encoding="utf-8").write('{"mcpServers": {"other": {"command": "x"}}}')
        rec = clients.entry()
        check("setup: added beside what was there", clients._json_with(path, "mcpServers", rec), ("added", None))
        check("setup: a second time changes nothing", clients._json_with(path, "mcpServers", rec), ("already", None))
        doc = json.load(io.open(path, encoding="utf-8"))
        check("setup: the other server survives", sorted(doc["mcpServers"]), ["other", "rs3cache"])
        check("setup: the original is kept", os.path.exists(path + ".before-rs3cache"), True)
        commented = os.path.join(home, "settings.json")
        io.open(commented, "w", encoding="utf-8").write('// mine\n{\n  "theme": "x"\n}\n')
        clients._json_with(commented, "context_servers", rec)
        text = io.open(commented, encoding="utf-8").read()
        check("setup: a settings file keeps its comments", text.startswith("// mine") and '"rs3cache"' in text, True)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    pool = tempfile.mkdtemp()
    old = poollog.POOL
    try:
        poollog.POOL = pool
        log = poollog.Log()
        log.start({"name": "claude-code"})
        check("pool log: Claude Code is not logged twice", log.path, None)
        log.start({"name": "LM Studio", "version": "0.3.20"})
        cid = log.call("rs3_var", {"id": "-60893"})
        log.result(cid, "x" * (poollog.CLIP + 10), False)
        got = journal.Journal({"rs3_var": "game"}, root=os.path.join(pool, "no-claude-here")).poll()
        mine = [e for e in got["events"] if e.get("rs3") == "rs3_var" or e.get("of") == cid]
        check("pool log: the call and its answer reach the pool", [e["kind"] for e in mine], ["call", "result"])
        check("pool log: the app is named", [s["title"] for s in got["sessions"].values()],
              ["LM Studio 0.3.20, through rs3cache"])
        check("pool log: a long answer is clipped", mine[1]["chars"] < poollog.CLIP + 100, True)
    finally:
        poollog.POOL = old
        shutil.rmtree(pool, ignore_errors=True)


TESTS = [test_everything_compiles, test_escape_guard, test_brief_features_logged, test_scrying_pool, test_handback, test_learned_lines, test_handback_replay, test_agent_budget, test_bypass_advice, test_front_door,
         test_toolset_file, test_toolsets, test_spill_and_brief, test_answer_size, test_resources,
         test_continuation, test_protocol, test_claims,
         test_drift_runs, test_scan_cache, test_script_index, test_vars, test_iface,
         test_cache, test_index, test_invariants, test_opcode_specs, test_sprites, test_maplocs, test_maptiles, test_mapwalk, test_mapnear, test_mapscene, test_worldaudit, test_memo, test_agent_view, test_dev_loop, test_zone, test_zone_ui, test_research_store, test_citation_check, test_research_find, test_research_network, test_research_cli, test_research_review, test_research_source_is_not_html, test_research_json_reads_as_text, test_research_gate, test_gateway, test_chat, test_runner, test_context, test_judge, test_unseen_numbers_and_files, test_claim_head, test_second_opinion, test_labels, test_all_registered,test_maprender, test_big_model, test_sprite_names, test_textures, test_docs, test_wiki_offline, test_dds_rounding, test_kept_numbers, test_likeliest, test_stack_context_sets_aside_unread, test_stack_ties_two_unknowns, test_calibration, test_script_names, test_cross_tool, test_regressions]


_TIMES = []
_CARRIED = [0]


def _run_each(tests):
    import time
    from rs3mcp.test import memo
    for t in tests:
        name = t.__name__.replace("test_", "")
        key = "%s.%s" % (t.__module__, t.__name__)
        kept = memo.carried(key)
        if kept is not None:
            _RUN[0] += kept
            _CARRIED[0] += kept
            print("  %-18s ok, unchanged" % name)
            continue
        before, skipped, ran = len(_FAILS), len(_SKIPS), _RUN[0]
        began = time.time()
        memo.start()
        try:
            t()
        except Exception as e:
            _FAILS.append((t.__name__, "raised", str(e)[:70], ""))
        n = len(_FAILS) - before
        # skipped tests rerun
        memo.stop(key, n == 0 and len(_SKIPS) == skipped, _RUN[0] - ran)
        _TIMES.append((time.time() - began, name))
        print("  %-18s %s%s" % (name, "ok" if n == 0 else "%d FAILED" % n,
                                ", some skipped" if len(_SKIPS) > skipped else ""))
    memo.save()


def _report_skips():
    if not _SKIPS:
        return
    by = {}
    for what, reasons in _SKIPS:
        for why in reasons:
            by.setdefault(why, []).append(what)
    print("skipped, for want of what this machine does not have:")
    for why, whats in sorted(by.items()):
        print("   %s: %s" % (why, ", ".join(sorted(set(whats)))))


def run():
    from rs3mcp import addons
    # these tests drive the pool, so ignore RS3_MONITOR
    os.environ.pop("RS3_MONITOR", None)
    print("known-answer tests")
    print("-" * 62)
    _run_each(TESTS)
    # an add-on's tests, on the same tally
    for loader in addons.every("tests"):
        extra = loader()
        print("  add-on %s" % loader.__module__.split(".")[0])
        _run_each(extra)
    print("-" * 62)
    _report_skips()
    if _TIMES:
        slow = sorted(_TIMES, reverse=True)[:5]
        print("ran %d tests in %.0fs; slowest %s" % (len(_TIMES), sum(t for t, _ in _TIMES),
                                                    ", ".join("%s %.0fs" % (n, t) for t, n in slow)))
    if _CARRIED[0]:
        print("%d checks carried from tests nothing has touched since they passed; --full runs them"
              % _CARRIED[0])
    if _FAILS:
        print("%d of %d checks failed" % (len(_FAILS), _RUN[0]))
        for name, got, want, note in _FAILS:
            print("   %-38s got %r want %r" % (name, got, want))
            if note:
                print("      %s" % note)
        return 1
    print("%d checks passed" % _RUN[0])
    return 0


if __name__ == "__main__":
    # through the package, sharing this tally
    from rs3mcp.test import correctness
    sys.exit(correctness.run())
