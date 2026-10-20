"""
Every tool, a hundred calls each, with the spread rather than a single number.

    python rs3mcp/test/bench.py           every tool
    python rs3mcp/test/bench.py --runs 50 fewer, when something is slow
    python rs3mcp/test/bench.py --json    machine readable, for a report

A mean on its own hides the shape. A tool that answers in a millisecond and occasionally takes
three hundred is a different thing from one that always takes forty, and only the spread says
which you have. The first call is reported separately for the same reason: several tools read a
tree or a corpus once and are quick forever after, and averaging that in describes neither state.

The arguments come from the latency harness, so the two measure the same thing and cannot drift
apart. Anything it skips is skipped here, with the same reason.
"""

import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rs3mcp.common import registry  # noqa
from rs3mcp.test import latency  # noqa

# tools that reach out, not to be hammered
NETWORK = {"rs3_wiki", "rs3_db", "rs3_db_query", "rs3_db_where", "rs3_crosscheck", "rs3_drift"}


def measure(fn, args, runs):
    """(first call, then the spread of the rest)."""
    t = time.perf_counter()
    fn(dict(args))
    first = (time.perf_counter() - t) * 1000
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn(dict(args))
        times.append((time.perf_counter() - t) * 1000)
    return first, times


def main():
    runs = 100
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])
    as_json = "--json" in sys.argv

    reg = registry()
    seen, rows = set(), []
    for name, args in latency.CALLS:
        if name in seen:
            continue
        seen.add(name)
        t = reg.get(name)
        if not t:
            continue
        n = 10 if name in NETWORK else runs
        try:
            first, times = measure(t["fn"], args, n)
        except Exception as e:
            rows.append({"tool": name, "error": str(e)[:70]})
            continue
        times.sort()
        rows.append({
            "tool": name, "runs": n,
            "first": round(first, 3),
            "mean": round(statistics.fmean(times), 3),
            "median": round(statistics.median(times), 3),
            "p95": round(times[min(len(times) - 1, int(len(times) * 0.95))], 3),
            "max": round(times[-1], 3),
            "network": name in NETWORK,
        })

    for name, why in sorted(getattr(latency, "SKIP", {}).items()):
        rows.append({"tool": name, "skipped": why})

    if as_json:
        print(json.dumps(rows, indent=1))
        return 0

    done = [r for r in rows if "mean" in r]
    done.sort(key=lambda r: -r["mean"])
    print("%-22s %6s %9s %8s %8s %8s %8s" %
          ("tool", "runs", "first ms", "mean", "median", "p95", "max"))
    print("-" * 76)
    for r in done:
        print("%-22s %6d %9.2f %8.3f %8.3f %8.3f %8.3f%s"
              % (r["tool"], r["runs"], r["first"], r["mean"], r["median"], r["p95"], r["max"],
                 "  net" if r["network"] else ""))
    for r in rows:
        if "error" in r:
            print("%-22s ERROR %s" % (r["tool"], r["error"]))
        if "skipped" in r:
            print("%-22s not measured: %s" % (r["tool"], r["skipped"]))
    print("-" * 76)
    means = [r["mean"] for r in done]
    print("%d tools, mean of means %.2f ms, median tool %.3f ms, %d under 1 ms"
          % (len(done), statistics.fmean(means), statistics.median(means),
             sum(1 for m in means if m < 1)))
    slow = [r for r in done if r["p95"] > 250 and not r["network"]]
    print("local tools whose p95 is over 250ms: %s" % ([r["tool"] for r in slow] or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
