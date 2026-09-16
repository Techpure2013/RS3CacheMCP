"""
MCP server over the RS3 game data on this machine, so it can be asked a question instead of having a
one off script written for it every time.

Any MCP app can run it as the "rs3cache" server; `rs3 setup` writes the config for the ones it
knows. It speaks JSON-RPC on stdin and stdout, which is all MCP is, so there is nothing to install
and nothing to keep up to date. People use the same tools through `rs3` (rs3mcp/cli.py).

Read only by construction. The cache database is opened mode=ro, sql that is not a single SELECT
is refused, and no tool anywhere writes to anything.

Tool families live in rs3mcp/, one module each, and every module exposes a TOOLS list. Adding a
family is an import here and nothing else.

  cache    the decoded cache: its shape, sql over it, one config resolved, sprites
  families config families: which id is the real one
  trace    everything connected to one config, both directions
  script   the decompiled clientscripts, which settle what the cache cannot
  vars     varbits decoded to the base var, mask and shift they are packed into
  dbtable  the game's data tables and the rows in them
  iface    interfaces and their components, from a packed id or the text on screen
  wiki     the rs3 wiki, cached locally: drops, infoboxes, anything a person wrote
  drift    every number written here against the data

Tools that only mean something in one workspace live in add-on packages beside rs3mcp and are added
to these at start. See rs3mcp/addons.py.

Run standalone to sanity check it:  python rs3-mcp.py --selftest
Known-answer tests only:            python rs3-mcp.py --test
Record every tool's answer:         python rs3-mcp.py --golden
Diff against it after a change:     python rs3-mcp.py --diff
Or call any tool straight from a shell:
  python rs3-mcp.py rs3_lookup kind=loc name=HOUSE_STATUE_1
  python rs3-mcp.py query sql="select id,display from config where type='items' limit 5"
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rs3mcp import addons, common
from rs3mcp import howto
from rs3mcp import resources
from rs3mcp import (cache, families, cachediff, drift, scriptrefs, dbtable, iface, script, trace, vars,
                    wiki)

PROTOCOL = "2025-06-18"

FAMILIES = [cache, families, trace, cachediff, scriptrefs, script, vars, dbtable, iface, wiki, drift]
FAMILIES += addons.families()

TOOLS = []
for _mod in FAMILIES:
    TOOLS.extend(_mod.TOOLS)

BY_NAME = {t["name"]: t for t in TOOLS}

# Toolsets, after github-mcp-server, which groups its tools so a client lists only the ones it
# needs. The listing is paid once a session whether or not a tool is called, and 49 of them is
# 8,800 tokens.
#
# Derived from the module a tool is registered by, never a written list: a hand kept mapping is one
# more copy to go stale, and a new tool would default to belonging nowhere.
SETS = {
    "cache": ("cache", "families", "trace", "cachediff"),
    "docs": ("wiki",),
    "scripts": ("script", "scriptrefs"),
    "game": ("vars", "iface", "dbtable"),
    "meta": ("howto", "drift"),
}
for _extra in addons.every("toolsets"):
    for _name, _mods in _extra.items():
        SETS[_name] = tuple(SETS.get(_name, ())) + tuple(_mods)
IN_SET = {mod: name for name, mods in SETS.items() for mod in mods}


def toolset_of(tool):
    """Which set a tool belongs to, from the module that registered it.

    A module nobody placed lands in meta and is listed, rather than vanishing from the listing
    because of an oversight. test_toolsets is what catches the oversight.
    """
    mod = (tool.get("fn").__module__ or "").rsplit(".", 1)[-1]
    return IN_SET.get(mod, "meta")


TOOLSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rs3-toolsets")


def wanted_sets():
    """Sets to list. Unset means all of them, so nothing changes by default.

    A file beside the server before the environment, because the sets change per task and the
    environment is fixed when the client launches us. One line to edit, then restart.
    """
    raw = ""
    try:
        raw = io.open(TOOLSET_FILE, encoding="utf-8").read()
    except OSError:
        raw = os.environ.get("RS3_TOOLSETS") or ""
    raw = " ".join(l for l in raw.splitlines() if not l.strip().startswith("#")).strip()
    if not raw or raw.lower() == "all":
        return None
    return {x.strip().lower() for x in raw.replace(",", " ").split() if x.strip()}


# Tool annotations, from the 2025-06-18 schema. Almost everything here reads the cache and
# answers. The ones that write a file, run a process or reach the network are named; the rest take
# the default below. A wrong readOnlyHint is a safety claim that is false, hence a list.
SIDE_EFFECTS = {
    "rs3_wiki": {"readOnlyHint": True, "openWorldHint": True},
}
# an add-on names its own writers, since it is the one that knows
for _extra in addons.every("side_effects"):
    SIDE_EFFECTS.update(_extra)
READS_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def listing():
    """The tools/list payload.

    One function. The budget check used to name the same three keys in a second place and could
    drift from what was sent.
    """
    keep = wanted_sets()
    out = []
    for t in TOOLS:
        if keep is not None and toolset_of(t) not in keep:
            continue
        rec = {k: t[k] for k in ("name", "description", "inputSchema")}
        rec["annotations"] = SIDE_EFFECTS.get(t["name"], READS_ONLY)
        out.append(rec)
    return out


# An answer written to a file instead of into the conversation. Artifact's read_db does this with
# out_dir and Bash caps its output outright; both say the same thing, that a big answer does not
# have to be paid for in context to be useful.
#
# Handled here rather than in 49 schemas: any tool takes it, and the routing guide says so once.
PICK_ARG = "pick"


def pick(got, path):
    """One part of an answer, by dotted path.

    Grafana pairs a full dashboard tool with one that takes a JSONPath, because most questions want
    a corner of the document. Same idea, dispatcher side, so every tool has it: `pick=counts.npcs`
    is a number rather than fourteen thousand characters.

    A segment that is all digits indexes a list. Anything the path does not reach comes back as a
    miss naming what was there, which is more use than an empty answer.
    """
    here, walked = got, []
    for seg in str(path).split("."):
        walked.append(seg)
        if isinstance(here, dict) and seg in here:
            here = here[seg]
        elif isinstance(here, list) and seg.lstrip("-").isdigit():
            try:
                here = here[int(seg)]
            except IndexError:
                return {"pick": path, "miss": "%s has %d entries" % (".".join(walked[:-1]),
                                                                     len(here))}
        else:
            where = sorted(here) if isinstance(here, dict) else type(here).__name__
            return {"pick": path, "miss": "nothing at %s" % ".".join(walked), "thereIs": where}
    return {"pick": path, "value": here}


SPILL_ARG = "outDir"


def spill(name, args, got):
    """Write the answer beside the caller and hand back a receipt.

    The receipt carries the shape, so a reader knows whether to open the file at all: the top level
    keys, and the length of any list among them.
    """
    where = os.path.abspath(str(args.get(SPILL_ARG)))
    os.makedirs(where, exist_ok=True)
    path = os.path.join(where, "%s.json" % name)
    body = json.dumps(got, indent=1, default=str)
    io.open(path, "w", encoding="utf-8", newline="\n").write(body)

    rec = {"writtenTo": path, "characters": len(body)}
    if isinstance(got, dict):
        rec["keys"] = sorted(got)
        counts = {k: len(v) for k, v in got.items() if isinstance(v, (list, dict))}
        if counts:
            rec["lengths"] = counts
    elif isinstance(got, list):
        rec["entries"] = len(got)
    rec["note"] = "the answer is on disk, not here. Read the file for the parts you need."
    return rec


# Everything on the wire goes out compact. The default separators spend ", " and ": " on every
# key, which is 26% of a typical answer, and nothing reads the raw stream by eye.
WIRE = (",", ":")


def lean(o):
    """The answer without its empty keys.

    A null says the same as an absent key to anything reading this, and costs bytes on every call.
    Empty lists stay: `unresolved: []` is a finding, `unresolved: null` is not.
    """
    if isinstance(o, dict):
        return {k: lean(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [lean(x) for x in o]
    return o


class NoSuchResource(LookupError):
    """A uri naming nothing. -32002 in the spec, and not the same as a missing method.

    Subclasses LookupError so it has to be caught before that one, which is why it sits above.
    """


def handle(req):
    method = req.get("method")
    if method == "initialize":
        ver = (req.get("params") or {}).get("protocolVersion") or PROTOCOL
        return {"protocolVersion": ver,
                # resources declares neither subscribe nor listChanged: nothing here changes
                # under a running session except the files, and we do not watch them.
                "capabilities": {"tools": {}, "resources": {}},
                "instructions": howto.instructions(),
                "serverInfo": {"name": "rs3cache", "version": "2.0.0"}}
    if method == "tools/list":
        return {"tools": listing()}
    if method == "resources/list":
        return {"resources": resources.listing()}
    if method == "resources/templates/list":
        return {"resourceTemplates": resources.templates()}
    if method == "resources/read":
        uri = (req.get("params") or {}).get("uri") or ""
        got = resources.read(uri)
        if got is None:
            raise NoSuchResource(uri)
        body, mime = got
        return {"contents": [{"uri": uri, "mimeType": mime, "text": body}]}
    if method == "tools/call":
        p = req.get("params") or {}
        name = p.get("name")
        t = BY_NAME.get(name)
        if t is None:
            return {"content": [{"type": "text", "text": "no tool called " + str(name)}],
                    "isError": True}
        try:
            call = p.get("arguments") or {}
            ours = (SPILL_ARG, PICK_ARG)
            got = lean(t["fn"]({k: v for k, v in call.items() if k not in ours}))
            if call.get(PICK_ARG):
                got = pick(got, call[PICK_ARG])
            if call.get(SPILL_ARG):
                got = spill(name, call, got)
            text = json.dumps(got, separators=WIRE, default=str)
        except Exception as e:
            return {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True}
        # No structuredContent. The spec makes it optional and it is the same bytes a second
        # time: measured at +11,904 tokens over ten real answers, for a copy nothing here reads.
        return {"content": [{"type": "text", "text": text}]}
    raise LookupError(method)


def from_shell(argv):
    """Any tool by name, arguments as key=value. The rs3_ prefix is optional because nobody wants
    to type it, and a bare number or a bare word is taken as the first required argument so the
    common lookups stay short."""
    name = argv[0]
    if name not in BY_NAME and "rs3_" + name in BY_NAME:
        name = "rs3_" + name
    t = BY_NAME.get(name)
    if t is None:
        print("tools: " + ", ".join(sorted(BY_NAME)))
        return 1

    required = (t["inputSchema"].get("required") or [])
    args, loose = {}, []
    for a in argv[1:]:
        if "=" in a and not a.startswith("="):
            k, v = a.split("=", 1)
            args[k] = int(v) if v.lstrip("-").isdigit() else v
        else:
            loose.append(a)
    if loose:
        for k in required:
            if k not in args:
                args[k] = " ".join(loose)
                break

    try:
        print(json.dumps(t["fn"](args), indent=1, default=str))
    except Exception as e:
        print("error: %s" % e)
        # "need id" on its own sends you to the source to find out what else it wanted
        props = t["inputSchema"].get("properties") or {}
        if required:
            print("%s takes: %s" % (name, ", ".join(
                "%s=%s" % (k, "required" if k in required else
                           (props.get(k, {}).get("description") or "").split(",")[0][:34] or "-")
                for k in list(required) + [k for k in props if k not in required])))
        return 1
    return 0


def selftest():
    checks = [
        ("rs3_schema", {}, lambda r: "%d tables, %d config types"
                                     % (len(r["tables"]), len(r["config_types"]))),
        ("rs3_query", {"sql": "select id,name,display from config where type='items' "
                              "and display='Coins' order by id", "limit": 3},
         lambda r: "%d rows" % len(r["rows"])),
        ("rs3_sprite", {"name": "necrosis"}, lambda r: "%d sprites" % r["found"]),
        ("rs3_trace", {"kind": "obj", "id": "2"},
         lambda r: "%s: %d params, %d refs in" % (r.get("name"), len(r.get("carries") or []),
                                                  len(r.get("referencedBy") or []))),
        ("rs3_family", {"kind": "loc", "id": "545"},
         lambda r: "%s of %d parent(s)" % (r.get("role"), len(r.get("parents") or []))),
        ("rs3_script", {"pattern": "IF_SETHIDE", "limit": 5},
         lambda r: "%d hits over %d files" % (r["found"], r["filesScanned"])),
        ("rs3_script_read", {"script": "0"}, lambda r: "script 0 is %s" % r.get("tag")),
        ("rs3_wiki", {}, lambda r: "%d pages cached" % r["cachedPages"]),
        ("rs3_iface", {"id": "18612224"},
         lambda r: "packed splits to %s in %s" % (r["at"], r["name"])),
        ("rs3_var", {"id": "-60893"},
         lambda r: "%s is %s of %s" % (r["name"], r["holds"], r["base"]["name"])),
    ]
    for extra in addons.every("selftest"):
        checks += extra()
    from rs3mcp.test import workspace
    bad = 0
    for name, args, summarise, *need in checks:
        # a check for an add-on's tool, with that add-on not loaded
        if name not in BY_NAME:
            continue
        gone = workspace.missing(*need)
        if gone:
            print("  %-16s skipped, needs %s" % (name, "; ".join(gone)))
            continue
        try:
            print("  %-16s %s" % (name, summarise(BY_NAME[name]["fn"](args))))
        except Exception as e:
            print("  %-16s FAILED: %s" % (name, e))
            bad += 1
    print("\n%d tools registered across %d families%s"
          % (len(TOOLS), len(FAMILIES), ", %d broken" % bad if bad else ""))
    return 1 if bad else 0


def main():
    if "--golden" in sys.argv:
        from rs3mcp.test import golden
        sys.exit(golden.record())
    if "--diff" in sys.argv:
        from rs3mcp.test import golden
        sys.exit(golden.diff("-v" in sys.argv))
    if "--test" in sys.argv:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "rs3mcp"))
        from rs3mcp.test import correctness
        sys.exit(correctness.run())
    if "--selftest" in sys.argv:
        rc = selftest()
        from rs3mcp.test import correctness
        print()
        rc = correctness.run() or rc
        sys.exit(rc)
    if len(sys.argv) > 1:
        sys.exit(from_shell(sys.argv[1:]))

    from rs3mcp import bypass, journal, poollog
    journal.serve([{"name": t["name"], "set": toolset_of(t)} for t in TOOLS], bypass.advise)
    # apps other than Claude Code leave no transcript, so the pool reads what this server saw
    log = poollog.Log()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        # notifications carry no id and must not be answered
        if "id" not in req:
            continue
        params = req.get("params") or {}
        if req.get("method") == "initialize":
            log.start(params.get("clientInfo"))
        call_id = log.call(params.get("name"), params.get("arguments")) \
            if req.get("method") == "tools/call" else None
        try:
            body = {"jsonrpc": "2.0", "id": req["id"], "result": handle(req)}
            if call_id:
                got = body["result"]
                log.result(call_id, "".join(c.get("text", "") for c in got.get("content") or []),
                           got.get("isError"))
        except NoSuchResource as e:
            body = {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32002, "message": "Resource not found",
                              "data": {"uri": str(e)}}}
        except LookupError as e:
            body = {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32601, "message": "method not found: %s" % e}}
        except Exception as e:
            body = {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32603, "message": str(e)}}
        if call_id and "error" in body:
            log.result(call_id, body["error"]["message"], True)
        sys.stdout.write(json.dumps(body, separators=WIRE) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
