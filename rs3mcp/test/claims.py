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

# A number worth a second look: thousands-separated, a scaled figure like 6.8M, or a bare run of
# three or more digits.
NUMBERS = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?M\b|\d{3,}")
# Total characters of tool listing. Bumping it is a decision, not a formality.
#
# 2026-09-12, net down from 26900 despite four additions, because it is now measured the way it is
# sent. The old number counted ", " and ": " on every key, which never went on the wire.
#
# What the room bought: tool annotations at 3,216 characters (readOnlyHint on 45 of 49, and the
# four that write, run a process or reach the network named); next-tool pointers; argument
# provenance; and paging parameters. The last is the trade worth seeing -- 185 characters of
# listing, paid once, took the longest answer from 16,341 tokens to 1,715.
#
# 29200 to 30000: one new tool, about 820 characters of listing, roughly 250 tokens a session. That
# is what a capability costs and the number is here so it stays a decision.
#
# Measured with every add-on loaded, so a copy without them sits well under it.
#
# 30000 to 30100: descriptions for the three parameters that had none, lookup's kind and sprite's
# name and id. An argument with no description is one an app has to guess at.
LISTING_BUDGET = 30100
# Numbers in strings that nothing above checks. Each was read once and judged static: a protocol
# constant, a worked example, or a finished measurement. It moves when a new one appears, and the
# point of the pin is that somebody looks at that one before bumping it. git log has which.
WRITTEN_NUMBERS = 122


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


def _listing_size():
    """Characters in the tools/list payload, which is what a session pays before asking anything."""
    import json
    from rs3mcp.common import server
    # Measured the way it is sent. Default separators over-report by about 1,300 characters.
    return len(json.dumps({"tools": server().listing()}, separators=(",", ":")))


def _strings(package="rs3mcp", extra=None):
    """Every string literal in a package, so a new number cannot appear unnoticed."""
    import ast
    import glob
    out = []
    for path in (sorted(glob.glob(os.path.join(ROOT, package, "*.py")))
                 + sorted(glob.glob(os.path.join(ROOT, package, "test", "*.py")))
                 + (extra or [])):
        # not this file: its numbers are the baseline and the patterns that check the others,
        # so counting them here makes the count depend on itself
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
# (file, the text as written with the number captured, what it should be)
#
# The pattern has to be specific enough that it cannot match a different sentence, and loose enough
# to survive rewrapping -- so whitespace is normalised out of both sides before matching.

CLAIMS = [
    # 114 of the tables with rows hold nothing but ids. That is why most unnamed tables cannot be
    # identified from their contents, so the number is worth keeping honest.
    ("rs3mcp/dbtable.py", r"handle on 114 of the ([\d,]+) tables with rows",
     lambda: len({r[0] for r in _cache().execute(
         "select distinct json_extract(data, '$.table') from config where type='dbrows'")})),

    ("rs3mcp/dbtable.py", r"The ([\d,]+) strings in these rows",
     lambda: sum(1 for _, d in _cache().execute(
         "select id,data from config where type='dbrows'")
         for col in ((__import__("json").loads(d).get("columns") or {}).get("columns") or [])
         for row in col["values"] for v in row if isinstance(v, str))),

    # The count of rows rsmv filed under the wrong table is not history: it is exactly the rows
    # whose table id needs a second byte, which is where the two byte orders diverge. So it stays
    # derivable from the data rather than being a number somebody wrote down once.
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

    # the folder also holds clientvars.d.ts, which is searched but is not a script
    ("rs3mcp/script.py", r"Search the ([\d,]+) decompiled clientscripts",
     lambda: _files(CLIENTSCRIPT, ".ts", recurse=True) - _files(CLIENTSCRIPT, ".d.ts", recurse=True)),

    ("rs3mcp/script.py", r"Scanning ([\d,]+) short tokens",
     lambda: _count(_index(), "select count(distinct token) from script_token")),

    ("rs3mcp/buildindex.py", r"sixteen full scans of ([\d,]+) rows",
     lambda: _count(_cache(), "select count(*) from config where type='items'")),

    # npc ids that appear as a child of a multiloc. This one had gone stale: 2,305 when it was
    # written, 4,511 after the rebuild.
    ("rs3mcp/cache.py", r"([\d,]+) npc ids are ",
     lambda: _index().execute(
         "select count(distinct child_id) from morph_edge where type='npcs'").fetchone()[0]),

    ("rs3mcp/cache.py", r"sprite_hash holds all (\d+) hashes",
     lambda: _count(_cache(), "select count(*) from sprite_hash")),

    # the name lists add the rows only they know of, Quest and Inv among them
    ("rs3mcp/cachediff.py", r"sqlite: ([\d,]+) rows against",
     lambda: _count(_cache(), "select count(*) from config"), "name lists"),

    # Interface 0's components pack to ids that are also interface ids, which is why a bare number
    # has to be read one way or the other. How far the collision reaches is a fact about the cache.
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


def run(check, skip=None):
    """Called by the correctness suite with its own check(). Reports a claim it cannot locate as
    loudly as one that disagrees. skip(what, reasons) hears about the claims this machine has no
    source for."""
    run_claims(check, CLAIMS, skip=skip)

    # And say how much is unchecked. If a file grows a number nobody has read, this moves.
    total = written_numbers()
    check("numbers in strings, so the unchecked ones stay counted", total, WRITTEN_NUMBERS)

    # The listing is sent once per session, before any question, and grows with every tool. Pinned
    # so that growth is a decision: raising it should mean somebody looked at what was added and
    # decided the description earned its room.
    size = _listing_size()
    check("the tool listing stays within its budget", size <= LISTING_BUDGET, True)
    if size > LISTING_BUDGET:
        check("  listing characters", size, LISTING_BUDGET)

    # Three harnesses kept their own list and all three went stale. They read the registry now.
    # Asserted here so a gap has a name rather than an unexplained NEW.
    from rs3mcp.test import golden
    check("every tool has a golden baseline or a written reason", golden.uncovered(), [])

    # and whatever an add-on claims about its own files, on the same tally
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


