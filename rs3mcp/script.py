"""
The decompiled clientscripts, searched directly.

These settle what the cache cannot. The cache says a varbit exists; only the scripts say what writes
it, what reads it, and what the value means. Same for interface behaviour and quest gating.

Indexed by token, which reversed an earlier decision. The argument against was that the
corpus scans in about a second, and that holds for a warm cache and a pattern that matches
early. A pattern matching nothing reads all 21,099 files, and under a cold cache one took
123 seconds during a soak.

The index holds every identifier token. A pattern made only of identifier characters must
lie inside some token, so the tokens containing it name every file that can match --
verified identical to a full scan, not merely a superset. Anything with punctuation, a
space, or a regex still scans, because those can straddle a token boundary.

Tools:
  rs3_script       search the corpus
  rs3_script_read  one script in full
"""

import io
import os
import re

from .common import CLIENTSCRIPT

HEADER = re.compile(r"^//\[([^\]]*)\]")
NUM = re.compile(r"clientscript-(\d+)\.ts$")

_FILES = {"list": None}


def _files():
    if _FILES["list"] is None:
        if not os.path.isdir(CLIENTSCRIPT):
            _FILES["list"] = []
        else:
            names = [n for n in os.listdir(CLIENTSCRIPT) if n.endswith(".ts")]
            # numeric order, so results read in script id order rather than 1, 10, 100
            names.sort(key=lambda n: int(NUM.search(n).group(1)) if NUM.search(n) else 1 << 30)
            _FILES["list"] = names
    return _FILES["list"]


def _sid(name):
    m = NUM.search(name)
    return int(m.group(1)) if m else None


def _tag(text):
    h = HEADER.match(text)
    return h.group(1) if h else None


_IDX = {}
# Only a pattern made entirely of identifier characters can be answered from a token
# index. Any occurrence of one must lie inside a token, so a token containing it is a
# complete list of the files that can match. A pattern with punctuation or a space can
# straddle a boundary and has to be scanned for.
IDENT_ONLY = re.compile(r"^[A-Za-z0-9_]{3,}$")
# alternation, an inline flag, a wildcard run or an escape class
LOOKS_REGEX = re.compile(r"\||\(\?|\.\*|\.\+|\\[bdswBDSW]")
# a pattern that is really asking "what touches this id", which the reference
# index answers without opening anything
_REFERENCE_SHAPE = re.compile(
    r"\b(comp|varbitplayer|varplayer|varclient)[\(_]\s*(\d+)")


def _index():
    """The shared sidecar, probed on script_token. See common.sidecar."""
    from .common import sidecar
    return sidecar("script_token")


def _runs(pattern):
    """The parts of a pattern the token index could possibly hold.

    The index takes [A-Za-z_][A-Za-z0-9_]{2,}, so no token begins with a digit. Intersecting on
    `1786` dropped all 14 real hits of `comp(1786,`.
    """
    return [r for r in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", pattern)]


def _candidates(pattern, regex):
    """Which files can possibly match, or None meaning look at all of them.

    Substring of a token, not a whole one: SETHIDE has to find IF_SETHIDE. Scanning 124,451
    short tokens is 30ms against reading 22MB. A pattern with punctuation narrows by its longest
    identifier, so the result stays a superset and is searched properly after.
    """
    if regex:
        return None
    keys = [pattern] if IDENT_ONLY.match(pattern) else _runs(pattern)
    if not keys:
        return None
    idx = _index()
    if idx is None:
        return None
    out = None
    for key in keys:
        rows = idx.execute(
            "select distinct f.name from script_token t join script_file f "
            "on f.file_id = t.file_id where t.token like ?",
            ("%" + key.lower() + "%",)).fetchall()
        files = {r[0] for r in rows}
        out = files if out is None else (out & files)
        if not out:
            break
    return out


def tool_script(args):
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("give a pattern to search for")
    name = str(args.get("name") or "").strip().lower()
    limit = min(int(args.get("limit", 20)), 100)
    # Collect past the window so a later page can be sliced off. The scan walks the files in
    # sorted order, so page two is the same hits page one stopped before.
    start = max(0, int(args.get("start") or 0))
    ceiling = start + limit
    context = min(int(args.get("context", 2)), 10)
    regex = bool(args.get("regex"))

    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.I)
    except re.error as e:
        raise ValueError("bad regex: %s" % e)

    # the cheap test first: most files are thrown out on a raw byte check without being decoded,
    # which is what makes scanning the whole corpus per request affordable
    quick = None
    if not regex:
        quick = pattern.lower().encode("utf-8", "replace")

    # `a|b` or `(?i)x` without regex=true is searched as those exact characters, finds nothing,
    # and reads as "not in the corpus". Said plainly, since that answer gets believed.
    literal = None
    if not regex and LOOKS_REGEX.search(pattern):
        literal = ("the pattern was searched as literal text, and it looks like a regex; pass "
                   "regex=true to search it as one")

    only = _candidates(pattern, regex)
    if only is not None and not only:
        return {"found": 0, "filesScanned": 0, "hits": [],
                "note": literal or "that token appears nowhere in the corpus. Answered from the "
                        "index without opening a file; a substring or a regex still scans."}

    hits, scanned, matched_files = [], 0, 0
    for fname in _files():
        if only is not None and fname not in only:
            continue
        path = os.path.join(CLIENTSCRIPT, fname)
        try:
            raw = io.open(path, "rb").read()
        except Exception:
            continue
        scanned += 1
        if quick is not None and quick not in raw.lower():
            continue

        text = raw.decode("utf-8", "replace")
        if name:
            t = _tag(text)
            if not t or name not in t.lower():
                continue

        lines = text.split("\n")
        found_here = False
        for i, line in enumerate(lines):
            if rx.search(line):
                found_here = True
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                hits.append({
                    "script": _sid(fname),
                    "tag": _tag(text),
                    "file": fname,
                    "line": i + 1,
                    "code": "\n".join(lines[lo:hi]),
                })
                if len(hits) >= ceiling:
                    break
        if found_here:
            matched_files += 1
        if len(hits) >= ceiling:
            break

    window = hits[start:]
    notes = [literal] if literal and not hits else []
    if len(hits) >= ceiling:
        notes.append("more hits exist; pass start=%d for the next %d" % (ceiling, limit))
    # A typed reference is a question rs3_script_refs answers from an index rather than by
    # reading files. `comp(1786,` scans 7,256 of them here and is a keyed lookup there, because
    # the decompiler wrote the type alongside the number.
    ref = _REFERENCE_SHAPE.search(pattern)
    if ref:
        kind = {"comp": "iface", "varbitplayer": "varbit",
                "varplayer": "varp", "varclient": "varclient"}[ref.group(1)]
        notes.append("that is a typed reference; rs3_script_refs kind=%s id=%s answers it from an "
                     "index instead of by reading %d files" % (kind, ref.group(2), scanned))
    rec = {"found": len(hits), "filesMatched": matched_files, "filesScanned": scanned,
           "truncated": len(hits) >= ceiling,
           "start": start, "shown": len(window),
           "hits": window,
           "note": " ".join(notes) or None}
    if len(hits) >= ceiling:
        rec["nextStart"] = ceiling
    return rec


def tool_script_read(args):
    who = str(args.get("script") or "").strip()
    if not who:
        raise ValueError("give a script number or a name from its header")

    if who.isdigit():
        fname = "clientscript-%d.ts" % int(who)
        path = os.path.join(CLIENTSCRIPT, fname)
        if not os.path.exists(path):
            return {"found": 0, "note": "no script numbered " + who}
        text = io.open(path, encoding="utf-8", errors="replace").read()
        return {"found": 1, "script": int(who), "tag": _tag(text),
                "lines": text.count("\n") + 1, "source": text}

    low = who.lower()
    out = []
    for fname in _files():
        path = os.path.join(CLIENTSCRIPT, fname)
        try:
            head = io.open(path, "rb").read(200).decode("utf-8", "replace")
        except Exception:
            continue
        t = _tag(head)
        if t and low in t.lower():
            text = io.open(path, encoding="utf-8", errors="replace").read()
            out.append({"script": _sid(fname), "tag": t, "lines": text.count("\n") + 1,
                        "source": text if len(out) == 0 else None})
            if len(out) >= 20:
                break
    return {"found": len(out), "scripts": out,
            "note": "only the first match comes back whole; ask for one by number for the rest"
            if len(out) > 1 else None}


TOOLS = [
    {"name": "rs3_script", "fn": tool_script,
     "description": "Search the 21,098 decompiled clientscripts for a pattern, with surrounding "
                    "lines. These settle what the cache cannot: what writes a varbit, what reads "
                    "it, what a value means. About a second. rs3_script_read takes the id this "
                    "returns; rs3_script_refs goes the other way, from a varbit or item id.",
     "inputSchema": {"type": "object", "required": ["pattern"], "properties": {
         "pattern": {"type": "string", "description": "literal text, or a regex with regex=true"},
         "regex": {"type": "boolean", "description": "treat the pattern as a regex"},
         "name": {"type": "string", "description": "only scripts whose header name contains this"},
         "limit": {"type": "integer", "description": "max hits, default 20"},
         "start": {"type": "integer", "description": "pass the nextStart a truncated answer gives"},
         "context": {"type": "integer", "description": "lines either side, default 2"}}}},
    {"name": "rs3_script_read", "fn": tool_script_read,
     "description": "One clientscript whole, by its number or by a name from its header comment.",
     "inputSchema": {"type": "object", "required": ["script"], "properties": {
         "script": {"type": "string", "description": "an id from rs3_script, or a name like quicksort"}}}},
]
