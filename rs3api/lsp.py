"""What the language server says about a workspace, counted properly.

luac says whether it compiles, LuaLS says everything else. Every way of reading it wrong reports
ZERO rather than an error, so a false clean is what this is built against.

  the level    `--checklevel` reports its level and above: Error, Warning, Information, Hint.
               A run at Information reported 0 for a folder holding 32 unused locals
  the progress the bar shares the stream with carriage returns, so a diagnostic lands beside
               `>>>===== 186/502`. Split on \r first
  the paths    reported relative to a common ancestor LuaLS picks itself, which is neither the
               workspace nor the working directory. Compared as given, everything reads clean

Hint is the default. The level is reported back, so a zero says what it is a zero of.

A run is most of a minute, so it caches on the size and mtime of every lua file. rs3_lua_check
reads that cache without running anything.

  rs3_lsp  what LuaLS says about a file, a folder, or a whole workspace
"""

import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile

# `path\to\file.lua:12:34 [Warning] message here (rule-name)`
ROW = re.compile(r"^(?P<file>.+?\.lua):(?P<line>\d+):(?P<col>\d+)\s+"
                 r"\[(?P<level>\w+)\]\s+(?P<message>.*?)\s*(?:\((?P<rule>[a-z-]+)\))?$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# whatever the progress bar left in front
PROGRESS = re.compile(r"^.*[>=]{3,}[0-9 ]*/[0-9]+ *")

LEVELS = ("Error", "Warning", "Information", "Hint")

# build artifacts and vendored trees nobody can fix
NOT_YOURS = ("base/", "node_modules/", "dist/", ".git/")


def _server():
    return shutil.which("lua-language-server") or shutil.which("lua-language-server.exe")


def _workspace(path):
    """The folder LuaLS should be pointed at: the nearest ancestor with a .luarc.json.

    Without one there are no stubs, so every client global reads undefined. A subfolder resolves
    none of the requires and reports a hopeful zero."""
    at = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    while True:
        if os.path.exists(os.path.join(at, ".luarc.json")):
            return at, True
        up = os.path.dirname(at)
        if up == at:
            return os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path)), False
        at = up


def _stamp(root):
    """Size and mtime of every lua file under the workspace, as one digest.

    Sorted before hashing. os.walk's order is not promised twice, so the digest moved on its own
    and a run from before a morning's edits came back marked `cached`."""
    got = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "dist")]
        for f in fn:
            if not f.endswith(".lua"):
                continue
            p = os.path.join(dp, f).replace("\\", "/")
            try:
                st = os.stat(p)
            except OSError:
                continue
            got.append("%s|%d|%d" % (p, st.st_size, int(st.st_mtime)))
    h = hashlib.sha1()
    for line in sorted(got):
        h.update((line + ";").encode("utf-8", "replace"))
    return h.hexdigest()


def _cache_path(root):
    key = hashlib.sha1(os.path.abspath(root).encode("utf-8", "replace")).hexdigest()[:16]
    at = os.path.join(tempfile.gettempdir(), "rs3mcp-lsp")
    os.makedirs(at, exist_ok=True)
    return os.path.join(at, key + ".json")


def read_cache(root):
    """The last run for this workspace, and whether the tree moved since. Runs nothing.

    Returned even when stale: "48 last time, 12 files changed since" beats silence."""
    try:
        got = json.load(io.open(_cache_path(root), encoding="utf-8"))
    except Exception:
        return None
    got["stale"] = got.get("stamp") != _stamp(root)
    return got


def _parse(text):
    out = []
    for raw in ANSI.sub("", text).replace("\r", "\n").split("\n"):
        line = PROGRESS.sub("", raw).strip()
        if not line:
            continue
        m = ROW.match(line)
        if m is None:
            continue
        got = m.groupdict()
        out.append({"file": got["file"].replace("\\", "/"),
                    "line": int(got["line"]),
                    "level": got["level"],
                    "rule": got["rule"] or "",
                    "message": got["message"]})
    return out


def typings():
    """The generated api typings (docstypes.py), or None before the first docs fetch."""
    from rs3api import docstypes
    return docstypes.OUT if glob.glob(os.path.join(docstypes.OUT, "*.lua")) else None


def run(root, level="Hint", library=None):
    """Check one workspace and cache what came back. `library`: a folder of typings to check
    against when the workspace has no .luarc.json of its own."""
    server = _server()
    if server is None:
        raise RuntimeError("lua-language-server is not on PATH")
    stamp = _stamp(root)
    with tempfile.TemporaryDirectory() as log:
        cmd = [server, "--check", root, "--checklevel=" + level, "--logpath=" + log]
        if library:
            config = os.path.join(log, "luarc.json")
            json.dump({"runtime.version": "Lua 5.4", "workspace.library": [library]}, io.open(config, "w", encoding="utf-8"))
            cmd.append("--configpath=" + config)
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    got = {"root": os.path.abspath(root).replace("\\", "/"), "level": level, "stamp": stamp,
           "library": library, "rows": _parse((p.stdout or "") + "\n" + (p.stderr or ""))}
    try:
        json.dump(got, io.open(_cache_path(root), "w", encoding="utf-8"))
    except OSError:
        pass
    got["stale"] = False
    return got


def _resolve(base, reported):
    """The absolute file a reported path names.

    LuaLS picks its own common ancestor: checking plugins/quest-dev reported `Runescape API
    Project/plugins/quest-dev/core/region.lua`. Joined on the folders they share, then only used
    when the result exists. Concatenating matches nothing and reports a clean workspace."""
    got = reported.replace("\\", "/")
    if os.path.isabs(got):
        return got
    root, part = base.split("/"), got.split("/")
    for n in range(min(len(root), len(part)), 0, -1):
        if root[-n:] == part[:n]:
            joined = "/".join(root + part[n:])
            if os.path.exists(joined):
                return joined
    joined = base + "/" + got.lstrip("/")
    return joined if os.path.exists(joined) else got


def _under(rows, root, path):
    """The rows for one file or folder, as paths relative to the workspace."""
    base = os.path.abspath(root).replace("\\", "/").rstrip("/")
    want = os.path.abspath(path).replace("\\", "/").rstrip("/")
    out = []
    for r in rows:
        full = _resolve(base, r["file"])
        if not (full == want or full.startswith(want + "/")):
            continue
        rel = full[len(base) + 1:] if full.startswith(base + "/") else full
        out.append(dict(r, file=rel))
    return out


def _tally(rows, key):
    got = {}
    for r in rows:
        got[r[key] or "-"] = got.get(r[key] or "-", 0) + 1
    return dict(sorted(got.items(), key=lambda kv: -kv[1]))


def _by_folder(rows):
    got = {}
    for r in rows:
        top = r["file"].split("/")[0] if "/" in r["file"] else "."
        got[top] = got.get(top, 0) + 1
    return dict(sorted(got.items(), key=lambda kv: -kv[1]))


def tool_lsp(args):
    target = str(args.get("path") or "").strip()
    if not target:
        raise ValueError("give a path to a lua file, a folder, or a workspace")
    if not os.path.exists(target):
        raise ValueError("no such path: " + target)
    level = str(args.get("level") or "Hint").capitalize()
    if level not in LEVELS:
        raise ValueError("level is one of " + ", ".join(LEVELS))
    limit = int(args.get("limit") or 60)

    root, configured = _workspace(target)
    library = None if configured else typings()
    got = None if args.get("refresh") else read_cache(root)
    reused = (got is not None and not got.get("stale") and got.get("level") == level
              and got.get("library") == library)
    if not reused:
        got = run(root, level, library)

    rows = _under(got["rows"], root, target)
    skipped = [r for r in rows if any(s in "/" + r["file"] for s in NOT_YOURS)]
    yours = [r for r in rows if r not in skipped]

    out = {
        "workspace": got["root"],
        "checked": os.path.abspath(target).replace("\\", "/"),
        "level": got["level"],
        "found": len(yours),
        "byFolder": _by_folder(yours),
        "byRule": _tally(yours, "rule"),
        "byLevel": _tally(yours, "level"),
        "rows": yours[:limit],
        "truncated": len(yours) > limit,
        "cached": reused,
    }
    if got["level"] != "Hint":
        out["warning"] = ("this is a %s-level count. Error, Warning, Information and Hint each "
                          "report their own level and above, so hints an editor shows are NOT in "
                          "this number. Re-run at Hint before calling anything clean." % got["level"])
    if not configured and library:
        out["note"] = ("no .luarc.json above %s, so it was checked against the api typings "
                       "generated from the public docs (%s)." % (out["checked"], library.replace("\\", "/")))
    elif not configured:
        out["warning"] = ("no .luarc.json above %s and no generated api typings yet (run "
                          "rs3api/docsfetch.py), so every client global reads as undefined. Fix "
                          "that before reading these numbers." % out["checked"])
    if skipped:
        out["notYours"] = {"found": len(skipped),
                           "where": sorted({r["file"].split("/")[0] for r in skipped}),
                           "note": "build artifacts and vendored copies, left out of the count. A "
                                   "folder holding a second copy of a file doubles every "
                                   "diagnostic in BOTH copies, because a class name is "
                                   "workspace-wide. Add it to workspace.ignoreDir in .luarc.json "
                                   "and the real number appears."}
    if not yours:
        out["note"] = ("nothing at %s level under this path. That is the whole of what LuaLS "
                       "reports, hints included." % got["level"])
    return out


def note_for(path):
    """What rs3_lua_check says about the language server, without running it."""
    try:
        root, configured = _workspace(path)
        if not configured:
            return {"ran": False,
                    "problem": "no .luarc.json above this path, so LuaLS has nothing to check "
                               "against. Every client global would read as undefined."}
        got = read_cache(root)
        if got is None:
            return {"ran": False, "workspace": os.path.abspath(root).replace("\\", "/"),
                    "problem": "the language server has never been run on this workspace. "
                               "rs3_lsp path=<this> is the other half of the check: luac says it "
                               "compiles, LuaLS says whether the fields exist."}
        rows = _under(got["rows"], root, path)
        rows = [r for r in rows if not any(s in "/" + r["file"] for s in NOT_YOURS)]
        out = {"ran": True, "level": got["level"], "found": len(rows),
               "byRule": _tally(rows, "rule"), "rows": rows[:8]}
        if got.get("stale"):
            out["problem"] = ("that run is out of date, lua files have changed since. Re-run "
                              "rs3_lsp for the current answer.")
        if got["level"] != "Hint":
            out["problem"] = ("that run was at %s level, which excludes the hints an editor "
                              "shows. Re-run rs3_lsp at Hint." % got["level"])
        return out
    except Exception as e:
        return {"ran": False, "problem": str(e)[:160]}


TOOLS = [
    {"name": "rs3_lsp", "fn": tool_lsp, "examples": [{"path": "RS3Plugins/my-plugin"}],
     "description": "What the language server says about a lua file, folder or workspace: fields "
                    "that do not exist, nils never checked, unused locals. The half luac cannot "
                    "see. Reports at Hint, because every higher level drops rows the editor "
                    "shows and still returns a number that reads as an answer.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string",
                  "description": "a .lua file or folder; checked from the nearest .luarc.json, "
                                 "rows filtered to it"},
         "level": {"type": "string", "enum": list(LEVELS),
                   "description": "lowest level reported, Hint by default"},
         "refresh": {"type": "boolean", "description": "re-run even if nothing has changed"},
         "limit": {"type": "integer", "description": "rows to return, 60 by default"}}}},
]
