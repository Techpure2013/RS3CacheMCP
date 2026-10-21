"""Run Lua against a plugin in a sandbox shaped like the RS3 client.

A copy of the plugin goes into data/lab/work, never the original. lab_runner.lua loads it with the
client's rules (shared _G, per-plugin _ENV, a require that returns nil for a missing module, no os,
io, package or debug) and runs a probe script against it, under a time and memory limit.

    from rs3api import lab
    lab.run(plugin_root, entry="main.lua", script="return lab.require('core/x').f(1)")
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile

from rs3mcp.common import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "lab_runner.lua")
WORK = os.path.join(ROOT, "lab", "work")
# the client runs 5.4, so 5.5 would mislead
CANDIDATES = [os.environ.get("RS3_LUA54") or "",
              os.path.join(os.path.expanduser("~"), "luatools", "bin", "lua54.exe"),
              os.path.join(os.path.expanduser("~"), "lua54", "lua54.exe"),
              shutil.which("lua5.4") or "", shutil.which("lua54") or ""]


def lua54():
    """The Lua 5.4 interpreter, or None."""
    for c in CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return None


def _tree_hash(root):
    h = hashlib.sha1()
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in (".git", "node_modules", "test"))
        for f in sorted(fn):
            p = os.path.join(dp, f)
            st = os.stat(p)
            h.update(("%s:%d:%d\n" % (os.path.relpath(p, root), st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()[:12]


def work_copy(root):
    """A copy of the plugin to run against, reused while the plugin is unchanged."""
    dest = os.path.join(WORK, "%s-%s" % (os.path.basename(os.path.normpath(root)), _tree_hash(root)))
    if not os.path.isdir(dest):
        shutil.copytree(root, dest, ignore=shutil.ignore_patterns(".git", "node_modules"))
    return dest


def _lua_string(s):
    return "nil" if s is None else "[==[" + s.replace("]==]", "]==] .. ']==]' .. [==[") + "]==]"


def _lua_list(items):
    return "{ " + ", ".join(_lua_string(str(s)) for s in items or []) + " }"


def run(root, entry=None, script=None, seed=1, seconds=5, mb=256, trace=False, why=None, brk=None,
        discover=False, model=False, list_gaps=False):
    """One run. Returns the runner's outcome: ok, result, error, stopped, prints, missing,
    required, findings, seconds, memoryKB, root. With trace, the recording is saved and its id
    comes back as run. why=(path, event seq) and brk=(condition, [expressions]) are replays. With
    model, the client answers only from what the live client was measured doing (clientmodel);
    list_gaps carries on past what it can't answer, to name it all in one run."""
    exe = lua54()
    if not exe:
        return {"ok": False, "error": "no Lua 5.4 interpreter found; set RS3_LUA54"}
    if not os.path.isdir(root):
        return {"ok": False, "error": "no plugin folder at %s" % root}
    work = work_copy(root)
    model_paths = "modelCode = nil, model = nil, harvestDir = nil"
    if model:
        from . import clientmodel
        built = clientmodel.ensure()
        if built.get("error"):
            return {"ok": False, "error": built["error"]}
        model_paths = "listGaps = %s, modelCode = %s, model = %s, harvestDir = %s" % ((
            "true" if list_gaps else "false",) + tuple(
            _lua_string(p.replace("\\", "/"))
            for p in (os.path.join(HERE, "lab_model.lua"), clientmodel.MODEL, clientmodel.HARVEST)))
    tmp = tempfile.mkdtemp(prefix="lab-")
    try:
        spec = os.path.join(tmp, "spec.lua")
        out = os.path.join(tmp, "out.json")
        io.open(spec, "w", encoding="utf-8").write(
            "return { root = %s, entry = %s, script = %s, seed = %d, seconds = %s, mb = %d, "
            "trace = %s, whyPath = %s, whyAt = %s, breakWhen = %s, breakShow = %s, discover = %s, %s }\n"
            % (_lua_string(work.replace("\\", "/")), _lua_string(entry), _lua_string(script),
               int(seed), float(seconds), int(mb), "true" if trace else "false",
               _lua_string(why[0]) if why else "nil", int(why[1]) if why else "nil",
               _lua_string(brk[0]) if brk else "nil", _lua_list(brk[1]) if brk else "nil",
               "true" if discover else "false", model_paths))
        try:
            p = subprocess.run([exe, RUNNER, spec, out], capture_output=True, text=True,
                               timeout=float(seconds) + 10)
        except subprocess.TimeoutExpired:
            return {"ok": False, "stopped": "wall clock", "error": "the runner did not come back",
                    "root": work}
        if not os.path.exists(out):
            return {"ok": False, "error": "the runner died: %s" % (p.stderr or p.stdout).strip()[:400],
                    "root": work}
        outcome = json.load(io.open(out, encoding="utf-8"))
        if trace and outcome.get("trace"):
            from .labtrace import store
            outcome["run"] = store(outcome.pop("trace"), root, work, entry, script, seed, seconds, mb)
        return outcome
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def order_dependent(root, entry=None, script=None, seed=1, runs=5, seconds=5, mb=256):
    """Run the same seed several times. Lua 5.4 hashes strings with a per-process seed, so code
    whose result depends on pairs order gives different answers from run to run, here and in the
    client. Returns the first run's outcome plus orderDependent and the distinct results seen."""
    outcomes = [run(root, entry, script, seed, seconds, mb) for _ in range(max(2, int(runs)))]
    seen = []
    for o in outcomes:
        key = json.dumps(o.get("result"), sort_keys=True)
        if key not in seen:
            seen.append(key)
    first = dict(outcomes[0])
    first["orderDependent"] = len(seen) > 1
    first["distinctResults"] = len(seen)
    return first


def query(run, op, **kw):
    """One question about a traced run; see labtrace."""
    from .labtrace import query as ask
    return ask(run, op, **kw)


def tool_lab_run(args):
    path = str(args.get("path") or "").strip()
    if not path:
        raise ValueError("give path, the plugin folder to run against")
    out = run(path, entry=args.get("entry") or None, script=args.get("script") or None,
              seed=int(args.get("seed") or 1), seconds=float(args.get("seconds") or 5),
              mb=int(args.get("mb") or 256), trace=bool(args.get("trace")),
              model=bool(args.get("model")) or bool(args.get("gaps")), list_gaps=bool(args.get("gaps")))
    # the last lines are what explain an error
    prints = out.get("prints") or []
    if len(prints) > 40:
        out["printsShown"] = "last 40 of %d" % len(prints)
        out["prints"] = prints[-40:]
    return out


TOOLS = [
    {"name": "rs3_lab_run", "fn": tool_lab_run,
     "examples": [{"path": "C:/Users/you/RS3Plugins/my-plugin",
                   "script": "return lab.require('core/textfmt').fmt(1234567)"}],
     "description": "Run a Lua probe against a sandboxed copy of a plugin, loaded as the client loads it; "
                    "lab.fire sends events.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "the plugin folder"},
         "entry": {"type": "string", "description": "file to load first"},
         "script": {"type": "string", "description": "Lua; its return value is the result"},
         "seed": {"type": "integer", "description": "default 1"},
         "seconds": {"type": "number", "description": "limit, default 5"},
         "mb": {"type": "integer", "description": "limit, default 256"},
         "trace": {"type": "boolean", "description": "for rs3_lab_query"},
         "model": {"type": "boolean", "description": "use the measured client"},
         "gaps": {"type": "boolean", "description": "list all gaps"}}}},
]
