"""
Hammer every tool with real data and report what breaks.

The known-answer tests check inputs I thought of. The golden baseline checks that fixed answers
stay fixed. Neither touches the other 61,589 items, and a tool that works on Coins and dies on a
noted shard is still broken.

This draws real ids and names out of the cache and calls every tool hundreds of times with them,
checking three things:

  it answers          no exception, and the result is a dict with something in it
  it agrees           where two tools know the same fact they must say the same thing
  it stays quick      nothing crawls on an input that happens to be large

    python rs3mcp/test/soak.py            a few hundred calls per tool
    python rs3mcp/test/soak.py --deep     several thousand, slower
    python rs3mcp/test/soak.py --seed 7   reproduce a previous run exactly

Failures print the exact call that caused them, so a bug is one paste away from a repro.
"""

import io
import json
import os
import random
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rs3mcp.common import connect  # noqa

SLOW_MS = 1500

STALE_DAYS = 14

# Tools allowed past SLOW_MS, with what they are doing to deserve it. Anything not named here is
# held to the bar; adding a name is meant to be a decision somebody made, not a threshold nudged up
# until the run went quiet.
SLOW_ALLOWANCE = {
    "rs3_drift": (3000, "re-runs all 24 drift checks and the listing budget in one call, which is "
                        "the job rather than a slow query"),
}

FAILS = []
STATS = {}


STAMP = os.path.join(ROOT, "data", ".soak-last.json")


def write_stamp(seed, deep, calls, failures):
    """Record that a run happened and how it went.

    Written even when the run fails, because a failing run is still a run: the interesting question
    for staleness is when the tools were last exercised, not whether they passed."""
    try:
        io.open(os.path.abspath(STAMP), "w", encoding="utf-8", newline="\n").write(
            json.dumps({"when": int(time.time()), "seed": seed, "deep": deep,
                        "calls": calls, "failures": failures}, indent=1))
    except Exception:
        pass


def read_stamp():
    """The last run, or None. Callers decide what counts as too old."""
    try:
        d = json.loads(io.open(os.path.abspath(STAMP), encoding="utf-8").read())
    except Exception:
        return None
    d["ageDays"] = round((time.time() - d["when"]) / 86400.0, 1)
    return d



def fail(tool, args, why, detail=""):
    FAILS.append((tool, args, why, detail))


def call(fn, tool, args, want_keys=()):
    """One call, timed, with the failure attached to its input."""
    t0 = time.time()
    try:
        r = fn(dict(args))
    except Exception as e:
        fail(tool, args, "raised", "%s: %s" % (type(e).__name__, str(e)[:120]))
        return None
    ms = (time.time() - t0) * 1000
    s = STATS.setdefault(tool, {"n": 0, "ms": 0.0, "max": 0.0, "slow": 0})
    s["n"] += 1
    s["ms"] += ms
    s["max"] = max(s["max"], ms)
    if ms > SLOW_ALLOWANCE.get(tool, (SLOW_MS, ""))[0]:
        s["slow"] += 1
        fail(tool, args, "slow", "%.0f ms" % ms)
    if not isinstance(r, dict):
        fail(tool, args, "not a dict", type(r).__name__)
        return r
    if not r:
        fail(tool, args, "empty result", "")
        return r
    for k in want_keys:
        if k not in r:
            fail(tool, args, "missing key", k)
    return r


def sample(c, kind, n, rnd):
    rows = c.execute("select id, name, display from config where type=? order by id",
                     (kind,)).fetchall()
    return rnd.sample(rows, min(n, len(rows)))


def main():
    if "--stamp" in sys.argv:
        d = read_stamp()
        if d is None:
            print("  soak: never run here.  python rs3mcp/test/soak.py")
        else:
            print("  soak: %.1f days ago, %d calls, %d failures%s"
                  % (d["ageDays"], d["calls"], d["failures"],
                     "  python rs3mcp/test/soak.py" if d["ageDays"] > STALE_DAYS else ""))
        return 0
    deep = "--deep" in sys.argv
    seed = 1
    if "--seed" in sys.argv:
        seed = int(sys.argv[sys.argv.index("--seed") + 1])
    rnd = random.Random(seed)
    N = 400 if deep else 120
    print("soak: seed %d, about %d samples per kind%s" % (seed, N, ", deep" if deep else ""))

    c = connect()
    from rs3mcp.cache import tool_lookup
    from rs3mcp.trace import tool_trace
    from rs3mcp.families import tool_family
    from rs3mcp.vars import tool_var
    from rs3mcp.iface import tool_iface
    # every tool the server registers, add-on included, so this never names an add-on module
    import runpy
    reg = runpy.run_path(os.path.join(ROOT, "rs3-mcp.py"))["BY_NAME"]

    # ---- items, npcs, locs: lookup, trace and family agree on the name
    for kind, ctype in (("item", "items"), ("npc", "npcs"), ("loc", "objects")):
        rows = sample(c, ctype, N, rnd)
        print("  %-5s %d ids" % (kind, len(rows)), flush=True)
        for r in rows:
            cid = str(r["id"])
            lk = call(tool_lookup, "rs3_lookup", {"kind": kind, "id": cid})
            tr = call(tool_trace, "rs3_trace", {"kind": kind, "id": cid})
            call(tool_family, "rs3_family", {"kind": kind, "id": cid})
            if lk and tr:
                a = lk.get("name") or (lk.get("results") or [{}])[0].get("name")
                b = tr.get("name")
                if a and b and a != b:
                    fail("agreement", {"kind": kind, "id": cid},
                         "lookup and trace disagree on the name", "%s vs %s" % (a, b))

    # ---- vars: every varbit must resolve, and its base must exist and contain it
    vb = rnd.sample(c.execute("select id, name, base_id from varbit order by id").fetchall(),
                    min(N, c.execute("select count(*) from varbit").fetchone()[0]))
    print("  vars  %d varbits" % len(vb), flush=True)
    for r in vb:
        v = call(tool_var, "rs3_var", {"id": str(r["id"])}, ("name", "base"))
        if not v:
            continue
        base = (v.get("base") or {}).get("id")
        if base != r["base_id"]:
            fail("rs3_var", {"id": r["id"]}, "base id disagrees with the varbit table",
                 "%s vs %s" % (base, r["base_id"]))
        if v.get("bits") and not v.get("range"):
            fail("rs3_var", {"id": r["id"]}, "has bits but no range", "")
        b = call(tool_var, "rs3_var", {"id": str(r["base_id"])})
        if b and b.get("packs") is not None:
            if not any(p["varbit"] == r["id"] for p in b["packs"]):
                fail("rs3_var", {"id": r["base_id"]}, "base does not list its own varbit",
                     str(r["id"]))

    # ---- interfaces: packed ids must round-trip through the tool
    all_ifs = c.execute("select distinct id/65536 as i from config where type='interfaces' "
                        "order by 1").fetchall()
    ifs = rnd.sample(all_ifs, min(N, 200, len(all_ifs)))
    print("  iface %d interfaces" % len(ifs), flush=True)
    for r in ifs:
        d = call(tool_iface, "rs3_iface", {"id": str(r["i"])})
        if not d or not d.get("components"):
            continue
        root = d.get("root")
        root = root[0] if isinstance(root, list) and root else root
        if isinstance(root, int):
            one = call(tool_iface, "rs3_iface", {"id": "%d:%d" % (r["i"], root)})
            if one and one.get("packedID") is not None:
                again = call(tool_iface, "rs3_iface", {"id": str(one["packedID"])})
                if again and again.get("at") != "%d:%d" % (r["i"], root):
                    fail("rs3_iface", {"id": one["packedID"]}, "packed id does not round-trip",
                         str(again.get("at")))

    # item names a person would type, for the add-ons that take names
    names = [r["display"] for r in sample(c, "items", 60, rnd) if r["display"]]

    # ---- sprites, both ways in, and the png must be where it says it is
    from rs3mcp.cache import tool_sprite
    snames = rnd.sample([r[0] for r in c.execute("select name from sprite_name order by name")],
                        min(N, 120))
    sids = rnd.sample([r[0] for r in c.execute(
        "select distinct sprite_id from sprite_use order by 1")], N)
    print("  sprite %d names, %d ids" % (len(snames), len(sids)), flush=True)
    for nm in snames:
        call(tool_sprite, "rs3_sprite", {"name": nm})
    for i in sids:
        r = call(tool_sprite, "rs3_sprite", {"id": str(i)})
        if not r:
            continue
        for got in (r.get("sprites") or r.get("results") or [])[:2]:
            fp = got.get("file") or got.get("path")
            if fp and not os.path.exists(fp):
                fail("rs3_sprite", {"id": i}, "png named but not on disk", str(fp)[:70])

    # ---- clientscript search, with patterns drawn from real opcodes
    from rs3mcp.script import tool_script
    pats = ["IF_SETHIDE", "CC_SETONOP", "INV_GETVAR", "IF_SETTEXT", "SOUND_SYNTH",
            "IF_SETONVARTRANSMIT", "OC_NAME", "NPC_FINDEXACT"]
    print("  script %d patterns" % len(pats), flush=True)
    for pat in pats:
        call(tool_script, "rs3_script", {"pattern": pat, "limit": 5})

    # ---- the tools that take a name or a path rather than an id
    #
    # These were exercised by nobody for months while the run still printed 0 failures over 11 of
    # 38 tools. They are driven the same way: real inputs drawn from the same sources a person
    # would type, not a fixed list of things known to work.
    def hit(name, argsets):
        # an add-on's tool, with that add-on not loaded
        if name not in reg:
            return
        fn = reg[name]["fn"]
        for a in argsets:
            call(fn, name, a)

    # an add-on soaks its own tools, with the same hit() and the same random picks
    from rs3mcp import addons
    for extra in addons.every("soak"):
        extra(hit, fail, rnd, N, names)

    tabs = [r["id"] for r in c.execute(
        "select id from config where type='dbtables' order by id").fetchall()]
    pick = rnd.sample(tabs, min(N // 4, len(tabs)))
    print("  dbtab %d tables, with rows and packed columns" % len(pick), flush=True)
    hit("rs3_dbtable", [{"id": str(t)} for t in pick]
        + [{"id": str(t), "rows": "true", "limit": 5} for t in pick[:20]]
        # a packed column for each, plus a table id sitting where a packed id goes, which is the
        # shape a caller gets wrong first
        + [{"column": str((t << 12) | (0 << 4))} for t in pick[:20]]
        + [{"column": str(t)} for t in pick[:5]]
        + [{"search": "TABLE"}, {}]
        + [{"text": w} for w in ("Pouch", "Trail", "Currency", "zzzznope")]
        # key-only tables go down a different branch, so they are soaked explicitly
        + [{"id": "97", "rows": "true"}, {"id": "144", "rows": "true"}]
        + [{"drops": "true"}, {"drops": "true", "all": "true"},
           {"text": "Pouch", "all": "true"}, {"text": "Dragon", "limit": 5}])

    print("  misc  the remaining readers", flush=True)
    hit("rs3_schema", [{}])
    hit("rs3_query", [{"sql": "select id,name from config where type='items' order by id",
                       "limit": 20}])
    hit("rs3_drift", [{}])
    hit("rs3_script_refs", [{}] + [{"kind": k} for k in ("varbit","obj","iface","script")]
                           + [{"kind": "varbit", "id": str(v)} for v in (27169, 38842, 22875)])
    hit("rs3_cache_diff", [{"type": t, "limit": 5} for t in ("items","objects","npcs")])
    hit("rs3_script_read", [{"script": str(i)} for i in rnd.sample(range(0, 4000), 20)])

    # A tool nobody soaks is a tool whose failure nobody would see. Anything registered has to be
    # driven above or named here with a reason.
    SKIP = {"rs3_wiki": "goes to the network, and the courtesy gap makes a soak antisocial"}
    for extra in addons.every("soak_skip"):
        SKIP.update(extra)
    missed = sorted(set(reg) - set(STATS) - set(SKIP))
    if missed:
        fail("coverage", {}, "registered but soaked by nobody", ", ".join(missed))

    # ---- report
    print()
    print("%-18s %7s %9s %9s %7s" % ("tool", "calls", "avg ms", "max ms", "slow"))
    print("-" * 56)
    total = 0
    for t, s in sorted(STATS.items(), key=lambda kv: -kv[1]["n"]):
        total += s["n"]
        print("%-18s %7d %9.1f %9.0f %7d"
              % (t, s["n"], s["ms"] / s["n"], s["max"], s["slow"]))
    print("-" * 56)
    print("%d calls, %d failures" % (total, len(FAILS)))
    write_stamp(seed, deep, total, len(FAILS))

    if FAILS:
        print()
        by = {}
        for tool, args, why, detail in FAILS:
            by.setdefault((tool, why), []).append((args, detail))
        for (tool, why), rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print("  %-18s %-34s x%d" % (tool, why, len(rows)))
            for args, detail in rows[:3]:
                print("      %s   %s" % (json.dumps(args)[:70], detail[:60]))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
