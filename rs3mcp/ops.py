"""Our opcode specs: one readable line per opcode, checked against the live cache.

A config record is a run of opcodes, each followed by its value, ending on 0x00. A spec says what
follows each opcode:

    type npcs
    index 18                      the cache index. `group N` when one group holds the whole type

    0x02 name        string
    0x0C size        u8
    0x28 recolour    list<{u16 from; u16 to}>
    0xBB morphs      {u8 _flags; u32 var -> var; list[varu16]<u16 -> npcs> ids; u16 fallback -> npcs}
    0xF9 params      params

A type that is not a run of opcodes is one shape for the whole record instead, and every line
after `record` belongs to it:

    type mapzones
    index 23
    group 0
    record {string name; string title; ...}

`layout groups` makes each group one record with the group as its id, `layout files` gives each
file the id group << 16 | file. The default takes ids from full groups, as items and npcs do.

Values
    u8 i8 u16 i16 u24 u32 i32     fixed width, big endian
    varu16 vari16                 one byte, or two when the top bit is set
    varu32 vari32                 two bytes, or four when the top bit is set
    leb128                        seven bits a byte, low first, top bit set while more follow
    string                        latin-1, zero terminated
    pstring                       a zero byte, then a string
    bool                          one byte, 0 or 1 and nothing else
    true false                    no bytes; the opcode being there is the value
    bytes[N]                      N raw bytes, kept as hex
    bytes[n]                      as many bytes as field n says, read earlier in the same struct
    bytes[rest]                   every byte left in the record
    f32                           a big endian float
    params                        the param map: a count, then key and int or string each
    list<T>                       a varu16 count, then that many T
    list[u8]<T>                   the count read as the width named
    list[4]<T>                    a fixed count
    list[n]<T>                    the count is field n, read earlier in the same struct
    list[n-1]<T>                  field n less one, never below nothing. Also n&15 and n>>4
    list[end 0xFF]<T>             T until the next byte is 0xFF, which is taken and ends it
    dbvalues[t]                   a row count, then per row one value per type in list t:
                                  a string for type 36, an i32 for any other
    {T name; T name}              a struct. A name starting with _ is read and not kept
    T name if f==N                only there when field f is N. Also !=, >=, <, and & or !& for bits
    T name if f==N or g==M        there when any of them holds
    T -> type                     what the value is an id of. Decoding ignores it; the graph uses it

Only what the live cache uses goes in a spec. A record that reaches an opcode the spec lacks is
decoded up to it and reported, never dropped, and `solve` tries shapes for that opcode against the
records carrying it.

    python rs3mcp/ops.py verify npcs          every record, opcode by opcode
    python rs3mcp/ops.py decode npcs 798      one record
    python rs3mcp/ops.py solve npcs 0x2A      shapes that make the records carrying it decode whole
"""

import collections
import io
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPECS = os.path.join(HERE, "specs")

FIXED = {"u8": ">B", "i8": ">b", "u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i", "f32": ">f"}
PRIMS = set(FIXED) | {"u24", "varu16", "vari16", "varu32", "vari32", "leb128", "string", "pstring", "bool"}


class SpecError(ValueError):
    pass


class Stop(Exception):
    """Decoding cannot go on: the bytes ran out, or a value was not what its shape allows."""


# parsing a spec

TOKEN = re.compile(r"\s*(->|==|!=|>=|!&|0x[0-9A-Fa-f]+|[A-Za-z_][A-Za-z0-9_]*|\d+|[<>\[\]{};&-])")

COMPARE = {
    "==": lambda v, n: v == n,
    "!=": lambda v, n: v != n,
    ">=": lambda v, n: v >= n,
    "<": lambda v, n: v < n,
    "&": lambda v, n: bool(v & n),
    "!&": lambda v, n: not v & n,
}


def _tokens(text):
    out, at = [], 0
    text = text.rstrip()
    while at < len(text):
        m = TOKEN.match(text, at)
        if not m:
            raise SpecError("cannot read %r" % text[at:at + 20])
        out.append(m.group(1))
        at = m.end()
    return out


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, want=None):
        tok = self.peek()
        if tok is None or (want is not None and tok != want):
            raise SpecError("wanted %s, got %s" % (want or "more", tok))
        self.i += 1
        return tok

    def shape(self):
        tok = self.take()
        if tok in PRIMS:
            node = ("prim", tok)
        elif tok in ("true", "false"):
            node = ("const", tok == "true")
        elif tok == "params":
            node = ("params",)
        elif tok == "dbvalues":
            self.take("[")
            node = ("dbvalues", self.take())
            self.take("]")
        elif tok == "bytes":
            self.take("[")
            n = self.take()
            self.take("]")
            node = ("bytes", ("fixed", int(n)) if n.isdigit() else ("field", n))
        elif tok == "list":
            count = ("prim", "varu16")
            if self.peek() == "[":
                self.take("[")
                c = self.take()
                if c == "end":
                    count = ("end", int(self.take(), 0))
                elif c.isdigit():
                    count = ("fixed", int(c))
                else:
                    count = ("prim", c) if c in PRIMS else ("field", c)
                if count[0] == "field" and self.peek() in ("-", "&", ">"):
                    op = self.take()
                    if op == ">":
                        self.take(">")
                        op = ">>"
                    count = ("field", c, op, int(self.take(), 0))
                self.take("]")
            self.take("<")
            elem = self.shape()
            self.take(">")
            node = ("list", count, elem)
        elif tok == "{":
            fields = []
            while self.peek() != "}":
                ftype = self.shape()
                name = self.take()
                # `u16 from -> materials` reads better than `u16 -> materials from`, so both work
                if self.peek() == "->":
                    self.take("->")
                    ftype = ("ref", self.take(), ftype)
                if self.peek() == "if":
                    self.take("if")
                    tests = []
                    while True:
                        field, op, n = self.take(), self.take(), self.take()
                        if op not in COMPARE:
                            raise SpecError("if takes %s, not %s" % (" ".join(COMPARE), op))
                        if n == "-":
                            n = "-" + self.take()
                        tests.append((field, op, int(n, 0)))
                        if self.peek() != "or":
                            break
                        self.take("or")
                    ftype = ("if", tuple(tests), ftype)
                fields.append((name, ftype))
                if self.peek() == ";":
                    self.take(";")
            self.take("}")
            node = ("struct", fields)
        else:
            raise SpecError("no such shape: %s" % tok)
        if self.peek() == "->":
            self.take("->")
            node = ("ref", self.take(), node)
        return node


def parse_shape(text):
    p = _Parser(_tokens(text))
    node = p.shape()
    if p.peek() is not None:
        raise SpecError("left over: %s" % " ".join(p.t[p.i:]))
    return node


def parse(text, source="spec"):
    """A spec's text into {"type", "index", "ops": {opcode: (name, shape, note)}}."""
    spec = {"type": None, "index": None, "group": None, "layout": "chunked", "end": 0, "ops": {},
            "record": None}
    lines = text.splitlines()
    for n, raw in enumerate(lines, 1):
        line, _, note = raw.partition("#")
        line = line.strip()
        if not line:
            continue
        try:
            head, _, rest = line.partition(" ")
            if head in ("type", "index", "group", "end", "layout"):
                value = rest.strip()
                spec[head] = value if head in ("type", "layout") else int(value, 0)
                continue
            if head == "record":
                # the rest of the file is one shape, read once for the whole record
                body = [rest] + [r.partition("#")[0] for r in lines[n:]]
                spec["record"] = parse_shape(" ".join(body))
                break
            if not head.lower().startswith("0x"):
                raise SpecError("a line starts with an opcode, type, index or end")
            op = int(head, 16)
            name, _, shape = rest.strip().partition(" ")
            if op in spec["ops"]:
                raise SpecError("0x%02X twice" % op)
            spec["ops"][op] = (name, parse_shape(shape), note.strip())
        except (SpecError, ValueError) as e:
            raise SpecError("%s line %d: %s" % (source, n, e))
    if spec["index"] is None:
        raise SpecError("%s names no index" % source)
    return spec


_LOADED = {}


def load(name):
    if name not in _LOADED:
        path = os.path.join(SPECS, name + ".ops")
        _LOADED[name] = parse(io.open(path, encoding="utf-8").read(), os.path.basename(path))
    return _LOADED[name]


def refs(spec):
    """{field path: target type} for every id the spec says points somewhere."""
    out = {}

    def walk(node, path):
        kind = node[0]
        if kind == "ref":
            out[path] = node[1]
            walk(node[2], path)
        elif kind == "if":
            walk(node[2], path)
        elif kind == "list":
            walk(node[2], path + "[]")
        elif kind == "struct":
            for fname, fnode in node[1]:
                if not fname.startswith("_"):
                    walk(fnode, path + "." + fname)

    for op, (name, shape, _note) in spec["ops"].items():
        walk(shape, name)
    return out


# decoding

class Buf:
    def __init__(self, data):
        self.b = data
        self.i = 0

    def need(self, n):
        if self.i + n > len(self.b):
            raise Stop("ran out of bytes")

    def fixed(self, fmt):
        n = struct.calcsize(fmt)
        self.need(n)
        v = struct.unpack_from(fmt, self.b, self.i)[0]
        self.i += n
        return v

    def u24(self):
        self.need(3)
        a, b, c = self.b[self.i:self.i + 3]
        self.i += 3
        return (a << 16) | (b << 8) | c

    def varu16(self):
        self.need(1)
        if self.b[self.i] & 0x80:
            return self.fixed(">H") & 0x7FFF
        return self.fixed(">B")

    def vari16(self):
        self.need(1)
        if self.b[self.i] & 0x80:
            v = self.fixed(">H") & 0x7FFF
            return v - 0x8000 if v & 0x4000 else v
        v = self.fixed(">B")
        return v - 0x80 if v & 0x40 else v

    def varu32(self):
        self.need(2)
        if self.b[self.i] & 0x80:
            return self.fixed(">I") & 0x7FFFFFFF
        return self.fixed(">H")

    def vari32(self):
        self.need(2)
        if self.b[self.i] & 0x80:
            v = self.fixed(">I") & 0x7FFFFFFF
            return v - 0x80000000 if v & 0x40000000 else v
        v = self.fixed(">H")
        return v - 0x8000 if v & 0x4000 else v

    def leb128(self):
        v = shift = 0
        while True:
            if shift > 28:
                raise Stop("a leb128 longer than five bytes")
            b = self.fixed(">B")
            v |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return v

    def string(self):
        end = self.b.find(0, self.i)
        if end < 0:
            raise Stop("a string with no end")
        s = self.b[self.i:end].decode("latin-1")
        self.i = end + 1
        return s

    def pstring(self):
        if self.fixed(">B") != 0:
            raise Stop("a pstring not starting on zero")
        return self.string()


def read(node, buf, scope):
    kind = node[0]
    if kind == "prim":
        p = node[1]
        if p in FIXED:
            return buf.fixed(FIXED[p])
        if p == "bool":
            v = buf.fixed(">B")
            if v > 1:
                raise Stop("bool read %d" % v)
            return v == 1
        return getattr(buf, p)()
    if kind == "const":
        return node[1]
    if kind == "ref":
        return read(node[2], buf, scope)
    if kind == "bytes":
        how = node[1]
        if how == ("field", "rest"):
            n = len(buf.b) - buf.i
        elif how[0] == "field":
            if how[1] not in scope:
                raise Stop("size field %s not read yet" % how[1])
            n = int(scope[how[1]])
        else:
            n = how[1]
        buf.need(n)
        v = buf.b[buf.i:buf.i + n].hex()
        buf.i += n
        return v
    if kind == "params":
        out = {}
        for _ in range(buf.fixed(">B")):
            t = buf.fixed(">B")
            key = buf.u24()
            if t == 0:
                out[key] = buf.fixed(">i")
            elif t == 1:
                out[key] = buf.string()
            else:
                raise Stop("param type %d" % t)
        return out
    if kind == "dbvalues":
        if node[1] not in scope:
            raise Stop("types field %s not read yet" % node[1])
        types = scope[node[1]]
        rows = buf.varu16()
        if rows * len(types) > len(buf.b) - buf.i:
            raise Stop("%d rows with %d bytes left" % (rows, len(buf.b) - buf.i))
        return [[buf.string() if t == 36 else buf.fixed(">i") for t in types] for _ in range(rows)]
    if kind == "list":
        how = node[1]
        if how[0] == "end":
            out = []
            while True:
                buf.need(1)
                if buf.b[buf.i] == how[1]:
                    buf.i += 1
                    return out
                out.append(read(node[2], buf, scope))
        if how[0] == "fixed":
            n = how[1]
        elif how[0] == "field":
            if how[1] not in scope:
                raise Stop("count field %s not read yet" % how[1])
            n = int(scope[how[1]])
            if len(how) > 2:
                op, k = how[2], how[3]
                n = n - k if op == "-" else n & k if op == "&" else n >> k
            n = max(0, n)
        else:
            n = read(how, buf, scope)
        # a count past the bytes left cannot be real, and trying it is how a wrong shape hangs
        if n > len(buf.b) - buf.i:
            raise Stop("a count of %d with %d bytes left" % (n, len(buf.b) - buf.i))
        return [read(node[2], buf, scope) for _ in range(n)]
    if kind == "struct":
        out, inner = {}, dict(scope)
        for fname, fnode in node[1]:
            if fnode[0] == "if":
                for field, _op, _n in fnode[1]:
                    if field not in inner:
                        raise Stop("if on %s, which is not read yet" % field)
                if not any(COMPARE[op](int(inner[field]), n) for field, op, n in fnode[1]):
                    continue
                fnode = fnode[2]
            v = read(fnode, buf, inner)
            inner[fname] = v
            if not fname.startswith("_"):
                out[fname] = v
        return out
    raise SpecError("no reader for %s" % kind)


def decode(spec, data, overrides=None):
    """One record: (fields, problem). problem is None when it decoded to exactly its end.

    Otherwise it says where it stopped and why, and the fields read before that are kept.
    """
    ops = spec["ops"]
    buf, out = Buf(data), {}
    if spec.get("record"):
        try:
            out = read(spec["record"], buf, {})
        except Stop as e:
            return out, {"kind": "broken", "at": buf.i, "why": str(e)}
        if buf.i != len(data):
            return out, {"kind": "trailing", "at": buf.i, "bytes": len(data) - buf.i}
        return out, None
    while buf.i < len(data):
        at = buf.i
        op = buf.fixed(">B")
        entry = (overrides or {}).get(op) or ops.get(op)
        if entry is None:
            if op == spec["end"]:
                if buf.i != len(data):
                    return out, {"kind": "trailing", "at": buf.i, "bytes": len(data) - buf.i}
                return out, None
            return out, {"kind": "unknown", "opcode": op, "at": at}
        name, shape = entry[0], entry[1]
        try:
            out[name] = read(shape, buf, {})
        except Stop as e:
            return out, {"kind": "broken", "opcode": op, "at": at, "why": str(e)}
    return out, {"kind": "no end", "at": buf.i}


# the live cache

def records(spec, root=None, minors=None):
    """(config id, bytes) for every record of this type in the game cache, or only those in the
    given groups."""
    from . import cachefile
    root = root or cachefile.GAME
    desc = cachefile.descriptor(spec["index"], root)
    if not desc:
        return
    layout = spec.get("layout", "chunked")
    # from the whole index: a part of it can have a smaller highest file id
    size = cachefile.chunk(desc) if layout == "chunked" else None
    if minors is not None:
        desc = {m: desc[m] for m in minors if m in desc}
    if spec["group"] is not None:
        # one group holds the whole type and the file id is the config id
        files = cachefile.files(spec["index"], spec["group"], root, desc) or {}
        for fid in sorted(files):
            yield fid, files[fid]
        return
    for minor in sorted(desc):
        files = cachefile.files(spec["index"], minor, root, desc) or {}
        for fid in sorted(files):
            if layout == "groups":
                # a group is one record, and its id is the group
                yield minor, files[fid]
            elif layout == "files":
                yield (minor << 16) | fid, files[fid]
            else:
                yield minor * size + fid, files[fid]


def verify(name, root=None):
    """Every record of a type through its spec, with the evidence per opcode."""
    spec = load(name)
    uses = collections.Counter()
    problems = collections.Counter()
    examples = {}
    whole = total = 0
    for cid, data in records(spec, root):
        total += 1
        fields, problem = decode(spec, data)
        for op, (fname, _shape, _note) in spec["ops"].items():
            if fname in fields:
                uses[op] += 1
        if problem is None:
            whole += 1
            continue
        key = (problem["kind"], problem.get("opcode"))
        problems[key] += 1
        examples.setdefault(key, cid)
    unused = sorted(op for op in spec["ops"] if not uses[op])
    return {
        "type": name, "records": total, "decodedWhole": whole,
        "share": round(100.0 * whole / total, 3) if total else 0.0,
        "opcodes": {"0x%02X" % op: {"name": spec["ops"][op][0], "records": uses[op]}
                    for op in sorted(spec["ops"])},
        "problems": [{"kind": k, "opcode": None if o is None else "0x%02X" % o, "records": n,
                      "example": examples[(k, o)]}
                     for (k, o), n in problems.most_common()],
        "neverUsed": ["0x%02X" % op for op in unused],
    }


CANDIDATES = [
    "true", "u8", "i8", "u16", "i16", "u24", "u32", "i32", "varu16", "vari16", "varu32", "vari32",
    "string", "params", "{u8 a; u8 b}", "{u16 a; u16 b}", "{u16 a; u8 b}", "{u8 a; u16 b}",
    "{u32 a; u32 b}", "list<u8>", "list<i8>", "list<u16>", "list<varu32>", "list<{u16 a; u16 b}>",
    "list[u8]<u8>", "list[u8]<u16>", "list[u8]<u32>", "list[u8]<string>", "bytes[2]", "bytes[3]",
    "bytes[4]", "bytes[5]", "bytes[6]", "bytes[8]",
]


def solve(name, opcode, root=None, limit=8, sample=400, spec=None):
    """Shapes for one opcode, ranked by how many records carrying it then decode whole.

    A shape that fits every record is a strong reading, not a proof: two shapes of the same width
    fit the same records, and only the values say which. The top few are shown with a sample of
    what each reads, for that reason. Tried on up to `sample` records that stop at the opcode.
    """
    spec = spec or load(name)
    carrying = []
    for cid, data in records(spec, root):
        _fields, problem = decode(spec, data)
        if problem and problem.get("opcode") == opcode:
            carrying.append((cid, data))
            if len(carrying) >= sample:
                break
    if not carrying:
        return {"type": name, "opcode": "0x%02X" % opcode, "records": 0,
                "note": "no record stops at this opcode"}

    ranked = []
    for text in CANDIDATES:
        shape = parse_shape(text)
        trial = {opcode: ("candidate", shape, "")}
        fits, samples = 0, []
        for cid, data in carrying:
            fields, problem = decode(spec, data, trial)
            if problem is None:
                fits += 1
                if len(samples) < 3:
                    samples.append({"id": cid, "value": fields.get("candidate")})
        if fits:
            ranked.append({"shape": text, "fits": fits, "of": len(carrying), "samples": samples})
    ranked.sort(key=lambda r: (-r["fits"], len(r["shape"])))
    return {"type": name, "opcode": "0x%02X" % opcode, "records": len(carrying),
            "shapes": ranked[:limit]}


def main(argv):
    import json
    if len(argv) < 2 or argv[0] not in ("verify", "decode", "solve"):
        print(__doc__)
        return 1
    cmd, name = argv[0], argv[1]
    if cmd == "verify":
        got = verify(name)
    elif cmd == "decode":
        spec, want = load(name), int(argv[2])
        got = next(({"id": cid, "fields": f, "problem": p}
                    for cid, data in records(spec) if cid == want
                    for f, p in [decode(spec, data)]), {"id": want, "problem": "no such record"})
    else:
        got = solve(name, int(argv[2], 0))
    print(json.dumps(got, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(HERE))
    from rs3mcp import ops as _self
    sys.exit(_self.main(sys.argv[1:]))
