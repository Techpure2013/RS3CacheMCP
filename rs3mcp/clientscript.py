"""Clientscripts straight out of the cache: the file format, and the instructions in it.

Index 12 holds one script per group. A script is a byte, its instructions, then a footer read
from the end backwards:

    byte0 | instructions | u32 count | u16 local ints, strings, longs | u16 arg ints, strings,
    longs | switch tables | u16 switch table size

An instruction is a u16 opcode and one immediate, whose width the opcode decides. Jagex scrambles
the opcode numbers on every update, so reading one needs the map for that update: scrambled number
to what the op is and how wide its immediate is. The map lives in data/, beside the cache it fits.

    python rs3mcp/clientscript.py verify          every script, instruction by instruction
    python rs3mcp/clientscript.py dis 1000        one script as instructions
"""

import io
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rs3mcp import cachefile  # noqa: E402

MAJOR = 12
OPMAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opcodes", "clientscript-opmap.json")
NAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opcodes", "clientscript-opnames.json")

# immediate kinds, and the const kinds a push carries in its first byte
IMM = ("byte", "int", "tribyte", "const")
CONST = {0: "int", 1: "long", 2: "string"}


class Bad(Exception):
    pass


def footer(raw):
    """(instruction bytes, header dict) for one script file."""
    end = len(raw)

    def take(n, fmt):
        nonlocal end
        if end - n < 1:
            raise Bad("footer runs past the start")
        end -= n
        return struct.unpack_from(fmt, raw, end)[0]

    size = take(2, ">H")
    end -= size
    if end < 1:
        raise Bad("switch tables run past the start")
    switches = _switches(raw[end:end + size])
    head = {"switches": switches}
    for key in ("argLongs", "argStrings", "argInts", "localLongs", "localStrings", "localInts"):
        head[key] = take(2, ">H")
    head["count"] = take(4, ">I")
    head["byte0"] = raw[0]
    return raw[1:end], head


def _switches(blob):
    if not blob:
        return []
    out, i = [], 1
    for _ in range(blob[0]):
        n = struct.unpack_from(">H", blob, i)[0]
        i += 2
        table = {}
        for _ in range(n):
            value, jump = struct.unpack_from(">iI", blob, i)
            i += 8
            table[value] = jump
        out.append(table)
    if i != len(blob):
        raise Bad("switch tables leave %d bytes" % (len(blob) - i))
    return out


def load_map(path=OPMAP):
    """{scrambled: (op id, immediate kind)} for the cache on disk."""
    d = json.load(io.open(path, encoding="utf-8"))
    return {int(k): (v["op"], v["imm"]) for k, v in d["ops"].items()}


def names():
    if not os.path.exists(NAMES):
        return {}
    return {int(k): v for k, v in json.load(io.open(NAMES, encoding="utf-8")).items()}


def instructions(code, count, opmap, starts=None):
    """[(op id, immediate)] for count instructions, which must use every byte. starts, when given,
    gets where each instruction begins in code."""
    out, i, n = [], 0, len(code)
    for _ in range(count):
        if i + 2 > n:
            raise Bad("ran out after %d of %d instructions" % (len(out), count))
        if starts is not None:
            starts.append(i)
        scrambled = (code[i] << 8) | code[i + 1]
        i += 2
        got = opmap.get(scrambled)
        if got is None:
            raise Bad("opcode %d at instruction %d is not in the map" % (scrambled, len(out)))
        op, kind = got
        if kind == "byte":
            imm = code[i]
            i += 1
        elif kind == "int":
            imm = struct.unpack_from(">i", code, i)[0]
            i += 4
        elif kind == "tribyte":
            imm = (code[i] << 16) | (code[i + 1] << 8) | code[i + 2]
            i += 3
        else:
            sub = code[i]
            i += 1
            if sub == 0:
                imm = ("int", struct.unpack_from(">i", code, i)[0])
                i += 4
            elif sub == 1:
                imm = ("long", struct.unpack_from(">q", code, i)[0])
                i += 8
            elif sub == 2:
                stop = code.find(0, i)
                if stop < 0:
                    raise Bad("a string const with no end")
                imm = ("string", code[i:stop].decode("latin-1"))
                i = stop + 1
            else:
                raise Bad("const kind %d at instruction %d" % (sub, len(out)))
        if i > n:
            raise Bad("instruction %d runs past the code" % len(out))
        out.append((op, imm))
    if i != n:
        raise Bad("%d instructions leave %d bytes" % (count, n - i))
    return out


def scripts(root=None):
    """(script id, raw bytes) for every script in the cache."""
    root = root or cachefile.GAME
    desc = cachefile.descriptor(MAJOR, root)
    for group in sorted(desc or {}):
        files = cachefile.files(MAJOR, group, root, desc) or {}
        for fid in files:
            yield group, files[fid]


def read(raw, opmap):
    code, head = footer(raw)
    head["code"] = instructions(code, head["count"], opmap)
    return head


def verify(root=None, opmap=None):
    opmap = opmap or load_map()
    total, bad, first = 0, {}, {}
    for sid, raw in scripts(root):
        total += 1
        try:
            read(raw, opmap)
        except (Bad, struct.error, IndexError) as e:
            why = str(e).split(" at ")[0]
            bad[why] = bad.get(why, 0) + 1
            first.setdefault(why, sid)
    return {"scripts": total, "read": total - sum(bad.values()),
            "problems": [{"why": w, "scripts": n, "example": first[w]}
                         for w, n in sorted(bad.items(), key=lambda x: -x[1])]}


def _dis(sid):
    opmap, nm = load_map(), names()
    files = cachefile.files(MAJOR, sid) or {}
    if not files:
        raise SystemExit("no script %d" % sid)
    raw = next(iter(files.values()))
    head = read(raw, opmap)
    for k, v in head.items():
        if k != "code":
            print("%-13s %s" % (k, v))
    for i, (op, imm) in enumerate(head["code"]):
        print("%5d  %-28s %s" % (i, nm.get(op, "op%d" % op), imm))


if __name__ == "__main__":
    if sys.argv[1:2] == ["verify"]:
        print(json.dumps(verify(), indent=1))
    elif sys.argv[1:2] == ["dis"]:
        _dis(int(sys.argv[2]))
    else:
        raise SystemExit(__doc__)
