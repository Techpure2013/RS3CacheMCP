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
import tempfile
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
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

    # an id both domains use answers the VarPlayer and says so, rather than asking again by id
    both = tool_var({"id": "12"})
    check("vars: a shared id gives the VarPlayer", both.get("domain"), "VarPlayer")
    check("vars: and names the other", (both.get("sameIdIn") or {}).get("domain"), "VarClient")
    check("vars: domain picks the other", tool_var({"id": "12", "domain": "varclient"}).get("domain"),
          "VarClient")

    # the names below come from the name lists, which the cache does not hold
    if not needs("var names", "name lists"):
        return
    check("vars: name", r.get("name"), "QUEST_EASTER26_BOSS_BATTLE_ROUND")
    check("vars: base name", (r.get("base") or {}).get("name"), "QUEST_EASTER26_PERM")
    check("vars: transmits the base", r.get("transmits"), "QUEST_EASTER26_PERM")

    # the globetrotter layout, a question once asked and worth pinning
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
    # player varbits, from the cache since 2026-09-13; the name list csv had 50,172
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
    # the index must agree with a direct scan, on an item known to have inbound links
    target = idx.execute("select target_id from item_link group by target_id "
                         "having count(*) between 2 and 8 limit 1").fetchone()[0]
    from_index = {(f, o) for f, o in idx.execute(
        "select field, owner_id from item_link where target_id=?", (target,)) if o != target}
    from_scan = set()
    for field in ITEM_LINKS:
        # anchor on the suffix exactly as the real fallback does: a bare %"noteTemplate":121%
        # also matches 1212, which made this reference wrong rather than the index
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

    # a mask can never claim more than the 32 bits of a varp
    over = [m for m in (0, 1, 255, 0xFFFFFFFF, 14336, 268402688) if _width(m)[0] > 32]
    check("invariant: no mask exceeds 32 bits", over, [])

    # width must equal popcount, always
    wrong = [m for m in (0, 1, 3, 255, 14336, 0xFFFFFFFF, 0b1011)
             if _width(m)[0] != bin(m).count("1")]
    check("invariant: width equals popcount", wrong, [])


def test_opcode_specs():
    """Our opcode format: what it reads, what it refuses, and that the specs hold on the live cache."""
    from rs3mcp import ops

    # every value shape, on bytes worked out by hand
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

    # the shapes that depend on what came before them
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

    # the specs against the game's own cache, every record
    from rs3mcp import cachefile
    # every record of every type decodes, interfaces included since the kind 13 and 16 layouts
    known = {}
    if os.path.exists(os.path.join(cachefile.GAME, "js5-22.jcache")):
        for name in sorted(f[:-4] for f in os.listdir(ops.SPECS) if f.endswith(".ops")):
            got = ops.verify(name)
            check("ops: every %s record decodes to its end" % name, got["records"] - got["decodedWhole"],
                  known.get(name, 0), str(got["problems"][:2]))
            check("  and every opcode in the %s spec is used" % name, got["neverUsed"], [])

        # the solver finds a shape it was not told: take combat level out and ask for it back
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

    # paletted: a 2x2 frame at (1, 0) on a 3x2 canvas, stored column by column, red and blue
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

    # true colour: one header, two 1x2 frames each followed by its alpha
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


def test_maplocs():
    """Map placements: id deltas, packed positions, the extras a flags byte names, and live squares."""
    import struct
    from rs3mcp import cachefile, maplocs

    # loc 32766 + 1 through a tailed id (0x7FFF then 0), placed at plane 1 x 5 y 9 and again one
    # tile on, the second with a z offset of -22; then loc 32767 once, bare
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
    compared = 0
    for tex in stored[::97]:
        a, b = textures.bmp(tex), textures.dds(tex)
        if not a or not b or a[:2] != b[:2]:
            continue
        pa, pb, n = a[2], b[2], len(a[2]) // 4
        # red and green: normal maps keep their blue only in the bmp copy
        err = sum(abs(pa[i * 4] - pb[i * 4]) + abs(pa[i * 4 + 1] - pb[i * 4 + 1]) for i in range(n)) / (2.0 * n)
        check("textures: dds %d decodes close to its bmp" % tex, err < 12, True, "%.1f" % err)
        compared += 1
        if compared == 3:
            break


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

    # a script someone can run is on the commands page, or says here why it is not
    reached = {"rs3.py": "the launcher behind rs3",
               "correctness.py": "run by rs3-mcp.py --test", "golden.py": "run by rs3-mcp.py --golden and --diff"}
    # scripts beside the tools that belong to an add-on, and are documented with it
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

    # a varbit's base must be a real var that resolves on its own
    v = tool_var({"id": "-49770"})
    base = (v.get("base") or {}).get("id")
    check("cross: capsule cooldown base id", base, 10064)
    b = tool_var({"id": str(base)})
    check("cross: and it packs the varbit back", any(s["varbit"] == -49770 for s in (b.get("packs") or [])), True)
    if needs("var names across tools", "name lists"):
        check("cross: that base resolves by id", b.get("name"), "EGWD_REWARDS_VARP2")

    # an interface component's packed id must resolve to the same component
    c = tool_iface({"id": "1188:5"})
    packed = c.get("packedID")
    again = tool_iface({"id": str(packed)})
    check("cross: packed id round-trips through the tool", again.get("at"), "1188:5")

    # an item found by lookup must carry the id the cache holds
    it = tool_lookup({"kind": "item", "name": "Abyssal whip"})
    ids = [r["id"] for r in (it.get("results") or it.get("matches") or [])] \
        if isinstance(it, dict) else []
    check("cross: abyssal whip resolves", 4151 in ids or it.get("id") == 4151, True)


def test_regressions():
    # Every tool named in the startup instructions has to exist. A renamed tool would otherwise
    # leave a step pointing at nothing, and nobody reads instructions looking for typos.
    from rs3mcp import howto
    from rs3mcp.common import registry
    known = set(registry())
    dangling = [t for t in howto._tools_named() if t not in known]
    check("startup instructions name only real tools", dangling, [])
    check("  and cover the checks that gate a claim",
          all(w in howto.instructions() for w in ("--selftest", "rs3_drift", "soak.py")), True)

    # No source file carries a control character from a mangled escape. A heredoc turns a written
    # \b into a real backspace: the file parses, the pattern matches nothing, nothing complains.
    # The tests are covered too, because a mangled escape in a check weakens the check.
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

    # a substring of a token has to be found: SETHIDE lives inside IF_SETHIDE
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
    # alternation without regex=true is literal, and the empty answer has to say so
    r = S.tool_script({"pattern": "inv_getvar|oc_name", "limit": 5})
    check("script index: a regex-looking pattern searched literally says so",
          (r.get("found"), "regex=true" in (r.get("note") or "")), (0, True))
    r = S.tool_script({"pattern": "inv_getvar|oc_name", "regex": True, "limit": 5})
    check("script index: and finds hits once regex=true", r.get("found", 0) > 0, True)

    # Punctuation used to fall through to reading all 21,098 files, which is 1.3 seconds. It is
    # narrowed by the token-shaped runs inside it instead, and the only thing that matters about
    # that is it must not lose a hit: the narrowed set has to contain every file a full scan finds.
    for pat in ("SETHIDE(", "script18930(", "SCALE(300,", "comp(1786,"):
        cand = S._candidates(pat, False)
        q = pat.lower().encode()
        truth = {f for f in S._files()
                 if q in io.open(os.path.join(S.CLIENTSCRIPT, f), "rb").read().lower()}
        check("script index: %s narrows without losing a hit" % pat,
              cand is None or not (truth - cand), True)

    # a pattern with no token-shaped run in it cannot be narrowed at all
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
    claims.run(check, skip)


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
    # the pool also reads what other apps logged here, and a machine that has used it has some
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
            check("pool: serves the page", "<title>Scrying Pool</title>" in page, True)
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
        # an unlisted tool still answers: the instructions name tools the listing may not carry
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

    # pick, from Grafana's get_dashboard_property: most questions want a corner of the document
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

    # and the right error, which is not the one for a missing method
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
    check("protocol: version is current", init.get("protocolVersion"), "2025-06-18")
    check("protocol: instructions are sent", bool(init.get("instructions")), True)

    tools = mod.handle({"method": "tools/list"})["tools"]
    check("protocol: every tool is listed", len(tools), len(registry()))
    check("protocol: the budget measures the sent shape", len(tools), len(server().listing()))

    missing = [t["name"] for t in tools if not t.get("annotations")]
    check("protocol: every tool carries annotations", missing, [])

    # A wrong readOnlyHint is a false safety claim. Named, not counted: the core's by name here, and
    # an add-on's by the names it offered itself.
    from rs3mcp import addons
    theirs = {}
    for extra in addons.every("side_effects"):
        theirs.update(extra)
    writes = sorted(t["name"] for t in tools
                    if not t["annotations"].get("readOnlyHint", False))
    check("protocol: only the known writers are not read only", writes,
          sorted([n for n, a in theirs.items() if not a.get("readOnlyHint", False)]))
    outward = sorted(t["name"] for t in tools if t["annotations"].get("openWorldHint"))
    check("protocol: only the ones that fetch are open world", outward,
          sorted(["rs3_wiki"] + [n for n, a in theirs.items() if a.get("openWorldHint")]))

    call = mod.handle({"method": "tools/call",
                       "params": {"name": "rs3_var", "arguments": {"id": "-60893"}}})
    text = (call.get("content") or [{}])[0].get("text") or ""
    check("protocol: a call returns text", bool(text), True)
    check("protocol: which parses", json.loads(text).get("shift"), 11)

    # structuredContent is optional in the spec and is the same bytes twice. It was sent for one
    # day and measured at +11,904 tokens over ten answers.
    check("protocol: and nothing sent twice", "structuredContent" in call, False)

    # Compact, and no key whose value is null. Both are per-call savings on every answer.
    # Re-serialising is the only exact test: ", " appears inside string values too.
    check("protocol: the wire is compact", len(text),
          len(json.dumps(json.loads(text), separators=(",", ":"))))
    check("protocol: and carries no empty keys",
          any(v is None for v in json.loads(text).values()), False)


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


TESTS = [test_everything_compiles, test_scrying_pool, test_bypass_advice, test_front_door,
         test_toolset_file, test_toolsets, test_spill_and_brief, test_answer_size, test_resources,
         test_continuation, test_protocol, test_drift_runs,
         test_claims, test_scan_cache, test_script_index, test_vars, test_iface,
         test_cache, test_index, test_invariants, test_opcode_specs, test_sprites, test_maplocs, test_big_model, test_sprite_names, test_textures, test_docs, test_wiki_offline, test_calibration, test_cross_tool, test_regressions]


def _run_each(tests):
    for t in tests:
        before, skipped = len(_FAILS), len(_SKIPS)
        try:
            t()
        except Exception as e:
            _FAILS.append((t.__name__, "raised", str(e)[:70], ""))
        n = len(_FAILS) - before
        print("  %-18s %s%s" % (t.__name__.replace("test_", ""), "ok" if n == 0 else "%d FAILED" % n,
                                ", some skipped" if len(_SKIPS) > skipped else ""))


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
    # these tests drive the scrying pool, so somebody's RS3_MONITOR=off cannot apply to them
    os.environ.pop("RS3_MONITOR", None)
    print("known-answer tests")
    print("-" * 62)
    _run_each(TESTS)
    # an add-on's tests, after the shared ones and on the same tally
    for loader in addons.every("tests"):
        extra = loader()
        print("  add-on %s" % loader.__module__.split(".")[0])
        _run_each(extra)
    print("-" * 62)
    _report_skips()
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
    # through the package, so an add-on's tests share this tally rather than a second copy of it
    from rs3mcp.test import correctness
    sys.exit(correctness.run())
