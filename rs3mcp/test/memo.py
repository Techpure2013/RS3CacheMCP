"""The selftest's memory: a test that passed is not run again while nothing it touched has changed.

A running test's reach is recorded: the Python functions it executes (sys.monitoring), and the
files, databases, folders and Lua it opens. While all of that still matches, the test is skipped
and its checks count as passed. A test that failed or skipped runs again; --full runs everything.

In a changed Python file only the functions the test ran, and the top-level names they read, are
compared, by syntax tree, so editing one test re-runs that test and not the whole file.

It cannot guard what it cannot see: the environment, a running process, or a file read other than
through Python's own calls. That is what --full is for.
"""

import ast
import builtins
import glob
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

from rs3mcp import common

PATH = os.path.join(common.DEFAULT_ROOT, "selftest-memo.json")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = sys.monitoring.PROFILER_ID if hasattr(sys, "monitoring") else None
# what a test makes for itself
SCRATCH = os.path.abspath(tempfile.gettempdir())

_memo = None
_seen = None
_code = None
_real = {}
_watching = False
_parsed = {}


def full():
    return "--full" in sys.argv


def _load():
    global _memo
    if _memo is None:
        try:
            _memo = json.load(open(PATH, encoding="utf-8"))
        except (OSError, ValueError):
            _memo = {}
    return _memo


def save():
    if _memo is not None:
        with open(PATH, "w", encoding="utf-8") as fh:
            json.dump(_memo, fh)


def _stamp(path):
    """size and modified time, or a listing's names for a folder; None when it is gone."""
    try:
        if os.path.isdir(path):
            return sorted(os.listdir(path))
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _digest(node):
    return hashlib.sha1(ast.dump(node).encode()).hexdigest()[:16]


def _shape(path):
    """{"funcs": {qualname: hash}, "names": {top-level name: hash}} of a Python file, or None;
    parsed again only when the file has moved."""
    stamp = _stamp(path)
    kept = _parsed.get(path)
    if kept is None or kept[0] != stamp:
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except (OSError, SyntaxError, ValueError):
            _parsed[path] = (stamp, None)
            return None
        funcs, names = {}, {}

        def walk(body, prefix, in_func):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qual = prefix + node.name
                    funcs[qual] = _digest(node)
                    inner = qual + (".<locals>." if not isinstance(node, ast.ClassDef) else ".")
                    walk(node.body, inner, True)
                elif not in_func and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        for leaf in ast.walk(t):
                            if isinstance(leaf, ast.Name):
                                names[leaf.id] = _digest(node)
        walk(tree.body, "", False)
        _parsed[path] = (stamp, {"funcs": funcs, "names": names})
    return _parsed[path][1]


def _owner(qualname):
    """The def a code object belongs to: a lambda, a comprehension or a def nested in another
    belongs to the outermost def around it, whose digest covers its body. A nested def kept its
    own name, found in no tree, and that tracked its whole file: one test's edit made every
    lesson checked from that file due."""
    parts = qualname.split(".<locals>.", 1)[0].split(".")
    while parts and parts[-1].startswith("<"):
        parts.pop()
    return ".".join(parts)


def carried(key):
    """The checks a test last counted, when it passed and nothing it touched has moved; else None."""
    if full():
        return None
    got = _load().get(key)
    if not got or not got.get("passed"):
        return None
    for path, was in got["files"].items():
        now = _stamp(path)
        if now == was["stamp"]:
            continue
        if "funcs" not in was:
            return None
        shape = _shape(path)
        if shape is None:
            return None
        for qual, h in was["funcs"].items():
            if shape["funcs"].get(qual) != h:
                return None
        for name, h in was["names"].items():
            if shape["names"].get(name) != h:
                return None
    return got["checks"]


def _note(path):
    if _seen is not None and isinstance(path, (str, bytes, os.PathLike)):
        path = os.fsdecode(path)
        if path.startswith("file:"):
            path = path[5:].split("?", 1)[0]
        if os.path.exists(path):
            _seen.add(os.path.abspath(path))


def _on_start(code, offset):
    _code.append(code)
    return sys.monitoring.DISABLE


def _wrap(module, name, pick):
    real = getattr(module, name)
    _real[(module, name)] = real

    def spy(*args, **kwargs):
        for path in pick(args, kwargs):
            _note(path)
        return real(*args, **kwargs)
    setattr(module, name, spy)


def _lua(args, kwargs):
    cmd = args[0] if args else kwargs.get("args") or []
    out = []
    for part in (cmd if isinstance(cmd, (list, tuple)) else [cmd]):
        if isinstance(part, str) and part.endswith(".lua") and os.path.exists(part):
            # its dofile neighbours
            out += glob.glob(os.path.join(os.path.dirname(os.path.abspath(part)), "*.lua"))
    return out


def start():
    """Begin recording what the next test touches."""
    global _seen, _code, _watching
    _seen, _code = set(), []
    _watching = False
    if TOOL is not None:
        try:
            sys.monitoring.use_tool_id(TOOL, "selftest memo")
        except ValueError:
            # a debugger or coverage holds it
            pass
        else:
            _watching = True
            sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, _on_start)
            sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
            sys.monitoring.restart_events()
    first = lambda args, kwargs: [args[0]] if args else []
    _wrap(builtins, "open", first)
    _wrap(io, "open", first)
    _wrap(sqlite3, "connect", lambda args, kwargs: [args[0] if args else kwargs.get("database")])
    _wrap(os, "listdir", lambda args, kwargs: [args[0] if args else "."])
    _wrap(os, "scandir", lambda args, kwargs: [args[0] if args else "."])
    _wrap(subprocess, "Popen", _lua)


def _kept(path):
    # windows paths ignore case
    p = os.path.normcase(path)
    return (not p.startswith(os.path.normcase(SCRATCH)) and p != os.path.normcase(os.path.abspath(PATH))
            and (p.startswith(os.path.normcase(ROOT)) or not p.startswith(os.path.normcase(sys.prefix))))


def _read_modules(reads, have, kept=None):
    """Loaded modules whose top-level values the test read from elsewhere, such as a TOOLS list.

    A module imported before the test and never run or opened by it is otherwise invisible,
    so its edits carried the test (the listing budget passed at one character over).
    """
    kept = kept or _kept
    out = {}
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        path = os.path.abspath(path)
        if path in have or not kept(path) or not os.path.exists(path):
            continue
        shape = _shape(path)
        used = {n: h for n, h in (shape or {}).get("names", {}).items() if n in reads}
        if used:
            out[path] = {"stamp": _stamp(path), "funcs": {}, "names": used}
    return out


def stop(key, passed, checks):
    """Stop recording and keep what the test touched, under key."""
    global _seen, _code
    if _watching:
        sys.monitoring.set_events(TOOL, 0)
        sys.monitoring.free_tool_id(TOOL)
    for (module, name), real in _real.items():
        setattr(module, name, real)
    _real.clear()
    files = _files(_seen, _code)
    _seen, _code = None, None
    # unseen code cannot be trusted as unchanged
    _load()[key] = {"passed": passed and _watching, "checks": checks, "files": files}


def _files(seen, codes, kept=None):
    """{path: stamp, and the functions and names used} for the files opened and the code run."""
    kept = kept or _kept
    ran, reads = {}, set()
    for code in codes or ():
        path = os.path.abspath(code.co_filename)
        if os.path.exists(path) and kept(path):
            ran.setdefault(path, set()).add(_owner(code.co_qualname))
            reads.update(code.co_names)
    files = {}
    for path in sorted((seen or set()) | set(ran)):
        if not kept(path):
            continue
        entry = {"stamp": _stamp(path)}
        # files read as text track whole
        shape = _shape(path) if path.endswith(".py") and path not in (seen or ()) else None
        if shape is not None:
            entry["funcs"] = {q: shape["funcs"][q] for q in ran.get(path, ()) if q in shape["funcs"]}
            entry["names"] = {n: h for n, h in shape["names"].items() if n in reads}
            if any(q not in shape["funcs"] and q for q in ran.get(path, ())):
                # code not found in the file's own tree
                entry.pop("funcs")
                entry.pop("names")
        files[path] = entry
    files.update(_read_modules(reads, files, kept))
    return files
