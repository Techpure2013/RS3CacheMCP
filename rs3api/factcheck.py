"""Source against what the live client was measured doing.

Every finding quotes the row it came from: the api, the outcome, the client's own message, the build
and the day it was measured. Nothing here holds a list of bad names; the rules ask facts.db and
report its answer, so a crash measured tomorrow is a check tomorrow, and a claim nobody measured
cannot be made at all.

The facts come from qd groundtruth through rs3api.groundtruth. Only the newest build is read: an
older run's answer for a call the client has since changed is not evidence about this client.
"""

import io
import os
import re
import sqlite3

from . import groundtruth

# a local made by a constructor call
MADE = re.compile(r"\blocal\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*((?:ui|config)\.[A-Za-z0-9_]+)\.new\s*\(")
# what a line does to a name
FIELD = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])\s*(=[^=]|[^=(]|$)")
METHOD = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*):([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)\n]*)\)")
DOTTED = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)\n]*)\)")


def _code(text):
    """Strings and comments blanked, positions kept, so a name inside a string is not a use."""
    from .lualex import strip
    return strip(text)


def _newest(db):
    row = db.execute("select build from facts order by rowid desc limit 1").fetchone()
    return row[0] if row else None


def load(path=None, build=None):
    """Every fact the rules need, by what a line can do: read, write, call."""
    path = path or groundtruth.FACTS
    if not os.path.exists(path):
        return None
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        build = build or _newest(db)
        rows = db.execute("select api, kind, how, args, outcome, detail, measured from facts where build is ?",
                          (build,)).fetchall()
    finally:
        db.close()
    facts = {"build": build, "read": {}, "write": {}, "call": {}, "exists": set(), "throwers": set()}
    for api, kind, how, args, outcome, detail, measured in rows:
        row = {"api": api, "outcome": outcome, "detail": detail, "build": build, "measured": measured,
               "args": args, "kind": kind}
        if kind == "exists":
            facts["exists"].add(api)
            continue
        if kind not in ("read", "write", "call"):
            continue
        # the crash is never overwritten by a skip
        held = facts[kind].setdefault(api, {}).get(args or "")
        if held is None or (held["outcome"] != "crashed" and outcome == "crashed"):
            facts[kind][api][args or ""] = row
        if kind == "call" and outcome == "throws":
            facts["throwers"].add(api)
    return facts


# only a reason about this api counts
CONDEMNED = re.compile(r"this function crashed the client")


def condemned(api, facts):
    """The row saying this api is not safe to touch at all, whatever the argument."""
    for kind in ("call", "read", "write"):
        for row in (facts[kind].get(api) or {}).values():
            if row["outcome"] == "skipped" and row["detail"] and CONDEMNED.search(row["detail"]):
                return row
    return None


def _kind(literal):
    """The argument kind the walk measures, when the source shows a literal. None when it doesn't."""
    v = literal.strip()
    if v in ("", "..."):
        return "()" if v == "" else None
    if v == "nil":
        return "(nil)"
    if v in ("true", "false"):
        return "(true)"
    if v.startswith("{") :
        return "({})" if v.strip("{} \t") == "" else None
    if v[0] in "\"'[":
        return '("qdev")'
    try:
        n = float(v)
    except ValueError:
        return None
    if n != int(n):
        return "(0.5)"
    if n < 0:
        return "(-1)"
    if n == 0:
        return "(0)"
    if n >= 99999999:
        return "(99999999)"
    return None


def _why(row):
    """What the row says, in the words of the run that measured it."""
    if row["outcome"] == "skipped":
        return "the walk stopped calling this: " + row["detail"]
    return "the walk took the client down calling this with %s" % (row["args"] or "no argument")


def _finding(rel, line, rule, member, problem, row):
    """A finding is the rule plus the row that proves it. There is no other way to make one."""
    return {"file": rel, "line": line, "rule": rule, "member": member, "problem": problem,
            "evidence": {"outcome": row["outcome"], "client": row.get("detail"),
                         "measured": row["measured"], "build": row["build"],
                         "step": _step(row)}}


def _step(row):
    """The step as the run wrote it, so it can be looked up in facts.db by hand."""
    if row["kind"] == "write":
        return "%s = %s" % (row["api"], row["args"])
    if row["kind"] == "read":
        return row["api"] + ": read"
    return row["api"] + (row["args"] or "")


def _crashes(api, facts, where, kind_of_arg=None):
    """The row saying this line takes the client down, or None.

    Three ways it can say so: the api is condemned whatever the argument; the exact thing this line
    does was measured crashing; or, for a call, the argument kind the source shows was. A call whose
    argument is a variable is only reported on the first of those, since a variable might hold
    anything and a guess is not a fact."""
    row = condemned(api, facts)
    if row is not None:
        return row
    by_args = facts[where].get(api) or {}
    if where != "call":
        # a write is keyed by the value written
        for row in by_args.values():
            if row["outcome"] == "crashed":
                return row
        return None
    if kind_of_arg is None:
        return None
    row = by_args.get(kind_of_arg)
    return row if row and row["outcome"] == "crashed" else None


def check(path, facts=None, root=None):
    """Findings for one lua file, each quoting facts.db."""
    facts = facts or load()
    if facts is None:
        return []
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    code = _code(text)
    made = {}
    out = []
    for n, line in enumerate(code.splitlines(), 1):
        for name, cls in MADE.findall(line):
            made[name] = cls
        # a field on something a constructor made
        for name, field, tail in FIELD.findall(line):
            cls = made.get(name)
            if not cls:
                continue
            where = "write" if tail.startswith("=") else "read"
            row = _crashes("%s#%s" % (cls, field), facts, where)
            if row:
                out.append(_finding(rel, n, "measured-crash", "%s#%s" % (cls, field),
                                    "the walk took the client down on a %s of this field" % where, row))
        for name, method, args in METHOD.findall(line):
            cls = made.get(name)
            if not cls:
                continue
            row = _crashes("%s#%s" % (cls, method), facts, "call", _kind(args))
            if row:
                out.append(_finding(rel, n, "measured-crash", "%s#%s" % (cls, method), _why(row), row))
        for base, fn, args in DOTTED.findall(line):
            api = base + "." + fn
            if base in made:
                continue
            row = _crashes(api, facts, "call", _kind(args))
            if row:
                out.append(_finding(rel, n, "measured-crash", api, _why(row), row))
    return out


def check_tree(target, build=None):
    """Every lua file under a folder, or one file."""
    facts = load(build=build)
    if facts is None:
        return {"error": "no client facts: run qd groundtruth, then rs3_client_facts import=true"}
    root = target if os.path.isdir(target) else os.path.dirname(target)
    files = []
    if os.path.isdir(target):
        for here, _, names in os.walk(target):
            files += [os.path.join(here, f) for f in names if f.endswith(".lua")]
    else:
        files = [target]
    found = []
    for f in sorted(files):
        found += check(f, facts, root)
    return {"build": facts["build"], "files": len(files), "findings": found}
