"""
Keep the cache tooling in this repo, and say when a copy has drifted.

    python sync.py            what differs, in either direction
    python sync.py --pull     take the working copy into the repo
    python sync.py --push     put the repo copy back where it runs from

The tooling (the mcp server, the database builder, the rebuild pipeline) runs from the desktop
because that is where the mcp is registered. This holds the copy of record.

Drift is reported both ways on purpose. A file newer in the working copy is work not yet saved; a
file newer here is work that was saved and then lost. Neither is safe to guess at, so nothing is
copied without being asked.
"""

import ast
import hashlib
import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# where the tools run from: the server, its scripts, and data/ beside them; rs3cache beside this
# repo unless RS3CACHE_LIVE names it
LIVE = os.environ.get("RS3CACHE_LIVE") or os.path.join(os.path.dirname(HERE), "rs3cache")

# (path in this repo, path where it actually runs)
TOOLING = [
    ("rs3-mcp.py", os.path.join(LIVE, "rs3-mcp.py")),
    ("build_db.py", os.path.join(LIVE, "build_db.py")),
    ("rebuild-cache.py", os.path.join(LIVE, "rebuild-cache.py")),
    ("rs3-cache.py", os.path.join(LIVE, "rs3-cache.py")),
    ("rs3.py", os.path.join(LIVE, "rs3.py")),
    ("rs3.cmd", os.path.join(LIVE, "rs3.cmd")),
    ("rs3", os.path.join(LIVE, "rs3")),
    ("rs3-icon.py", os.path.join(LIVE, "rs3-icon.py")),
    ("rs3-areas.py", os.path.join(LIVE, "rs3-areas.py")),
    ("model_obj.py", os.path.join(LIVE, "model_obj.py")),
    ("model_png.py", os.path.join(LIVE, "model_png.py")),
]

# the shared tools and their documentation. Add-on packages and the scripts that belong to them stay
# on the live copy; naming one here is all it takes to keep it
PKGS = ("rs3mcp", "docs")
# generated beside the tools and rebuilt on demand, so not worth keeping
SKIP = {"__pycache__", ".pytest_cache"}


def digest(path):
    try:
        with io.open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def package_files():
    """Every source file of the mcp packages, as (relative path, absolute working path)."""
    out = []
    walks = [os.walk(os.path.join(LIVE, p)) for p in PKGS if os.path.isdir(os.path.join(LIVE, p))]
    for dp, dn, fn in (step for walk in walks for step in walk):
        dn[:] = [d for d in dn if d not in SKIP]
        for f in sorted(fn):
            # Everything the package carries, not only its python. Filtering on .py and .json once
            # left out data files and test fixtures, a copy of record that could not pass its tests.
            if not f.endswith((".pyc", ".pyo")):
                live = os.path.join(dp, f)
                out.append((os.path.relpath(live, LIVE).replace("\\", "/"), live))
    return out


def pairs():
    """(repo path, working path, label) for everything tracked here."""
    out = []
    for rel, live in TOOLING:
        out.append((os.path.join(HERE, rel), live, rel))
    for rel, live in package_files():
        out.append((os.path.join(HERE, rel), live, rel))
    return out


def untracked_imports():
    """Modules a tracked tool imports that live beside it and are not tracked themselves.

    Presence in the folder is not the test; plenty there is nobody's business. Being imported by
    something already tracked is, because that makes it part of the pipeline whether or not anybody
    remembered to list it."""
    tracked = {os.path.basename(live) for _, live in TOOLING}
    tracked |= {os.path.basename(live) for _, live in package_files()}
    found = []
    for _, live in TOOLING:
        if not live.endswith(".py"):
            continue
        try:
            tree = ast.parse(io.open(live, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        for n in sorted(names):
            f = n + ".py"
            if f not in tracked and os.path.isfile(os.path.join(LIVE, f)):
                found.append((os.path.basename(live), f))
    return found



def report():
    same = missing_repo = missing_live = differ = 0
    lines = []
    for repo, live, label in pairs():
        a, b = digest(repo), digest(live)
        if a is None and b is None:
            continue
        if a is None:
            missing_repo += 1
            lines.append("  not saved here yet   %s" % label)
        elif b is None:
            missing_live += 1
            lines.append("  gone from where it runs  %s" % label)
        elif a != b:
            differ += 1
            newer = "working copy" if os.path.getmtime(live) > os.path.getmtime(repo) else "repo"
            lines.append("  differs (%s is newer)  %s" % (newer, label))
        else:
            same += 1
    loose = untracked_imports()
    for importer, f in loose:
        print("  not tracked, but %s imports it  %s" % (importer, f))
    print("%d in step, %d differ, %d not saved here, %d missing where they run"
          % (same, differ, missing_repo, missing_live))
    for line in lines:
        print(line)
    return differ + missing_repo + missing_live


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def pull():
    n = 0
    for repo, live, label in pairs():
        if digest(live) is not None and digest(repo) != digest(live):
            copy(live, repo)
            n += 1
            print("  saved  %s" % label)
    print("%d file(s) taken into the repo" % n)


def push():
    n = 0
    for repo, live, label in pairs():
        if digest(repo) is not None and digest(repo) != digest(live):
            copy(repo, live)
            n += 1
            print("  restored  %s" % label)
    print("%d file(s) written back" % n)


def write_manifest():
    rows = {}
    for repo, live, label in pairs():
        d = digest(repo)
        if d:
            rows[label] = d
    blob = {"files": rows,
            "note": "sha256 of each file as saved here. sync.py compares these against the "
                    "working copies and says which side is newer."}
    with io.open(os.path.join(HERE, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(blob, indent=1, sort_keys=True))
    print("manifest: %d files" % len(rows))


def main():
    if "--pull" in sys.argv:
        pull()
        write_manifest()
    elif "--push" in sys.argv:
        push()
    else:
        return 1 if report() else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
