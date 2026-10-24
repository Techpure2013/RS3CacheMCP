"""
Shared plumbing for the rs3 mcp tools: where things live on disk, the read only database, and the
two rules every tool result obeys -- never guess past what was asked for, and always say when an
answer has been cut short.
"""

import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))

# BASE holds the server, ROOT what it builds
BASE = os.path.dirname(HERE)
DEFAULT_ROOT = os.path.join(BASE, "data")
ROOT = os.environ.get("RS3MCP_DATA") or DEFAULT_ROOT
# a second build beside the default, the beta
OWN_ROOT = os.path.normcase(os.path.abspath(ROOT)) != os.path.normcase(os.path.abspath(DEFAULT_ROOT))

DB = os.path.join(ROOT, "rs3-cache.db")
SPRITES = os.path.join(ROOT, "rs3-cache-stage", "sprites")
CLIENTSCRIPT = os.path.join(ROOT, "rs3-cache-stage", "clientscript")


NAMES = os.path.join(HERE, "names")


def name_lists():
    """The folder of config name lists (`Vars - <type>.csv`).

    RS3MCP_NAMES first, then whatever an add-on offers, then the set that ships here. The cache
    carries no name for an interface, a var or a quest, so without a list those come out of a
    build as bare ids and nothing can look them up.
    """
    folder = os.environ.get("RS3MCP_NAMES")
    if not folder:
        from . import addons
        folder = addons.first("name_lists")
    if not folder or not os.path.isdir(folder):
        folder = NAMES
    return folder if os.path.isdir(folder) else None

MAX_ROWS = 500

def connect():
    if not os.path.exists(DB):
        raise RuntimeError("no database at " + DB)
    c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("pragma busy_timeout = 15000")
    return c


def check_readonly(sql):
    s = sql.strip().lstrip("(").lstrip()
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("only SELECT (or WITH ... SELECT) is allowed")
    if ";" in s.rstrip().rstrip(";"):
        raise ValueError("one statement at a time")
    return sql


def page(items, start=0, limit=50):
    """One window of a list, and what it takes to ask for the next.

    Returns (window, meta). A bare limit only lets a caller ask for more from the beginning, which
    resends what they already have; fetch pairs max_length with start_index and this is the same
    idea. Merge meta into the answer beside the window.
    """
    try:
        start = max(0, int(start or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        limit = max(1, int(limit or 50))
    except (TypeError, ValueError):
        limit = 50

    total = len(items)
    window = items[start:start + limit]
    meta = {"total": total, "start": start, "shown": len(window)}
    if start + len(window) < total:
        meta["nextStart"] = start + len(window)
        meta["more"] = ("%d of %d shown. Ask again with start=%d for the next %d"
                        % (start + len(window), total, start + len(window), limit))
    return window, meta


def rows_to_json(cur, limit):
    cols = [d[0] for d in cur.description] if cur.description else []
    out = []
    for r in cur:
        out.append({c: r[c] for c in cols})
        if len(out) >= limit:
            break
    # say so when the answer is cut short
    more = cur.fetchone() is not None
    return {"columns": cols, "rows": out, "truncated": more,
            "note": ("more rows exist, raise limit or narrow the query" if more else None)}


# ------------------------------------------------------------------- caching a scan of the tree
#
# the first question pays for the whole read
#
# kept under a fingerprint of its inputs

import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402

CACHE_DIR = ROOT


def tree_fingerprint(roots, suffix=""):
    """Name, size and mtime of every matching file under these roots. A root can be one file."""
    parts = []
    for root in roots:
        if os.path.isfile(root):
            st = os.stat(root)
            parts.append("%s:%d:%d" % (root, st.st_size, st.st_mtime_ns))
            continue
        if not os.path.isdir(root):
            parts.append(root + ":missing")
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".") and d != "node_modules"]
            for f in sorted(fn):
                if f.endswith(suffix):
                    try:
                        st = os.stat(os.path.join(dp, f))
                        parts.append("%s:%d:%d" % (f, st.st_size, st.st_mtime_ns))
                    except OSError:
                        pass
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def cached_scan(name, stamp, build):
    """build() once per fingerprint, then read it back from disk. Whatever build returns has to
    survive a json round trip, so sets and tuples have to be resolved before they get here."""
    path = os.path.join(CACHE_DIR, "rs3-%s.json" % name)
    try:
        with io.open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("fingerprint") == stamp:
            return saved["value"]
    except Exception:
        pass
    value = build()
    try:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"fingerprint": stamp, "value": value}))
    except Exception:
        pass
    return value


# --------------------------------------------------------------------------- the tool registry
#
# the harnesses read the server's own registry

_REGISTRY = {}
_SERVER = {}


def _run_server():
    """The server module's globals, loaded once."""
    if not _SERVER:
        import runpy
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _SERVER.update(runpy.run_path(os.path.join(root, "rs3-mcp.py")))
    return _SERVER


def registry():
    """{tool name: tool} exactly as the server registers it."""
    if not _REGISTRY:
        _REGISTRY.update(_run_server()["BY_NAME"])
    return _REGISTRY


class server(object):
    """The server's own listing. A harness measuring its own shape is a harness that drifts."""

    @staticmethod
    def listing():
        return _run_server()["listing"]()


_SIDECAR = {}


def sidecar(probe):
    """The rs3-index.db accelerator, opened once, or None.

    Refused when the stamp it was built from is not the cache now loaded. Elsewhere a stale index
    costs speed and never correctness, but parents_of hands its rows straight to the caller, so an
    older index answers with older parents.
    """
    key = probe
    if key in _SIDECAR:
        return _SIDECAR[key]
    path = os.path.join(ROOT, "rs3-index.db")
    conn = None
    if os.path.exists(path):
        try:
            conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
            conn.execute("select 1 from %s limit 1" % probe)
            built = conn.execute("select v from meta where k='cache_mtime'").fetchone()
            if built is None or abs(int(built[0]) - int(os.path.getmtime(DB))) > 2:
                conn = None
        except Exception:
            conn = None
    _SIDECAR[key] = conn
    return conn
