"""
Interface trees dumped off a running client, for the ones the cache cannot describe.

They ship as a skeleton of empty containers and build their children while the game runs, so
decoding the cache more carefully will never help: the components do not exist until something
draws them. BUFF_BAR is the largest, and reading it out of the cache says the buff bar draws
nothing.

A dump is a snapshot, not a definition. An interface dumped in the wrong state is thin rather
than wrong. Everything here reports when a dump was taken and how much it caught, which is the
difference between "this interface has four components" and "four components were up when I
looked".

Written by the harvest server out of what quest-dev's `qd ifacedump` posts to it.
RS3_IFACE_LOG names extra logs to read.
"""

import io
import json
import os
import time

from .common import ROOT

LOG = os.path.join(ROOT, "rec-iface.jsonl")

_CACHE = {}


def logs():
    """Every log to read: the harvest sink, then anything RS3_IFACE_LOG names.

    A harvest server pointed somewhere else writes its own rec-iface.jsonl, so dumps taken against
    a second checkout are only reachable by naming its file. Several paths separate as PATH does."""
    found = [LOG]
    for part in (os.environ.get("RS3_IFACE_LOG") or "").split(os.pathsep):
        part = part.strip().strip('"')
        if part:
            part = os.path.abspath(part)
            if part not in found:
                found.append(part)
    return [p for p in found if os.path.exists(p)]


def _load():
    """{interface id: the most recent dump of it}, keyed on every log's mtime.

    Latest wins, across files as well as within one. A second dump of the same interface is a
    better look at it -- taken with more of it on screen, or in the state that mattered -- not a
    contradiction to reconcile."""
    paths = logs()
    if not paths:
        return {}
    stamp = tuple((p, os.path.getmtime(p)) for p in paths)
    if _CACHE.get("stamp") == stamp:
        return _CACHE["dumps"]

    dumps = {}
    for path in paths:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                iid = d.get("iface")
                rows, cols = d.get("rows"), d.get("columns")
                if not isinstance(iid, int) or not isinstance(rows, list) or not isinstance(cols, list):
                    continue
                if iid not in dumps or d.get("t", 0) >= dumps[iid].get("t", 0):
                    d = dict(d)
                    d["from"] = path
                    dumps[iid] = d
    _CACHE["stamp"], _CACHE["dumps"] = stamp, dumps
    return dumps


def _ago(t):
    if not t:
        return "unknown"
    secs = max(0, int(time.time() - t))
    if secs < 90:
        return "%ds ago" % secs
    if secs < 5400:
        return "%dm ago" % (secs // 60)
    if secs < 172800:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def _named(d):
    """The dump's rows as dicts, since a row arrives as a bare array to keep the payload small."""
    cols = d["columns"]
    return [dict(zip(cols, r)) for r in d["rows"] if isinstance(r, list) and len(r) == len(cols)]


def have(iface):
    return iface in _load()


def summary(iface, limit=80):
    """What a running client showed for this interface, or None if it was never dumped."""
    d = _load().get(iface)
    if d is None:
        return None
    rows = _named(d)
    drawn = [r for r in rows if str(r.get("text") or "").strip()]
    out = {"dumpedAt": _ago(d.get("t")), "components": len(rows),
           "drawingText": len(drawn),
           "visible": sum(1 for r in rows if r.get("visible")),
           "children": rows[:limit],
           "note": "read off a running client, so this is one moment rather than a definition. "
                   "A component that only appears in some state is missing unless that state "
                   "held when the dump was taken."}
    # not from the sink
    if d.get("from") and d["from"] != LOG:
        out["source"] = d["from"]
    # a dump adding nothing was taken while empty
    if not drawn and not any(r.get("dynamic", -1) >= 0 for r in rows):
        out["note"] += (" Nothing here draws text and no component is a runtime child, so this was "
                        "dumped with the interface empty. Dump it again with the thing you care "
                        "about actually on screen.")
    if len(rows) > limit:
        out["truncated"] = "%d of %d shown, raise limit" % (limit, len(rows))
    return out


def find_text(needle, limit=80):
    """Components drawing this text, across every dump.

    The cache answers the same question for text baked into an interface. This answers it for text
    the client wrote at runtime, which is the only kind those have."""
    needle = needle.strip().lower()
    hits = []
    for iid, d in sorted(_load().items()):
        for r in _named(d):
            drawn = str(r.get("text") or "")
            if needle and needle in drawn.lower():
                hits.append({"at": "%d:%d" % (iid, r.get("static", 0)),
                             "interface": iid, "text": drawn,
                             "category": r.get("category"), "dynamic": r.get("dynamic"),
                             "visible": r.get("visible"), "dumpedAt": _ago(d.get("t"))})
                if len(hits) >= limit:
                    return hits
    return hits


def dumped():
    """Every interface that has been dumped, newest first."""
    return [{"interface": iid, "components": len(d.get("rows") or []),
             "dumpedAt": _ago(d.get("t"))}
            for iid, d in sorted(_load().items(),
                                 key=lambda kv: kv[1].get("t", 0), reverse=True)]
