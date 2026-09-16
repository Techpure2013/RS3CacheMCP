"""
Try a change without risking the live server.

    python rs3mcp/test/sandbox.py new hoist-index      copy the package to a scratch tree
    python rs3mcp/test/sandbox.py check hoist-index    run its tests and diff its answers
    python rs3mcp/test/sandbox.py files hoist-index    what differs from live
    python rs3mcp/test/sandbox.py promote hoist-index  copy back, but only if it checks out
    python rs3mcp/test/sandbox.py drop hoist-index

The point is the check step. It runs the known-answer tests inside the sandbox and diffs every
tool's answer against the live baseline, so a change shows both what it fixed and what it moved
that nobody asked it to. Four separate times today a shared parser was edited and something
unrelated shifted; that is what this is for.

promote refuses on a failing test. It does not refuse on a moved answer, because moving one is
usually the whole point -- it prints them and asks you to look.
"""

import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
ROOT = os.path.dirname(PKG)
DATA = os.path.join(ROOT, "data")
BOXES = os.path.join(DATA, "rs3mcp-sandbox")
SKIP = {"__pycache__", "golden"}


def _box(name):
    return os.path.join(BOXES, name)


def new(name):
    dest = _box(name)
    if os.path.exists(dest):
        print("already exists: " + dest)
        return 1
    os.makedirs(BOXES, exist_ok=True)
    shutil.copytree(PKG, os.path.join(dest, "rs3mcp"),
                    ignore=shutil.ignore_patterns(*SKIP))
    shutil.copy2(os.path.join(ROOT, "rs3-mcp.py"), os.path.join(dest, "rs3-mcp.py"))
    # the add-ons ride along, or the box answers without them and every test on their tools fails.
    # files and promote still only compare rs3mcp/
    sys.path.insert(0, ROOT)
    from rs3mcp import addons
    for pkg in addons.named():
        if os.path.isdir(os.path.join(ROOT, pkg)):
            shutil.copytree(os.path.join(ROOT, pkg), os.path.join(dest, pkg),
                            ignore=shutil.ignore_patterns(*SKIP))
    # the golden baseline stays shared: a sandbox is compared against live, not against itself
    print("sandbox %s ready at %s" % (name, dest))
    print("edit rs3mcp/ under there, then: sandbox check %s" % name)
    return 0


def files(name):
    dest = _box(name)
    if not os.path.isdir(dest):
        print("no such sandbox: " + name)
        return 1
    changed = []
    for f in sorted(os.listdir(PKG)):
        if not f.endswith(".py"):
            continue
        a = os.path.join(PKG, f)
        b = os.path.join(dest, "rs3mcp", f)
        if not os.path.exists(b):
            changed.append((f, "removed"))
            continue
        if io.open(a, encoding="utf-8").read() != io.open(b, encoding="utf-8").read():
            changed.append((f, "changed"))
    for f in sorted(os.listdir(os.path.join(dest, "rs3mcp"))):
        if f.endswith(".py") and not os.path.exists(os.path.join(PKG, f)):
            changed.append((f, "added"))
    if not changed:
        print("identical to live")
    for f, how in changed:
        print("  %-10s %s" % (how, f))
    return 0


def check(name):
    dest = _box(name)
    if not os.path.isdir(dest):
        print("no such sandbox: " + name)
        return 1
    # the sandbox has its own copy of the package but shares the golden baseline, so a diff is
    # against live rather than against itself
    gold = os.path.join(PKG, "test", "golden")
    boxgold = os.path.join(dest, "rs3mcp", "test", "golden")
    if os.path.isdir(boxgold):
        shutil.rmtree(boxgold, ignore_errors=True)
    if os.path.isdir(gold):
        shutil.copytree(gold, boxgold)

    print("--- tests, inside the sandbox")
    env = dict(os.environ, RS3MCP_DATA=DATA)
    r1 = subprocess.run([sys.executable, "rs3-mcp.py", "--test"], cwd=dest, env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r1.stdout.strip()[-1200:] or r1.stderr.strip()[-600:])

    print()
    print("--- answers, against the live baseline")
    r2 = subprocess.run([sys.executable, "rs3-mcp.py", "--diff"], cwd=dest, env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r2.stdout.strip()[-2500:] or r2.stderr.strip()[-600:])

    ok = r1.returncode == 0
    print()
    print("tests %s, answers %s"
          % ("pass" if ok else "FAIL",
             "unchanged" if r2.returncode == 0 else "moved (read the list above)"))
    return 0 if ok else 1


def promote(name):
    dest = _box(name)
    if not os.path.isdir(dest):
        print("no such sandbox: " + name)
        return 1
    r = subprocess.run([sys.executable, "rs3-mcp.py", "--test"], cwd=dest,
                       env=dict(os.environ, RS3MCP_DATA=DATA),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("refusing: tests fail in the sandbox")
        print(r.stdout.strip()[-900:])
        return 1
    n = 0
    for f in sorted(os.listdir(os.path.join(dest, "rs3mcp"))):
        if not f.endswith(".py"):
            continue
        src = os.path.join(dest, "rs3mcp", f)
        dst = os.path.join(PKG, f)
        if not os.path.exists(dst) or \
                io.open(src, encoding="utf-8").read() != io.open(dst, encoding="utf-8").read():
            shutil.copy2(src, dst)
            print("  promoted %s" % f)
            n += 1
    print("%d file(s) promoted. Re-record the baseline if the moves were intended:" % n)
    print("  python rs3-mcp.py --golden")
    return 0


def drop(name):
    dest = _box(name)
    if not os.path.isdir(dest):
        print("no such sandbox: " + name)
        return 1
    shutil.rmtree(dest, ignore_errors=True)
    print("dropped " + name)
    return 0


def ls():
    if not os.path.isdir(BOXES):
        print("no sandboxes")
        return 0
    for d in sorted(os.listdir(BOXES)):
        print("  " + d)
    return 0


CMDS = {"new": new, "check": check, "files": files, "promote": promote, "drop": drop}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("ls", "list"):
        sys.exit(ls())
    cmd = sys.argv[1]
    if cmd not in CMDS:
        print(__doc__)
        sys.exit(1)
    if len(sys.argv) < 3:
        print("need a sandbox name")
        sys.exit(1)
    sys.exit(CMDS[cmd](sys.argv[2]))
