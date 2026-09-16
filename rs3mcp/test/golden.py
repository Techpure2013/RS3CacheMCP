"""
Every tool's answer on a fixed question, recorded, so a change that moves one is visible.

The known-answer tests in correctness.py check things I thought to check. This checks everything
else: it calls all 34 tools on fixed inputs, records the answers, and diffs them next time. The
block counter bug would have shown here as forty tool outputs moving at once, which is what a bug
in a shared parser looks like.

    python rs3-mcp.py --golden        record, overwriting the baseline
    python rs3-mcp.py --diff          compare against it

Recording is deliberate and separate. A diff that says nothing moved is worth something only if the
baseline was not quietly rewritten on the way past.

Volatile fields are dropped before comparing: absolute paths, row counts that grow as the wiki
cache fills, and anything with a timestamp in it. What is left is the shape and the substance of
the answer.
"""

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "golden")
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from rs3mcp import addons, common  # noqa

# One question per tool, chosen to exercise the parsing rather than the plumbing. A third item names
# what the answer reads beyond the database (test/workspace.py); without it the call is skipped, since
# the answer would only record that this machine lacks it. An add-on offers the calls for its own
# tools under "golden_calls".
CALLS = [
    ("rs3_schema", {}, "name lists"),
    ("rs3_dbtable", {"id": "66", "rows": "true", "limit": 4}, "name lists"),
    ("rs3_dbtable", {"column": "209088"}, "name lists"),
    ("rs3_dbtable", {"text": "Occult Trail"}, "name lists"),
    ("rs3_dbtable", {"drops": "true"}, "name lists"),
    ("rs3_dbtable", {"id": "97", "rows": "true", "limit": 3}, "name lists"),
    ("rs3_lookup", {"kind": "item", "name": "Coins"}, "name lists"),
    ("rs3_query", {"sql": "select id,name from config where type='items' and id in (2,4151) "
                          "order by id"}, "name lists"),
    ("rs3_sprite", {"name": "necrosis"}, "name lists"),
    ("rs3_family", {"kind": "item", "id": "4151"}, "name lists"),
    ("rs3_family", {"kind": "loc", "id": "545"}, "name lists"),
    ("rs3_trace", {"kind": "item", "id": "2"}, "name lists"),
    ("rs3_var", {"id": "-60893"}, "add-on"),
    ("rs3_var", {"name": "TT17_VARIABLE_2"}, "add-on"),
    ("rs3_var", {"name": "TRAIL_OUTFIT_BOOTS_CHARGE_PROGRESS"}, "add-on"),
    ("rs3_var", {"search": "EGWD_CAPSULE"}, "name lists"),
    ("rs3_iface", {"id": "18612224"}, "name lists"),
    ("rs3_iface", {"id": "1188"}, "name lists"),
    ("rs3_iface", {"text": "Slayer", "limit": 4}, "name lists"),
    ("rs3_script_read", {"script": "0"}),
    ("rs3_script", {"pattern": "IF_SETHIDE", "limit": 3}),
    ("rs3_wiki", {"page": "Abyssal demon"}),
    ("rs3_cache_diff", {"type": "items", "limit": 5}, "previous build"),
    ("rs3_script_refs", {}),
    ("rs3_script_refs", {"kind": "varbit", "id": "27169"}),
]

# A tool with no baseline is a tool whose answer can change without anyone seeing the diff. Same
# rule as the soak and the latency harness: driven here, or named with a reason. An add-on names
# its own under "no_baseline".
NO_BASELINE = {
    "rs3_drift": "its own result is the pass count of these checks, so it moves whenever a claim "
                 "is added -- which is the intended behaviour, not a regression",
}


def _roots():
    """Folders a path argument is named relative to, so a baseline reads the same on every machine."""
    return [p.replace("\\", "/") for offered in addons.every("path_roots") for p in offered]


def _no_baseline():
    out = dict(NO_BASELINE)
    for offered in addons.every("no_baseline"):
        out.update(offered)
    return out


# Fields that move on their own and say nothing about correctness. countedHoursAgo is the age of
# the cached counts, so it moves every run, and a baseline that always differs is one nobody reads.
# live, liveDumps and fromLiveDumps move whenever a new interface dump is taken.
VOLATILE = re.compile(r"^(path|target|store|store_path|cachedPages|knownMissing|fetched|"
                      r"generatedSkipped|countedHoursAgo|live|liveDumps|fromLiveDumps)$")
# The same age is spelled out in that tool's note, so the sentence carrying it goes too.
AGED = re.compile(r"\s*Counts are exact but were taken [\d.]+ hours ago[^.]*\.")
PATHY = re.compile(r"[A-Za-z]:[\\/][^\"]*")


def _norm(o):
    """Drop volatile fields, flatten absolute paths, round floats. Two runs of the same code must
    produce byte-identical output or the diff is noise."""
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in sorted(o.items()) if not VOLATILE.match(k)}
    if isinstance(o, list):
        return [_norm(x) for x in o]
    if isinstance(o, float):
        return round(o, 3)
    if isinstance(o, str):
        return PATHY.sub("<path>", AGED.sub("", o))
    return o


def _tools():
    """From the server, not from a list kept here. The copy that used to live in this file was
    missing four families, so those tools recorded __missing_tool__ instead of an answer and the
    baseline looked complete."""
    import runpy
    from rs3mcp.common import registry
    return registry()


def _where(path):
    """A path as what it points at, so the baseline has the same name on every machine."""
    s = str(path).replace("\\", "/")
    for root in _roots():
        if s.lower().startswith(root.lower() + "/"):
            return s[len(root) + 1:]
    return s


def _key(name, args):
    tag = "_".join("%s-%s" % (k, (_where(v) if k == "path" else str(v))[:24])
                   for k, v in sorted(args.items()))
    tag = re.sub(r"[^A-Za-z0-9._-]", "", tag)
    return "%s%s%s" % (name, "__" if tag else "", tag)


def calls():
    """CALLS and whatever the add-ons record answers for, as (name, args, need, home).

    home is the folder the baseline lives in. A call that cannot run on a fresh copy, because it
    reads something only a workspace has or its tool comes from an add-on, keeps its baseline in
    the folder an add-on offers for them, so what was recorded from the workspace stays with it.
    """
    from rs3mcp import addons
    local = addons.first("golden_home")
    reg = _tools()
    out = []
    for name, args, *need in CALLS:
        from_addon = name in reg and not reg[name]["fn"].__module__.startswith("rs3mcp.")
        workspace = bool(need) or from_addon or name not in reg
        out.append((name, args, need, local if (workspace and local) else BASE))
    for offered in addons.every("golden_calls"):
        out += [(name, args, need, local or BASE) for name, args, *need in offered()]
    return out


def homes():
    return sorted({c[3] for c in calls()} | {BASE})


def uncovered():
    """Registered tools with no baseline and no written reason."""
    return sorted(set(_tools()) - {c[0] for c in calls()} - set(_no_baseline()))


def run_all():
    """(answers, {key: why it was not asked}, {key: home}). An add-on's tool with the add-on not
    loaded, or a call reading something this machine does not have, is skipped rather than
    recorded."""
    from rs3mcp.test import workspace
    by = _tools()
    out, skipped, where = {}, {}, {}
    missing = uncovered()
    if missing:
        out["__uncovered__"] = {"tools": missing,
                                "fix": "add a call to CALLS, or a reason to NO_BASELINE"}
        where["__uncovered__"] = BASE
    for name, args, need, home in calls():
        key = _key(name, args)
        where[key] = home
        t = by.get(name)
        gone = workspace.missing(*need) if t else workspace.missing("add-on")
        if gone:
            skipped[key] = gone
            continue
        try:
            out[key] = _norm(t["fn"](dict(args)))
        except Exception as e:
            out[key] = {"__error__": str(e)[:200]}
    return out, skipped, where


def _say_skipped(skipped):
    if not skipped:
        return
    by = {}
    for key, gone in skipped.items():
        for why in gone:
            by.setdefault(why, []).append(key)
    for why, keys in sorted(by.items()):
        print("  skipped %d, needs %s" % (len(keys), why))


def record():
    got, skipped, where = run_all()
    for k, v in got.items():
        os.makedirs(where[k], exist_ok=True)
        io.open(os.path.join(where[k], k + ".json"), "w", encoding="utf-8", newline="\n").write(
            json.dumps(v, indent=1, ensure_ascii=False, sort_keys=True, default=str))
    print("recorded %d baselines to %s" % (len(got), ", ".join(sorted({where[k] for k in got}))))
    # a skipped call keeps whatever baseline it had, so recording here cannot erase another machine's
    _say_skipped(skipped)
    return 0


def _walk(a, b, path=""):
    """Field-level differences, so a moved number names itself rather than dumping both blobs."""
    out = []
    if type(a) is not type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append((path + "/" + k, "<absent>", b[k])) if False else None
                out.append((path + "/" + k, None, b[k]))
            elif k not in b:
                out.append((path + "/" + k, a[k], None))
            else:
                out += _walk(a[k], b[k], path + "/" + k)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path + "[]", "%d items" % len(a), "%d items" % len(b))]
        for i, (x, y) in enumerate(zip(a, b)):
            out += _walk(x, y, "%s[%d]" % (path, i))
        return out
    if a != b:
        return [(path, a, b)]
    return out


def diff(verbose=False):
    if not os.path.isdir(BASE):
        print("no baseline. Run: python rs3-mcp.py --golden")
        return 1
    got, skipped, where = run_all()
    stored = {}
    for home in homes():
        for f in sorted(os.listdir(home)) if os.path.isdir(home) else ():
            if f.endswith(".json") and f[:-5] not in skipped:
                stored[f[:-5]] = json.load(io.open(os.path.join(home, f), encoding="utf-8"))

    moved, gone, added = [], [], []
    for k in sorted(set(stored) | set(got)):
        if k not in got:
            gone.append(k)
        elif k not in stored:
            added.append(k)
        else:
            d = _walk(stored[k], got[k])
            if d:
                moved.append((k, d))

    print("golden diff: %d baselines, %d moved, %d new, %d gone"
          % (len(stored), len(moved), len(added), len(gone)))
    _say_skipped(skipped)
    for k in added:
        print("  NEW    %s" % k)
    for k in gone:
        print("  GONE   %s" % k)
    for k, d in moved:
        print("  MOVED  %s   (%d fields)" % (k, len(d)))
        for path, was, now in d[:6 if not verbose else 40]:
            print("      %-46s %s -> %s" % (path[:46], str(was)[:34], str(now)[:34]))
        if len(d) > 6 and not verbose:
            print("      ... %d more" % (len(d) - 6))
    if not moved and not gone:
        print("nothing moved")
    return 1 if (moved or gone) else 0


if __name__ == "__main__":
    sys.exit(record() if "--record" in sys.argv else diff("-v" in sys.argv))
