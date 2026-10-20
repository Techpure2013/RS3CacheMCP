"""Numbers written into the source, checked against the data they describe.

A description saying "6.8M placed locs" is a claim about a dataset, written once. The databases
here are rebuilt from cache dumps, so the day a dump is regenerated those sentences start being
confidently wrong. Each entry pairs the text as written with the query that produces it.

  the number disagrees      the data moved and the sentence did not
  the text is not found     it was reworded, so nothing is checking it any more

The second matters as much as the first. Only live counts belong here: a finished measurement, a
protocol constant or a worked example cannot drift.
"""

import io
import json
import os
import re
import sqlite3

from rs3mcp.common import CLIENTSCRIPT, ROOT as DATA

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MEM = {}

# a number worth a second look
NUMBERS = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?M\b|\d{3,}")
# listing characters, each bump in rs3api/ledger.md
LISTING_BUDGET = 39700
# unchecked numbers in strings, see rs3api/ledger.md
WRITTEN_NUMBERS = 181


def written_numbers():
    """How many numbers are written into the server's strings.

    Through the same fingerprinted disk cache as the other tree scans. Editing any source rebuilds
    it.
    """
    from rs3mcp.common import cached_scan, tree_fingerprint
    stamp = tree_fingerprint([os.path.join(ROOT, "rs3mcp")], ".py")
    return cached_scan("written-numbers", stamp,
                       lambda: sum(len(NUMBERS.findall(s)) for s in
                                   _strings("rs3mcp", [os.path.join(ROOT, "rs3-mcp.py")])))


SEEN = os.path.join(ROOT, "data", "rs3-written-numbers-seen.json")
LEDGER = os.path.join(ROOT, "rs3api", "ledger.md")
SRC = os.path.abspath(__file__)


def numbers_by_file():
    """{file: [numbers in its strings]}, the same strings written_numbers() counts."""
    import ast
    import glob
    out = {}
    for path in (sorted(glob.glob(os.path.join(ROOT, "rs3mcp", "*.py")))
                 + sorted(glob.glob(os.path.join(ROOT, "rs3mcp", "test", "*.py")))
                 + [os.path.join(ROOT, "rs3-mcp.py")]):
        if os.path.basename(path) == "claims.py":
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        got = [n for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and len(node.value) >= 12 for n in NUMBERS.findall(node.value)]
        if got:
            out[os.path.relpath(path, ROOT).replace(os.sep, "/")] = sorted(got)
    return out


def repin(total, today=None):
    """Move WRITTEN_NUMBERS to total and write the ledger entry naming what came and went.

    The pin went stale for days before anyone read it, with nothing left to say which strings
    moved it. Now the selftest moves it the moment it changes and the ledger says what changed,
    so the numbers stay counted and the entry is there to read in the diff."""
    import datetime
    global WRITTEN_NUMBERS
    now = numbers_by_file()
    try:
        with io.open(SEEN, encoding="utf-8") as fh:
            was = json.load(fh)
    except (OSError, ValueError):
        was = None
    if was is None:
        what = "no earlier record of the strings, so which ones is not known"
    else:
        from collections import Counter
        parts = []
        for f in sorted(set(was) | set(now)):
            a, b = Counter(was.get(f, [])), Counter(now.get(f, []))
            if a != b:
                parts.append("%s %s" % (f, ", ".join(["+" + n for n in sorted((b - a).elements())]
                                                     + ["-" + n for n in sorted((a - b).elements())])))
        what = "; ".join(parts) or "the same numbers, moved between strings"
    line = "- %d to %d: %s, recorded by the selftest: %s.\n" % (
        WRITTEN_NUMBERS, total, today or datetime.date.today().isoformat(), what)
    text = io.open(LEDGER, encoding="utf-8").read()
    head = "## WRITTEN_NUMBERS, rs3mcp/test/claims.py"
    start = text.index(head)
    end = text.find("\n## ", start + len(head))
    end = len(text) if end < 0 else end
    body = text[start:end].rstrip("\n") + "\n" + line
    io.open(LEDGER, "w", encoding="utf-8", newline="\n").write(text[:start] + body + text[end:])
    code = io.open(SRC, encoding="utf-8").read()
    io.open(SRC, "w", encoding="utf-8", newline="\n").write(
        re.sub(r"(?m)^WRITTEN_NUMBERS = \d+$", "WRITTEN_NUMBERS = %d" % total, code))
    WRITTEN_NUMBERS = total
    remember(now)


def remember(now=None):
    with io.open(SEEN, "w", encoding="utf-8") as fh:
        json.dump(now if now is not None else numbers_by_file(), fh, indent=0, sort_keys=True)


def _listing_size():
    """Characters in the tools/list payload, which is what a session pays before asking anything."""
    import json
    from rs3mcp.common import server
    # measured the way it is sent
    return len(json.dumps({"tools": server().listing()}, separators=(",", ":")))


def _strings(package="rs3mcp", extra=None):
    """Every string literal in a package, so a new number cannot appear unnoticed."""
    import ast
    import glob
    out = []
    for path in (sorted(glob.glob(os.path.join(ROOT, package, "*.py")))
                 + sorted(glob.glob(os.path.join(ROOT, package, "test", "*.py")))
                 + (extra or [])):
        # not this file: its numbers are the baseline
        if os.path.basename(path) == "claims.py":
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and len(node.value) >= 12:
                out.append(node.value)
    return out


def _cache():
    from rs3mcp.common import connect
    return connect()


def _index():
    if "idx" not in _MEM:
        _MEM["idx"] = sqlite3.connect(os.path.join(DATA, "rs3-index.db"))
    return _MEM["idx"]


def _count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def _files(root, suffix, recurse=False):
    """How many files of a kind live under here. Two claims quote the clientscript count and
    walking 20,625 files twice is most of a tenth of a second, so the answer is kept for the run."""
    key = ("files", root, suffix, recurse)
    if key in _MEM:
        return _MEM[key]
    if not os.path.isdir(root):
        n = 0
    elif not recurse:
        n = sum(1 for f in os.listdir(root) if f.endswith(suffix))
    else:
        n = sum(1 for dp, dn, fn in os.walk(root) for f in fn if f.endswith(suffix))
    _MEM[key] = n
    return n


# ---------------------------------------------------------------- what each written number means
#
# (file, the written text, what it should be)
#
# specific enough to match once, loose to rewrap

CLAIMS = [
    # 114 tables with rows hold nothing but ids
    ("rs3mcp/dbtable.py", r"handle on 114 of the ([\d,]+) tables with rows",
     lambda: len({r[0] for r in _cache().execute(
         "select distinct json_extract(data, '$.table') from config where type='dbrows'")})),

    ("rs3mcp/dbtable.py", r"The ([\d,]+) strings in these rows",
     lambda: sum(1 for _, d in _cache().execute(
         "select id,data from config where type='dbrows'")
         for col in ((__import__("json").loads(d).get("columns") or {}).get("columns") or [])
         for row in col["values"] for v in row if isinstance(v, str))),

    # derived from the data, not written down once
    ("rs3mcp/dbtable.py", r"([\d,]+) were filed under the wrong table",
     lambda: sum(1 for _, d in _cache().execute(
         "select id,data from config where type='dbrows'")
         if (__import__("json").loads(d).get("table") or 0) >= 128)),

    ("rs3mcp/dbtable.py", r"([\d,]+) tables holding",
     lambda: _count(_cache(), "select count(*) from config where type='dbtables'")),

    ("rs3mcp/dbtable.py", r"tables holding ([\d,]+) rows",
     lambda: _count(_cache(), "select count(*) from config where type='dbrows'")),


    ("rs3mcp/trace.py", r"param_use alone is ([\d,]+) typed edges",
     lambda: _count(_cache(), "select count(*) from param_use")),

    ("rs3mcp/script.py", r"reads all ([\d,]+) files",
     lambda: _files(CLIENTSCRIPT, ".ts", recurse=True)),

    # the folder also holds clientvars.d.ts, not a script
    ("rs3mcp/script.py", r"Search the ([\d,]+) decompiled clientscripts",
     lambda: _files(CLIENTSCRIPT, ".ts", recurse=True) - _files(CLIENTSCRIPT, ".d.ts", recurse=True)),

    ("rs3mcp/script.py", r"Scanning ([\d,]+) short tokens",
     lambda: _count(_index(), "select count(distinct token) from script_token")),

    ("rs3mcp/buildindex.py", r"sixteen full scans of ([\d,]+) rows",
     lambda: _count(_cache(), "select count(*) from config where type='items'")),

    # npc ids appearing as a multiloc child
    ("rs3mcp/cache.py", r"([\d,]+) npc ids are ",
     lambda: _index().execute(
         "select count(distinct child_id) from morph_edge where type='npcs'").fetchone()[0]),

    ("rs3mcp/cache.py", r"sprite_hash holds all (\d+) hashes",
     lambda: _count(_cache(), "select count(*) from sprite_hash")),

    # the name lists add rows only they know
    ("rs3mcp/cachediff.py", r"sqlite: ([\d,]+) rows against",
     lambda: _count(_cache(), "select count(*) from config"), "name lists"),

    # interface 0's components pack to interface ids
    ("rs3mcp/iface.py", r"components pack to 0 through (\d+)",
     lambda: _count(_cache(),
                    "select max(id) from config where type='interfaces' and id < 65536")),
]


def _norm(text):
    return " ".join(text.split())


def _written(path, pattern):
    """Every number the sentence states, or None if the sentence is no longer there."""
    try:
        text = _norm(io.open(os.path.join(ROOT, path), encoding="utf-8").read())
    except OSError:
        return None
    m = re.search(pattern, text)
    if not m:
        return None
    return tuple(int(g.replace(",", "")) for g in m.groups())


def rewrite(claims=None):
    """Write each claim's live number into its sentence, and return what moved.

    A rebuild moves these on purpose, and a dozen hand edits after every game update was the one
    step that kept a rebuild from running on its own. Only the exact claims: a rounded one is a
    choice of wording. Commas are kept as the sentence wrote them.
    """
    from rs3mcp.test import workspace
    moved = []
    for path, pattern, live, *need in claims or CLAIMS:
        if workspace.missing(*need):
            continue
        written = _written(path, pattern)
        try:
            actual = live()
        except Exception:
            continue
        if written is None or actual is None:
            continue
        actual = actual if isinstance(actual, tuple) else (actual,)
        if actual == written or len(actual) != len(written):
            continue
        full = os.path.join(ROOT, path)
        text = io.open(full, encoding="utf-8", newline="").read()
        # the sentence can wrap, so whitespace collapses
        m = re.search(pattern.replace(" ", r"\s+"), text)
        if not m:
            continue
        for i in range(len(actual), 0, -1):
            old = m.group(i)
            new = "{:,}".format(actual[i - 1]) if "," in old else str(actual[i - 1])
            text = text[:m.start(i)] + new + text[m.end(i):]
        io.open(full, "w", encoding="utf-8", newline="").write(text)
        moved.append((path, written, actual))
    return moved


def run(check, skip=None, repin_numbers=False):
    """Called by the correctness suite with its own check(). Reports a claim it cannot locate as
    loudly as one that disagrees. skip(what, reasons) hears about the claims this machine has no
    source for."""
    run_claims(check, CLAIMS, skip=skip)

    # unchecked numbers, repinned on change
    total = written_numbers()
    if repin_numbers and total != WRITTEN_NUMBERS:
        repin(total)
    elif repin_numbers and not os.path.exists(SEEN):
        remember()
    check("numbers in strings, so the unchecked ones stay counted", total, WRITTEN_NUMBERS)

    # pinned, so growth in the listing is decided
    size = _listing_size()
    check("the tool listing stays within its budget", size <= LISTING_BUDGET, True)
    if size > LISTING_BUDGET:
        check("  listing characters", size, LISTING_BUDGET)

    # they read the registry, so gaps get names
    from rs3mcp.test import golden
    check("every tool has a golden baseline or a written reason", golden.uncovered(), [])

    # and an add-on's claims, on the same tally
    from rs3mcp import addons
    for extra in addons.every("claims"):
        extra(check)


def run_claims(check, claims, rounded=(), skip=None):
    """Each written number against its live value. Shared, so an add-on checks its own the same way.

    A claim can name what it reads beyond the database, from test/workspace.py, as a fourth item.
    Where that is missing the claim is not checked, and skip() is told why."""
    from rs3mcp.test import workspace
    for path, pattern, live, *need in claims:
        label = "%s: %s" % (os.path.basename(path), pattern[:44])
        gone = workspace.missing(*need)
        if gone:
            if skip:
                skip("%s claim \"%s\"" % (os.path.basename(path), re.sub(r"\([^)]*\)", "N", pattern)), gone)
            continue
        written = _written(path, pattern)
        if written is None:
            check("claim not found, so nothing checks it -- %s" % label, "missing", "found")
            continue
        try:
            actual = live()
        except Exception as e:
            check("claim unverifiable -- %s" % label, "%s: %s" % (type(e).__name__, str(e)[:60]),
                  "a live value")
            continue
        if actual is None:
            check("claim has no live source -- %s" % label, None, "a live value")
            continue
        if not isinstance(actual, tuple):
            actual = (actual,)
        check("claim matches the data -- %s" % label, written, actual)

    for path, pattern, live in rounded:
        label = "%s: %s" % (os.path.basename(path), pattern[:40])
        try:
            text = _norm(io.open(os.path.join(ROOT, path), encoding="utf-8").read())
        except OSError:
            check("claim file missing -- %s" % label, path, "readable")
            continue
        m = re.search(pattern, text)
        if not m:
            check("claim not found, so nothing checks it -- %s" % label, "missing", "found")
            continue
        try:
            actual = live()
        except Exception as e:
            check("claim unverifiable -- %s" % label, str(e)[:60], "a live value")
            continue
        check("rounded claim still rounds there -- %s" % label,
              "%.1fM" % (actual / 1e6), m.group(1) + "M")


