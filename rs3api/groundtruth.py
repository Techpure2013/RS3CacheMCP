"""What qd groundtruth measured in the live client, turned into facts a tool can be asked.

The run saves its rows in the client's plugin store (PluginData.db) under qdevGT.*. This reads them,
makes one fact per call, write, read or crash, and keeps the facts in data/client/facts.db with the
client build they were measured on. Each fact says what was done, with what, and what happened:

    returns     it answered; `detail` is the shape of the answer
    throws      a Lua error came back; `detail` is the message
    crashed     the client went down on it
    skipped     not tried, and why (an argument or class that had crashed twice)
    refused     a property write the client threw on
    accepted    a property write that went in; `detail` is what reading it back gave

A crash nobody has written a lesson for is added to the staged lessons (lessons/pending.jsonl) as
it comes in, so no crash is only in the store.
"""

import json
import os
import re
import sqlite3
import time

from rs3mcp.common import ROOT

PLUGIN_DATA = os.environ.get("RS3_PLUGINDATA") or r"C:\ProgramData\Jagex\RuneScape-BETA\PluginData.db"
FACTS = os.path.join(ROOT, "client", "facts.db")
LESSONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lessons", "pending.jsonl")
PREFIX = "qdevGT."
# facts a page, sized to fit one answer
CAP = 25

# what was called, how, and with what
CALL = re.compile(r"^(?P<api>.+?)(?P<how>[.:])(?P<args>\(.*\))$")
# "ui.Text#alpha = 1.5"
WRITE = re.compile(r"^(?P<api>.+?#[^ ]+) = (?P<args>.+)$")
# "ui.Grid#cellHeight: read"
READ = re.compile(r"^(?P<api>.+?#[^:]+): read$")


def _unmap(v):
    """The store writes a table as [{key, value}, ...]; give back plain dicts and lists."""
    if isinstance(v, list) and v and all(isinstance(e, dict) and set(e) == {"key", "value"} for e in v):
        keys = [e["key"] for e in v]
        vals = [_unmap(e["value"]) for e in v]
        if all(isinstance(k, (int, float)) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [x for _, x in sorted(zip(keys, vals))]
        return dict(zip((str(k) for k in keys), vals))
    if isinstance(v, list):
        return [_unmap(x) for x in v]
    return v


def read(db_path=None):
    """Every qdevGT row, by section, plus the crashed steps and the rows the store refused."""
    db = sqlite3.connect("file:%s?mode=ro" % (db_path or PLUGIN_DATA), uri=True)
    try:
        raw = {k: v for k, v in db.execute("select Key, Value from DataStore where Key like ?", (PREFIX + "%",))}
    finally:
        db.close()

    def value(key):
        v = raw.get(PREFIX + key)
        if v is None:
            return None
        text = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        text = text.rstrip("\x00")
        try:
            return _unmap(json.loads(text))
        except ValueError:
            return text

    sections = {}
    for section, n in (value("index") or {}).items():
        rows = []
        for i in range(1, int(n) + 1):
            chunk = value("%s.%d" % (section, i)) or []
            rows += list(chunk.values()) if isinstance(chunk, dict) else chunk
        sections[section] = rows
    crashed = value("crashed") or {}
    # the walk stamps its build and its step
    return {"sections": sections, "crashed": sorted(crashed) if isinstance(crashed, dict) else [],
            "refused": value("refused") or [], "build": value("build"), "step": value("step")}


def _fact(step, outcome, detail=None):
    for kind, pat in (("call", CALL), ("write", WRITE), ("read", READ)):
        m = pat.match(step)
        if m:
            d = m.groupdict()
            return {"api": d["api"], "kind": kind, "how": d.get("how") or "",
                    "args": d.get("args") or "", "outcome": outcome, "detail": detail, "step": step}
    return {"api": step, "kind": "step", "how": "", "args": "", "outcome": outcome, "detail": detail, "step": step}


# the wrapper's position says nothing about the client
WRAPPER_POSITION = re.compile(r"^.*?engine/gt/describe\.lua:\d+: ")


def _message(m):
    return WRAPPER_POSITION.sub("", m) if isinstance(m, str) else m


def _short(v):
    # whole, never cut: the lab's model parses these
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def facts(data):
    """One fact per thing the run did, crashes included."""
    # a step's last row is its newest
    newest = {}
    for section in ("calls", "own"):
        for r in data["sections"].get(section, []):
            step = r.get("id")
            if isinstance(step, str):
                newest[step] = r
    out, seen = [], set(newest)
    for step, r in newest.items():
        if r.get("crashed"):
            out.append(_fact(step, "crashed"))
        elif r.get("skipped"):
            out.append(_fact(step, "skipped", r["skipped"]))
        elif "writeThrows" in r:
            out.append(_fact(step, "refused" if r["writeThrows"] else "accepted",
                             _message(r.get("message")) if r["writeThrows"] else _short(r.get("readBack"))))
        elif r.get("throws"):
            out.append(_fact(step, "throws", _message(r.get("message"))))
        elif "throws" in r:
            out.append(_fact(step, "returns", _short(r.get("shape")) if r.get("shape") else "nothing"))
        elif "made" in r:
            out.append(_fact(step, "returns", "made" if r["made"] else "nil"))
    for step in data["crashed"]:
        if step not in seen:
            seen.add(step)
            out.append(_fact(step, "crashed"))
    # what a table does with an unknown key
    for r in data["sections"].get("keys", []):
        step = r.get("id")
        if isinstance(step, str) and step not in seen:
            seen.add(step)
            if r.get("throws"):
                out.append({"api": step, "kind": "key", "how": "", "args": "", "outcome": "throws",
                            "detail": _message(r.get("message")), "step": step})
            else:
                out.append({"api": step, "kind": "key", "how": "", "args": "", "outcome": "returns",
                            "detail": _short(r.get("shape")) if r.get("shape") else "nothing", "step": step})
    # what the client made of each object argument
    for r in data["sections"].get("args", []):
        step = r.get("id")
        if isinstance(step, str) and step not in seen and r.get("shape"):
            seen.add(step)
            out.append({"api": step, "kind": "arg", "how": "", "args": "", "outcome": "returns",
                        "detail": _short(r["shape"]), "step": step})
    # each event's payload, and gone entities' reads
    for r in data["sections"].get("events", []):
        name = r.get("event")
        if isinstance(name, str) and ("event:" + name) not in seen and r.get("shape"):
            seen.add("event:" + name)
            out.append({"api": name, "kind": "event", "how": "", "args": "", "outcome": "returns",
                        "detail": _short(r["shape"]), "step": "event " + name})
    for r in data["sections"].get("stale", []):
        path = r.get("from")
        if isinstance(path, str) and ("gone:" + path) not in seen and r.get("shape"):
            seen.add("gone:" + path)
            out.append({"api": path, "kind": "gone", "how": "", "args": "", "outcome": "returns",
                        "detail": _short(r["shape"]), "step": "gone " + path})
    # the shape of something read but never called
    for r in data["sections"].get("catalog", []):
        path, shape = r.get("path"), r.get("shape")
        if isinstance(path, str) and isinstance(shape, dict) and shape.get("fields")                 and ("shape:" + path) not in seen:
            seen.add("shape:" + path)
            out.append({"api": path, "kind": "shape", "how": "", "args": "", "outcome": "returns",
                        "detail": _short(shape), "step": "shape " + path})
    # what exists, so never measured differs from absent
    for r in data["sections"].get("catalog", []):
        path, t = r.get("path"), r.get("type")
        if isinstance(path, str) and t in ("function", "table", "userdata") and ("exists:" + path) not in seen:
            seen.add("exists:" + path)
            out.append({"api": path.replace(" (call)", ""), "kind": "exists", "how": "", "args": "",
                        "outcome": "returns", "detail": t, "step": "exists " + path})
    # a class the walk called into exists
    for r in data["sections"].get("own", []):
        step = r.get("id")
        path = step.split(".new(")[0] if isinstance(step, str) and ".new(" in step else None
        if path and ("exists:" + path) not in seen:
            seen.add("exists:" + path)
            out.append({"api": path, "kind": "exists", "how": "", "args": "", "outcome": "returns",
                        "detail": "table", "step": "exists " + path})
    # constants the walk passed on its way
    for r in data["sections"].get("catalog", []):
        path, t = r.get("path"), r.get("type")
        if isinstance(path, str) and t in ("number", "string", "boolean") and "value" in r and path not in seen:
            seen.add(path)
            out.append({"api": path, "kind": "value", "how": "", "args": "", "outcome": "returns",
                        "detail": json.dumps({"type": t, "value": r["value"]}), "step": path})
    return out


def save(fs, build=None, path=None):
    """Replace the facts measured on this build; other builds' facts stay."""
    path = path or FACTS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    with db:
        db.execute("""create table if not exists facts (build text, measured text, api text, kind text,
                      how text, args text, outcome text, detail text, step text)""")
        db.execute("create index if not exists facts_api on facts (api)")
        db.execute("delete from facts where build is ?", (build,))
        day = time.strftime("%Y-%m-%d")
        db.executemany("insert into facts values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(build, day, f["api"], f["kind"], f["how"], f["args"], f["outcome"], f["detail"],
                         f["step"]) for f in fs])
    db.close()
    return path


def _lessons(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def stage_crash_lessons(crashes, build=None, path=None):
    """Every crash no staged lesson mentions becomes one, so none lives only in the store."""
    path = path or LESSONS
    have = _lessons(path)
    text = "\n".join(json.dumps(r) for r in have)
    added = []
    for step in crashes:
        if step in text:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", step.lower()).strip("-")[:60]
        added.append({"id": "gt-crash-" + slug, "date": time.strftime("%Y-%m-%d"),
                      "summary": "The client went down at `%s` in qd groundtruth" % step,
                      "pattern": step, "patternKind": "text", "plugin": "*", "file": "*",
                      "expected": "an answer or a throw", "actual": "client crash",
                      "fix": "not known yet; the run skips it", "tags": ["client-api", "crash", "auto"],
                      # the suite fails until the fixture has it
                      "enforcedBy": "rs3api/factcheck.py",
                      "provenBy": "python rs3-mcp.py --test (factcheck: every measured crash is caught)",
                      "severity": "high", "source": "qdevGT.crashed, imported%s" % (", build " + build if build else ""),
                      "confirmed": True, "auto": True})
    if added:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for r in added:
                fh.write(json.dumps(r) + "\n")
    return added


def import_run(db_path=None, build=None, facts_path=None, lessons_path=None):
    data = read(db_path)
    fs = facts(data)
    # the run's own stamp, so facts file right
    build = build or data.get("build")
    save(fs, build, facts_path)
    # a walk step is no plugin lesson
    usable = {f["step"] for f in fs if f["kind"] in ("read", "write", "call")}
    added = stage_crash_lessons([s for s in data["crashed"] if s in usable], build, lessons_path)
    counts, kinds = {}, {}
    for f in fs:
        counts[f["outcome"]] = counts.get(f["outcome"], 0) + 1
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    # counted by kind as well as in total
    return {"facts": len(fs), "build": build, "kinds": kinds, "outcomes": counts,
            "crashes": len(data["crashed"]), "crashedAt": data.get("step"),
            "lessonsAdded": [r["id"] for r in added], "refused": data["refused"]}


def query(api=None, outcome=None, kind=None, build=None, start=0, path=None):
    """Facts about an api, by prefix: `config.Obj.FromID`, `ui.Slider`, `Keyboard`."""
    path = path or FACTS
    if not os.path.exists(path):
        return {"error": "no facts yet: run qd groundtruth in the client, then rs3_client_facts import=true"}
    db = sqlite3.connect(path)
    where, args = [], []
    for col, v in (("outcome", outcome), ("kind", kind), ("build", build)):
        if v:
            where.append("%s = ?" % col)
            args.append(v)
    if api:
        where.append("(api = ? or api like ? or api like ?)")
        args += [api, api + ".%", api + "#%"]
    sql = "from facts" + (" where " + " and ".join(where) if where else "")
    total = db.execute("select count(*) " + sql, args).fetchone()[0]
    rows = db.execute("select api, kind, how, args, outcome, detail, build, measured " + sql +
                      " order by api, kind, how, args limit ? offset ?", args + [CAP, start]).fetchall()
    tally = dict(db.execute("select outcome, count(*) " + sql + " group by outcome", args).fetchall())
    db.close()
    out = {"total": total, "outcomes": tally,
           "facts": [{"api": a, "kind": k, "call": (h + g) if k == "call" else g, "outcome": o,
                      "detail": (d or "")[:120] or None, "build": b, "measured": m}
                     for a, k, h, g, o, d, b, m in rows]}
    if total > start + CAP:
        out["more"] = start + CAP
    return out


def _top_level(text):
    """The functions and values a namespace declares itself, not those of classes nested in it."""
    names, depth = [], 0
    for line in text.splitlines():
        if depth == 1:
            m = re.match(r"\s*(?:export\s+)?(?:function|const|let|var)\s+([A-Za-z_]\w*)", line)
            if m:
                names.append(m.group(1))
        depth += line.count("{") - line.count("}")
    return names


def declared(decls=None):
    """What the typings say each namespace and class has, by qualified name.

    config.Obj and var.VarPlayer keep their members behind a C __index, so the run's pairs() found
    nothing under them and FromID, the most used call there is, was never tried. These names are
    handed to the run to try as well; one the client doesn't have reads as nil, which is a finding.
    """
    if decls is None:
        from rs3api import api
        decls = api._all()
    out = {}
    for d in decls:
        if d.get("kind") in ("namespace", "class") and d.get("members") and d.get("qualified"):
            names = out.setdefault(d["qualified"], set())
            members = d["members"]
            if d["kind"] == "namespace" and d.get("text"):
                members = _top_level(d["text"])
            # ALL_CAPS members are id constants, not api
            names.update(m for m in members if re.match(r"^[A-Za-z_]\w*$", m) and re.search(r"[a-z]", m))
    # id.<Type>.FromID turns a number back into its name
    for d in decls:
        m = re.match(r"^Id([A-Z]\w*)$", d.get("name") or "")
        if d.get("kind") == "interface" and m:
            out.setdefault("id." + m.group(1), set()).add("FromID")
    # a nested name comes off the namespace above
    for q in list(out):
        nested = set()
        for other, members in out.items():
            if other.startswith(q + "."):
                nested |= members
        out[q] -= nested
    return {k: sorted(v) for k, v in sorted(out.items()) if v}


def var_samples(source_dir=None, per_type=2):
    """A couple of real var names per var table, from the client's own name lists.

    var.VarPlayer.X sits behind a C __index like config's, so the walk has never read one and
    nothing knows what a var object holds. Named here, it reads two and the shape is measured.
    """
    import csv
    import glob
    from rs3api import paths
    out = {}
    for path in sorted(glob.glob(os.path.join(source_dir or paths.API_INFO, "Vars - Var*.csv"))):
        m = re.match(r"Vars - (Var\w*)", os.path.basename(path))
        if not m:
            continue
        names = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("CONFIG_NAME") or "").strip()
                # a base var, not a bit slice
                if name and re.match(r"^[A-Za-z_]\w*$", name) and not (row.get("ID") or "").startswith("-"):
                    names.append(name)
                if len(names) >= per_type:
                    break
        if names:
            out["var." + m.group(1)] = names
    return out


def write_declared(path=None, decls=None):
    """engine/gt/declared.lua for quest-dev. Made from the typings, so it is gitignored there."""
    from rs3api import paths
    path = path or os.path.join(paths.PLUGINS, "quest-dev", "engine", "gt", "declared.lua")
    names = declared(decls)
    for q, members in var_samples().items():
        names.setdefault(q, []).extend(m for m in members if m not in names.get(q, []))
    lines = ["-- made by rs3_client_facts declared=true from the NDA typings: not hand edited, not committed, never copied to Quest-Dev",
             "return {"]
    for q, members in names.items():
        lines.append('  ["%s"] = { %s },' % (q, ", ".join('"%s"' % m for m in members)))
    lines.append("}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"written": path, "namespacesAndClasses": len(names), "names": sum(len(v) for v in names.values())}


def tool_client_facts(args):
    if args.get("declared"):
        return write_declared()
    if args.get("import"):
        return import_run(args.get("db"), args.get("build"))
    return query(args.get("api"), args.get("outcome"), args.get("kind"), args.get("build"), args.get("start") or 0)


TOOLS = [
    {"name": "rs3_client_facts", "fn": tool_client_facts,
     "description": "What the live client did when called: returns, throws, crashed, skipped, refused, "
                    "accepted.",
     "inputSchema": {"type": "object", "properties": {
         "api": {"type": "string", "description": "prefix, eg config.Obj.FromID or ui.Slider"},
         "outcome": {"type": "string", "description": "returns|throws|crashed|skipped|refused|accepted"},
         "import": {"type": "boolean", "description": "read the latest run from the client"},
         "declared": {"type": "boolean", "description": "typings' names for the run"},
         "start": {"type": "integer", "description": "paging"}}}},
]
