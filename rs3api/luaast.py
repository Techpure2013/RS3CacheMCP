"""Structural search and rewrite over lua, by shape rather than by text.

A regex sees characters. `pcall(function() $$$B end)` sees the call whatever the body does, over
however many lines. guardsNothing is the regex version, and it works only because it was calibrated
against a corpus.

From oh-my-claudecode's ast_grep tools. Node parses, through @ast-grep/napi pinned at 0.31.1 --
0.40 moved lua out of the built-in languages and Lang.Lua is undefined there.

Rewrites preview by default, the other way round from filesystem's edit_file. A structural rewrite
crosses files; a line edit does not.
"""

import glob
import io
import json
import os
import subprocess

from .common import page

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "astgrep.mjs")


def _files(path):
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".lua") else []
    return sorted(glob.glob(os.path.join(path, "**", "*.lua"), recursive=True))


def _ask(files, pattern, rewrite, limit):
    """One round trip to the node bridge."""
    req = json.dumps({"files": files, "pattern": pattern,
                      "rewrite": rewrite, "limit": limit})
    try:
        p = subprocess.run(["node", BRIDGE], input=req, capture_output=True,
                           text=True, timeout=120)
    except FileNotFoundError:
        return {"error": "node is not on PATH, and the parsing is done in node"}
    except subprocess.TimeoutExpired:
        return {"error": "the parse did not finish in 120s"}
    if p.returncode != 0:
        bad = (p.stderr or "").strip()
        if "Cannot find module" in bad:
            return {"error": "@ast-grep/napi is missing. npm install @ast-grep/napi@0.31.1",
                    "why": "0.40 dropped lua from the built-in languages"}
        return {"error": bad[:300] or "node exited %d" % p.returncode}
    try:
        return json.loads(p.stdout or "{}")
    except ValueError:
        return {"error": "the bridge did not answer with json"}


def tool_lua_ast(args):
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("give a pattern, eg pcall(function() $$$B end)")
    path = str(args.get("path") or "").strip()
    if not path or not os.path.exists(path):
        raise ValueError("give a lua file or a folder that exists")

    rewrite = args.get("rewrite")
    apply_it = bool(args.get("apply"))
    limit = min(int(args.get("limit") or 40), 200)

    files = _files(path)
    if not files:
        return {"found": 0, "note": "no lua under that path"}

    # ask for one past the window's end
    start = max(0, int(args.get("start") or 0))
    got = _ask(files, pattern, rewrite, start + limit + 1)
    if got.get("error"):
        return got

    hits = got.get("matches") or []
    more = len(hits) > start + limit
    window, meta = page(hits[:start + limit], start, limit)
    if more:
        meta["nextStart"] = start + limit
        meta["more"] = "more matches. Ask again with start=%d" % (start + limit)
    meta.pop("total", None)

    rec = {"pattern": pattern, "filesScanned": got.get("scanned"),
           "matches": [{"file": h["file"], "line": h["line"], "code": h["text"],
                        **({"wouldBecome": h["rewritten"]} if h.get("rewritten") else {}),
                        **({"rewriteError": h["rewriteError"]} if h.get("rewriteError") else {})}
                       for h in window]}
    rec.update(meta)
    # only a folder has a tree to miss
    if more and len(files) > 1:
        rec["note"] = "stopped once this window was full, so filesScanned is not the whole tree"

    if rewrite and not apply_it:
        rec["preview"] = ("nothing was written. wouldBecome is what each match would turn into. "
                          "Pass apply=true once it reads right")
    elif rewrite and apply_it:
        # exactly the window previewed, never the look-ahead
        rec["changed"] = _apply(window)
    return rec


def _apply(hits):
    """Write the rewrites back, last match in a file first.

    Back to front, so an earlier edit does not move a later offset. The bridge hands back text, not
    offsets, so a match whose text appears twice is skipped rather than guessed at.
    """
    by_file = {}
    for h in hits:
        if h.get("rewritten"):
            by_file.setdefault(h["file"], []).append(h)

    done = []
    for path, rows in by_file.items():
        s = io.open(path, encoding="utf-8").read()
        n, skipped = 0, 0
        for h in sorted(rows, key=lambda r: -r["line"]):
            if s.count(h["text"]) != 1:
                skipped += 1
                continue
            s = s.replace(h["text"], h["rewritten"], 1)
            n += 1
        if n:
            io.open(path, "w", encoding="utf-8", newline="\n").write(s)
        rec = {"file": path, "rewritten": n}
        if skipped:
            rec["skipped"] = "%d matched text that appears more than once" % skipped
        done.append(rec)
    return done


TOOLS = [
    {"name": "rs3_lua_ast", "fn": tool_lua_ast, "examples": [{"pattern": "pcall(function() $$$B end)", "path": "RS3Plugins/my-plugin"}],
     "description": "Search lua by shape, not text. `pcall(function() $$$B end)` matches the call "
                    "however the body is written. $X is one node, $$$X is several. rewrite= "
                    "previews a replacement; only apply=true writes.",
     "inputSchema": {"type": "object", "required": ["pattern", "path"], "properties": {
         "pattern": {"type": "string", "description": "ast-grep pattern, $X and $$$X are holes"},
         "path": {"type": "string", "description": "a lua file or a folder"},
         "rewrite": {"type": "string", "description": "replacement, reusing the same holes"},
         "apply": {"type": "boolean", "description": "write it. Off by default, so you see first"},
         "limit": {"type": "integer", "description": "default 40"},
         "start": {"type": "integer", "description": "pass the nextStart a paged answer gives"}}}},
]
