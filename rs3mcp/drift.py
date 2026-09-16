"""
Everything this server writes about the data, checked against the data.

Tools:
  rs3_drift  every claim in the code against the databases it describes
"""

import datetime
import os

from .common import ROOT


def tool_drift(args):
    """Everything this server asserts, checked against what it asserts it about.

    Three kinds of copy go stale: a number in a tool description, a sentence reworded until its
    check stops finding it, and a list kept beside the registry.
    """
    from rs3mcp.test import claims

    rows, unchecked = [], []

    def check(label, got, want):
        rows.append({"check": label, "value": got, "expected": want, "agrees": got == want})

    def skip(label, reasons):
        unchecked.append({"check": label, "needs": reasons})

    claims.run(check, skip)
    off = [r for r in rows if not r["agrees"]]
    return {"checked": len(rows), "disagreeing": len(off),
            "findings": off,
            "notCheckedHere": unchecked,
            "verdict": "everything written matches the data" if not off else
                       "%d claim(s) no longer match" % len(off),
            "sourceFreshness": _freshness(),
            "note": "this compares what the server says against the databases it has. It does not "
                    "say those databases match the live game: a clean drift report on a database "
                    "that is months behind is still clean, because the sentences are accurate about "
                    "the data they describe. A claim reported as not found was reworded, so nothing "
                    "was checking it any more, which counts as drift too. Completed measurements "
                    "and protocol constants are deliberately not checked; see rs3mcp/test/claims.py "
                    "for which and why."}


def _freshness():
    """When each source was last built, so a clean result is not read as a current one."""
    def when(path):
        try:
            return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        except OSError:
            return None

    out = {"cacheDump": when(os.path.join(ROOT, "rs3-cache.db")),
           "sidecarIndex": when(os.path.join(ROOT, "rs3-index.db"))}
    try:
        from .cachefile import GAME as game
        newest = max(os.path.getmtime(os.path.join(game, f)) for f in os.listdir(game)
                     if f.startswith("js5-"))
        out["gameCacheOnDisk"] = datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d")
        out["rebuildWouldChangeContent"] = newest > os.path.getmtime(os.path.join(ROOT, "rs3-cache.db"))
    except (OSError, ValueError):
        pass
    return out


TOOLS = [
    {"name": "rs3_drift", "fn": tool_drift, "examples": [{}],
     "description": "Check every number this server states about the data against the data, plus "
                    "the count of unchecked numbers. Run it after a rebuild, or when a "
                    "description's figure looks surprising.",
     "inputSchema": {"type": "object", "properties": {}}},
]
