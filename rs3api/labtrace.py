"""What happened in a traced lab run, asked a question at a time.

A traced run is saved to data/lab/runs/<id>.db as segments (the load, the probe between events,
each event, the end), what changed in the watched tables during each, and which lines ran. The
questions:

    events              the events in order, with how much each changed
    state at= path=     a value, or a table's entries, as they stood after an event
    diff frm= to=       what changed between two events, old and new
    why path= at=       the event that last changed a value, and the exact line that wrote it
    coverage file=      the lines of a file that never ran
    compare other=      the first event where two runs part ways
    break when= show=   the first event after which a condition holds, with values there

why and break replay the run with the same work copy, seed and script, so they cost a second run
and nothing else. Every answer is capped, because an agent asking questions should not be paying
for the whole recording each time.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time

from rs3mcp.common import ROOT

RUNS = os.path.join(ROOT, "lab", "runs")
PAGE = 40
CAP = 60


def _path(run):
    return os.path.join(RUNS, "%s.db" % re.sub(r"[^\w-]", "", str(run)))


def store(trace, root, work, entry, script, seed, seconds, mb):
    """Save one run's trace and return its id."""
    os.makedirs(RUNS, exist_ok=True)
    rid = time.strftime("%Y%m%d-%H%M%S-") + hashlib.sha1(os.urandom(8)).hexdigest()[:6]
    db = sqlite3.connect(_path(rid))
    with db:
        db.executescript("""
            create table meta (k text primary key, v text);
            create table segments (idx integer primary key, kind text, seq integer, name text, payload text);
            create table changes (idx integer, path text, old text, new text);
            create table lines (idx integer, file text, line integer, hits integer);
            create index changes_path on changes (path);
            create index lines_file on lines (file);
        """)
        for k, v in (("root", root), ("work", work), ("entry", entry), ("script", script),
                     ("seed", seed), ("seconds", seconds), ("mb", mb)):
            db.execute("insert into meta values (?, ?)", (k, json.dumps(v)))
        for i, seg in enumerate(trace.get("segments") or []):
            db.execute("insert into segments values (?, ?, ?, ?, ?)",
                       (i, seg.get("kind"), seg.get("seq"), seg.get("name"),
                        json.dumps(seg.get("payload")) if "payload" in seg else None))
            for c in seg.get("changes") or []:
                db.execute("insert into changes values (?, ?, ?, ?)",
                           (i, c["path"], json.dumps(c["old"]) if "old" in c else None,
                            json.dumps(c["new"]) if "new" in c else None))
            lines = seg.get("lines") or {}
            for key, hits in (lines.items() if isinstance(lines, dict) else []):
                f, _, n = key.rpartition(":")
                db.execute("insert into lines values (?, ?, ?, ?)", (i, f, int(n), hits))
    db.close()
    return rid


def _open(run):
    p = _path(run)
    if not os.path.exists(p):
        raise ValueError("no traced run %s; run rs3_lab_run with trace=true first" % run)
    return sqlite3.connect(p)


def _meta(db):
    return {k: json.loads(v) for k, v in db.execute("select k, v from meta")}


def _seg_of(db, seq):
    """The segment index where event seq ended; the last segment when seq is None."""
    if seq is None:
        return db.execute("select max(idx) from segments").fetchone()[0]
    row = db.execute("select idx from segments where kind = 'event' and seq = ?", (int(seq),)).fetchone()
    if not row:
        raise ValueError("there is no event %s in this run" % seq)
    return row[0]


def _load(v):
    return None if v is None else json.loads(v)


def _state_map(db, upto):
    state = {}
    for path, new in db.execute("select path, new from changes where idx <= ? order by idx", (upto,)):
        if new is None:
            state.pop(path, None)
        else:
            state[path] = json.loads(new)
    return state


def _under(path, key):
    return key == path or key.startswith(path + ".") or key.startswith(path + "[")


def q_events(db, start=0):
    rows = db.execute("select s.seq, s.name, s.payload, count(c.path) from segments s "
                      "left join changes c on c.idx = s.idx where s.kind = 'event' "
                      "group by s.idx order by s.idx").fetchall()
    start = int(start or 0)
    window = rows[start:start + PAGE]
    out = {"events": [{"seq": s, "name": n, "payload": _load(p), "changed": c} for s, n, p, c in window],
           "total": len(rows)}
    if start + PAGE < len(rows):
        out["nextStart"] = start + PAGE
    return out


def q_state(db, at=None, path=None):
    if not path:
        raise ValueError("give path, eg core/counter.state.kills")
    state = _state_map(db, _seg_of(db, at))
    if path in state:
        return {"at": at, "path": path, "value": state[path]}
    entries = {k[len(path):].lstrip("."): v for k, v in sorted(state.items()) if _under(path, k)}
    out = {"at": at, "path": path, "value": dict(list(entries.items())[:CAP]) or None}
    if len(entries) > CAP:
        out["more"] = "%d of %d entries shown; ask for a deeper path" % (CAP, len(entries))
    return out


def q_diff(db, frm=None, to=None, path=None):
    lo = _seg_of(db, frm) if frm is not None else -1
    hi = _seg_of(db, to)
    first, lastnew = {}, {}
    for p, old, new in db.execute("select path, old, new from changes where idx > ? and idx <= ? "
                                  "order by idx", (lo, hi)):
        if path and not _under(path, p):
            continue
        first.setdefault(p, _load(old))
        lastnew[p] = _load(new)
    changes = [{"path": p, "old": first[p], "new": lastnew[p]} for p in sorted(first) if first[p] != lastnew[p]]
    out = {"from": frm, "to": to, "changes": changes[:CAP]}
    if len(changes) > CAP:
        out["more"] = "%d of %d changes shown; narrow with path" % (CAP, len(changes))
    return out


def q_why(db, run, path=None, at=None):
    if not path:
        raise ValueError("give path, the value to explain")
    upto = _seg_of(db, at)
    row = db.execute("select c.idx, c.path, c.old, c.new, s.kind, s.seq, s.name from changes c "
                     "join segments s on s.idx = c.idx where c.idx <= ? and (c.path = ? or c.path like ? "
                     "or c.path like ?) order by c.idx desc limit 1",
                     (upto, path, path + ".%", path + "[%")).fetchone()
    if not row:
        return {"path": path, "note": "it never changed in this run up to that point"}
    idx, changed, old, new, kind, seq, name = row
    out = {"path": path, "changed": changed, "old": _load(old), "new": _load(new)}
    if kind != "event":
        out["during"] = kind
        out["note"] = "it changed outside any event, in the %s segment before event %s" % (kind, seq)
        return out
    out["event"] = {"seq": seq, "name": name}
    from . import lab
    m = _meta(db)
    replay = lab.run(m["root"], entry=m["entry"], script=m["script"], seed=m["seed"],
                     seconds=m["seconds"], mb=m["mb"], why=(changed, seq))
    found = replay.get("why") or {}
    out["line"] = found.get("line")
    out["locals"] = found.get("locals")
    if not found:
        out["note"] = "the replay did not see the write; the value may be set by code outside the plugin"
    return out


def _executable(file_path):
    from . import lab
    exe = lab.lua54()
    luac = os.path.join(os.path.dirname(exe), "luac54.exe") if exe else None
    if not luac or not os.path.exists(luac):
        return None
    p = subprocess.run([luac, "-p", "-l", "-l", file_path], capture_output=True, text=True)
    # VARARGPREP opens each chunk and never raises lines
    return {int(n) for n, op in re.findall(r"^\s+\d+\s+\[(\d+)\]\s+(\w+)", p.stdout, re.M)
            if op != "VARARGPREP"}


def q_coverage(db, file=None):
    if not file:
        raise ValueError("give file, eg core/counter.lua")
    m = _meta(db)
    executable = _executable(os.path.join(m["work"], file))
    if executable is None:
        raise ValueError("coverage needs luac54 beside lua54")
    ran = {n for (n,) in db.execute("select distinct line from lines where file = ?", (file,))}
    never = sorted(executable - ran)
    out = {"file": file, "executable": len(executable), "ran": len(executable & ran), "neverRan": never[:200]}
    if len(never) > 200:
        out["more"] = "%d lines never ran; first 200 shown" % len(never)
    return out


def _event_sig(db, seq):
    row = db.execute("select idx, name, payload from segments where kind = 'event' and seq = ?",
                     (seq,)).fetchone()
    if not row:
        return None
    changes = db.execute("select path, new from changes where idx = ? or idx = ? - 1 order by path",
                         (row[0], row[0])).fetchall()
    return (row[1], row[2], tuple(changes))


def q_compare(db, other=None):
    if not other:
        raise ValueError("give other, the run id to compare with")
    odb = _open(other)
    try:
        na = db.execute("select count(*) from segments where kind = 'event'").fetchone()[0]
        nb = odb.execute("select count(*) from segments where kind = 'event'").fetchone()[0]
        for seq in range(1, max(na, nb) + 1):
            a, b = _event_sig(db, seq), _event_sig(odb, seq)
            if a is None or b is None:
                return {"firstDifference": {"seq": seq, "why": "only one run has this event",
                                            "this": a and a[0], "other": b and b[0]}}
            if a[:2] != b[:2]:
                return {"firstDifference": {"seq": seq, "why": "a different event",
                                            "this": a[0], "other": b[0]}}
            if a[2] != b[2]:
                sa, sb = dict(a[2]), dict(b[2])
                paths = sorted(p for p in set(sa) | set(sb) if sa.get(p) != sb.get(p))
                return {"firstDifference": {"seq": seq, "why": "the same event changed different things",
                                            "paths": paths[:20]}}
        return {"firstDifference": None, "note": "the two runs match event for event"}
    finally:
        odb.close()


def q_break(db, when=None, show=None):
    if not when:
        raise ValueError("give when, a Lua condition; lab.get(path) reads the watched state")
    from . import lab
    m = _meta(db)
    replay = lab.run(m["root"], entry=m["entry"], script=m["script"], seed=m["seed"],
                     seconds=m["seconds"], mb=m["mb"], brk=(when, list(show or [])))
    hit = replay.get("hit")
    if not hit:
        return {"when": when, "seq": None, "note": "the condition never held after any event"}
    return {"when": when, "seq": hit.get("seq"), "values": hit.get("values") or {}}


def query(run, op, **kw):
    """One question about a traced run."""
    db = _open(run)
    try:
        if op == "events":
            return q_events(db, kw.get("start"))
        if op == "state":
            return q_state(db, kw.get("at"), kw.get("path"))
        if op == "diff":
            return q_diff(db, kw.get("frm"), kw.get("to"), kw.get("path"))
        if op == "why":
            return q_why(db, run, kw.get("path"), kw.get("at"))
        if op == "coverage":
            return q_coverage(db, kw.get("file"))
        if op == "compare":
            return q_compare(db, kw.get("other"))
        if op == "break":
            return q_break(db, kw.get("when"), kw.get("show"))
        raise ValueError("op is one of events, state, diff, why, coverage, compare, break")
    finally:
        db.close()


def tool_lab_query(args):
    run = str(args.get("run") or "").strip()
    if not run:
        raise ValueError("give run, the id rs3_lab_run returned with trace=true")
    show = args.get("show")
    if isinstance(show, str):
        show = [s.strip() for s in show.split(",") if s.strip()]
    return query(run, str(args.get("op") or "events"), start=args.get("start"), at=args.get("at"),
                 path=args.get("path"), frm=args.get("from"), to=args.get("to"), file=args.get("file"),
                 other=args.get("other"), when=args.get("when"), show=show)


TOOLS = [
    {"name": "rs3_lab_query", "fn": tool_lab_query,
     "description": "Ask a traced lab run: events, state, diff, why (exact line by replay), coverage, "
                    "compare, break.",
     "inputSchema": {"type": "object", "required": ["run", "op"], "properties": {
         "run": {"type": "string", "description": "id from rs3_lab_run trace=true"},
         "op": {"type": "string", "description": "events|state|diff|why|coverage|compare|break"},
         "at": {"type": "integer", "description": "event number"},
         "path": {"type": "string", "description": "eg core/x.state.kills"},
         "from": {"type": "integer", "description": "event number"},
         "to": {"type": "integer", "description": "event number"},
         "file": {"type": "string", "description": "eg core/x.lua"},
         "other": {"type": "string", "description": "run id"},
         "when": {"type": "string", "description": "Lua condition, lab.get(path) reads state"},
         "show": {"type": "array", "items": {"type": "string"}, "description": "Lua expressions"},
         "start": {"type": "integer", "description": "paging"}}}},
]
