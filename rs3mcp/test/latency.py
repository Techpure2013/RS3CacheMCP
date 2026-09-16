"""Time every tool, cold and warm.

Cold is the first call in a process, which is what a fresh session pays. Warm is the second, which
is what everything after pays. A tool whose cold call is slow but warm call is fast is caching; one
that is slow both times is doing the work every call and wants an index.
"""
import io, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# From the server's registry. A copy lived here once and went stale.
import runpy
BY_NAME = runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "rs3-mcp.py"))["BY_NAME"]

CALLS = [
    ("rs3_schema", {}), ("rs3_lookup", {"kind": "item", "name": "Coins"}),
    ("rs3_query", {"sql": "select id from config where type='items' limit 5"}),
    ("rs3_sprite", {"name": "necrosis"}),
    ("rs3_family", {"kind": "loc", "id": "545"}),
    ("rs3_trace", {"kind": "item", "id": "2"}),
    ("rs3_script", {"pattern": "IF_SETHIDE", "limit": 5}),
    ("rs3_script_read", {"script": "0"}),
    ("rs3_var", {"name": "TT17_VARIABLE_2"}),
    ("rs3_dbtable", {"id": "34", "rows": "true", "limit": 10}),
    ("rs3_dbtable", {"text": "Currency"}),
    ("rs3_iface", {"id": "1188"}),
    ("rs3_drift", {}),
    ("rs3_cache_diff", {"type": "items", "limit": 5}),
    ("rs3_script_refs", {"kind": "varbit", "id": "27169"}),
    ("rs3_wiki", {}),
]
# an add-on's own tools, timed on the questions it records answers for
from rs3mcp import addons  # noqa
for _extra in addons.every("golden_calls"):
    CALLS += [(name, args) for name, args, *_need in _extra()]


def timed(name, args):
    t = BY_NAME.get(name)
    if not t:
        return None, None, "not registered"
    try:
        t0 = time.time()
        t["fn"](dict(args))
        cold = (time.time() - t0) * 1000
        t0 = time.time()
        t["fn"](dict(args))
        warm = (time.time() - t0) * 1000
        return cold, warm, None
    except Exception as e:
        return None, None, str(e)[:60]


# Anything registered is listed here or named as skipped. The list that lived in this file went
# stale and left five families unmeasured while the run printed "0 tools over 250ms".
# Tools that are slow because of what they do, not because of how they are written. Named with a
# reason so the bar stays meaningful: anything else over it is a regression.
EXPECTED_SLOW = {
    "rs3_cache_diff": "joins two 600MB databases; it runs once after a rebuild, not in a loop",
}

# the same exemptions the soak takes, for the same reasons
SKIP = {}
for _extra in addons.every("soak_skip"):
    SKIP.update(_extra)
untimed = sorted(set(BY_NAME) - {n for n, _ in CALLS} - set(SKIP))
if untimed:
    print("%d registered tool(s) are timed by nobody: %s" % (len(untimed), ", ".join(untimed)))
    print("add them to CALLS, or to SKIP with a reason.\n")

rows = []
for name, args in CALLS:
    cold, warm, err = timed(name, args)
    rows.append((name, cold, warm, err))

print("%-22s %9s %9s   %s" % ("tool", "cold ms", "warm ms", ""))
print("-" * 60)
slow = []
for name, cold, warm, err in rows:
    if err:
        print("%-22s %9s %9s   ERROR %s" % (name, "-", "-", err))
        continue
    flag = ""
    if warm > 250 and name in EXPECTED_SLOW:
        flag = "  slow by design"
    elif warm > 250:
        flag = "  SLOW every call"
        slow.append((name, warm))
    elif cold > 1500:
        flag = "  slow cold"
    print("%-22s %9.0f %9.0f %s" % (name, cold, warm, flag))

print()
print("tools over 250ms warm: %d" % len(slow))
for n, w in sorted(slow, key=lambda x: -x[1]):
    print("   %-22s %.0f ms" % (n, w))
