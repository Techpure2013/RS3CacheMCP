"""Check lua before the client refuses it.

luac is installed, so syntax is free. The rest is the sandbox, which fails at load or first call
rather than at compile.

  syntax          luac -p. Box is 5.5, client is 5.4, so version drift gets through
  os and io       absent. A clock means tick events
  require         takes a path: require("core/store")
  string.format   %d with a float errors in 5.4, and client reads come back float
  pcall           lua errors only. A nil into a native argument takes the client down

  rs3_lua_check   syntax and sandbox problems in a file or folder

LuaLS is the other half, at a minute a run against twenty milliseconds here. The last run is read
instead and can go stale. rs3_lsp runs one.
"""

import io
import os
import re

from rs3api.paths import PLUGINS, TYPINGS
from rs3mcp import addons
from . import lsp
import shutil
import subprocess

# the sandbox withholds these outright
ABSENT = re.compile(r"\b(os|io)\s*\.")
# require takes a path, never a bare name
BARE_REQUIRE = re.compile(r"""require\s*\(\s*["']([^"'/]+)["']\s*\)""")
# %d with anything but an integer literal
FORMAT_D = re.compile(r"""string\.format\s*\(\s*["'][^"']*%d""")
# and nil or, which never yields nil
AND_NIL_OR = re.compile(r"\band\s+(nil|false)\s+or\b")

_CACHE = {}

CHECKS = [
    (ABSENT, "os and io are absent from the sandbox; use the tick events for timing"),
    (BARE_REQUIRE, "require takes a path, not a module name: require(\"core/store\")"),
    (FORMAT_D, "%d errors on a float in 5.4; use %s with tostring for anything read off the "
               "client"),
    (AND_NIL_OR, "a and nil or b is always b, since nil and false never pass through and/or; "
                 "use an if"),
]

PCALL_DIRECT = ("pcall with a client function as its direct callee took the client down three "
                "times on 949-1; give it a lua caller, pcall(function() f(...) end)")
FIELD_GUARD_PROBLEM = ("a pcall around a field read. A table or config lookup yields nil when it is "
                       "missing, so the guard catches nothing and costs a closure on every call. Guard "
                       "what crosses into the client instead: a property write through sol, a stale "
                       "entity handle, a nil into a native argument, Load().")


def _luac():
    return shutil.which("luac") or shutil.which("luac5.4") or shutil.which("luac54")


def _lua_files(target):
    if os.path.isfile(target):
        return [target] if target.endswith(".lua") else []
    out = []
    for dp, dn, fn in os.walk(target):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "dist")]
        out += [os.path.join(dp, f) for f in sorted(fn) if f.endswith(".lua")]
    return out


def _parse_error(text, files):
    """Match luac's `<binary>: <file>:<line>: <msg>` back by name. It stops at the first
    failure and never says which of the rest got that far."""
    m = re.search(r"([^:\s]+\.lua):(\d+):\s*(.*)$", text.strip())
    if not m:
        return {"problem": text.strip()[:120]}
    named = m.group(1).replace("\\", "/")
    hit = next((f for f in files if f.replace("\\", "/").endswith(named)), named)
    return {"file": hit, "line": int(m.group(2)), "problem": m.group(3)}


# batches cut to fit the command line cap
ARGV_ROOM = 28000


def _batches(files, luac):
    """File lists short enough to hand to one process."""
    room = ARGV_ROOM - len(luac) - 8
    out, cur, n = [], [], 0
    for f in files:
        if cur and n + len(f) + 3 > room:
            out.append(cur)
            cur, n = [], 0
        cur.append(f)
        n += len(f) + 3
    if cur:
        out.append(cur)
    return out


def _syntax_all(files, luac):
    """Which files will not compile.

    A batch at a time, then file by file only for a batch that failed. luac stops at its first bad
    one, so a failing batch says nothing about the rest of it."""
    if not files:
        return []
    out = []
    for batch in _batches(files, luac):
        p = subprocess.run([luac, "-p"] + batch, capture_output=True, text=True)
        if p.returncode == 0:
            continue
        found = False
        for path in batch:
            one = subprocess.run([luac, "-p", path], capture_output=True, text=True)
            if one.returncode != 0:
                out.append(_parse_error((one.stderr or one.stdout), [path]))
                found = True
        if not found:
            out.append(_parse_error((p.stderr or p.stdout), batch))
    return out


TEST_DIRS = ("test", "tests", "spec")


def _in_client(path):
    """Whether the client loads this file. A suite in test/ runs under lua54, with os and io.

    Only a file directly in the test folder: test/fixtures/ holds lua standing in for a plugin's,
    which is checked as the client would see it."""
    parent = os.path.basename(os.path.dirname(os.path.normcase(os.path.abspath(path))))
    return parent not in TEST_DIRS


def _client_roots():
    """Top-level client names the ground-truth walk reached, off facts.db.

    A written list would go stale and a derived one from the plugins misses whatever nothing here
    calls yet. This is what a running client answered to."""
    hold = _CACHE.get("roots")
    if hold is not None:
        return hold
    hold = set()
    try:
        import sqlite3
        from rs3api.groundtruth import FACTS
        # connect() would create one, so check first
        if not os.path.exists(FACTS):
            raise OSError(FACTS)
        db = sqlite3.connect(FACTS)
        build = db.execute("select max(build) from facts").fetchone()[0]
        for (api,) in db.execute("select distinct api from facts where build=?", (build,)):
            # an indexed read and a field, by name
            head = api.split(".")[0].split(":")[0].split("#")[0]
            head = head[6:] if head.startswith("index ") else head
            if re.fullmatch(r"[A-Za-z_]\w*", head or ""):
                hold.add(head)
        # Event, when the walk recorded any event
        if db.execute("select 1 from facts where build=? and kind='event' limit 1", (build,)).fetchone():
            hold.add("Event")
        db.close()
    except Exception:
        pass
    _CACHE["roots"] = hold
    return hold


def _pcall_direct():
    """pcall handed a client function itself, which is the shape that crashed.

    Only built when the facts are there to build it from. The closure form is NOT this: every one
    of the 18 lines the old pattern flagged was a closure, and every one of them runs."""
    if "pcallDirect" in _CACHE:
        return _CACHE["pcallDirect"]
    roots = _client_roots()
    got = re.compile(r"pcall\s*\(\s*(?:%s)(?:[.:]\w+)+\s*[,)]"
                     % "|".join(sorted(roots))) if roots else None
    _CACHE["pcallDirect"] = got
    return got


def _sandbox(path):
    """Traps that compile fine and fail in the client."""
    if not _in_client(path):
        return []
    try:
        lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    direct = _pcall_direct()
    out = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        for pattern, why in CHECKS:
            if pattern.search(line):
                out.append({"line": i, "problem": why, "code": stripped[:90]})
                break
        else:
            if direct and direct.search(line):
                out.append({"line": i, "problem": PCALL_DIRECT, "code": stripped[:90]})
    out += _id_names("\n".join(lines))
    out += _id_pairs(lines)
    return out


ID_PAIRS = re.compile(r"for\s+(\w+)\s*,\s*(\w+)\s+in\s+pairs\s*\(\s*id\.([A-Z]\w*)\s*\)")
ID_PAIRS_PROBLEM = ("pairs(id.{table}) yields id, name: {key} is the number and {value} the name, "
                    "the other way round from id.{table}[name]; this loop uses them the wrong way")
# lines of loop body searched
ID_PAIRS_BODY = 20


def _id_pairs(lines):
    """A loop over an id table that treats the name as the number or the id as the name."""
    out = []
    for i, line in enumerate(lines):
        m = ID_PAIRS.search(line)
        if not m or line.strip().startswith("--"):
            continue
        key, value, table = m.groups()
        body = "\n".join(lines[i + 1:i + 1 + ID_PAIRS_BODY])
        numeric = re.search(r"\b%s\s*(?:[<>]=?)\s*-?\d" % re.escape(value), body) \
            or re.search(r"-?\d\s*[<>]=?\s*%s\b" % re.escape(value), body)
        as_name = re.search(r"(?:VarPlayer|Interface|Component)\s*\[\s*%s\s*\]" % re.escape(key), body)
        if numeric or as_name:
            out.append({"line": i + 1, "problem": ID_PAIRS_PROBLEM.format(table=table, key=key, value=value),
                        "code": line.strip()[:90]})
    return out


ID_NAME = ("id.{table}.{name} is not in the client's {table} table, so it reads as nil, and nil reaching a native "
           "property or argument takes the client down; check the name in shared/types/rs3-ids/{table}.d.ts")
ID_READ = re.compile(r"\bid\.([A-Z][A-Za-z]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
ID_ALIAS = re.compile(r"^\s*local\s+([A-Za-z_]\w*)\s*=\s*id\.([A-Z][A-Za-z]*)\s*$", re.M)


def _id_table(table):
    """The names one id table holds, off its typings, or None when there is no typings file."""
    key = ("idtable", table)
    if key not in _CACHE:
        path = os.path.join(TYPINGS, "rs3-ids", table + ".d.ts")
        names = None
        if os.path.exists(path):
            names = set(re.findall(r'^\s*readonly\s+"?([^":\s]+)"?\s*:', io.open(path, encoding="utf-8").read(), re.M))
        _CACHE[key] = names
    return _CACHE[key]


def _id_names(code):
    """id.<Table>.<NAME> reads, direct or through a local alias, that name something the table lacks."""
    from rs3api.lualex import strip
    stripped = strip(code)
    reads = [(m.start(), m.group(1), m.group(2)) for m in ID_READ.finditer(stripped)]
    for alias, table in ID_ALIAS.findall(stripped):
        for m in re.finditer(r"\b%s\.([A-Z_][A-Z0-9_]*)\b" % re.escape(alias), stripped):
            reads.append((m.start(), table, m.group(1)))
    out = []
    for at, table, name in sorted(reads):
        names = _id_table(table)
        # a call, like id.Quest.FromID(i), is not a name
        if stripped[at:].split(name, 1)[1].lstrip().startswith("("):
            continue
        # _4DOSE names match 4DOSE entries
        bare = name[1:] if re.match(r"^_\d", name) else name
        if names is not None and name not in names and bare not in names:
            line = stripped.count("\n", 0, at) + 1
            out.append({"line": line, "problem": ID_NAME.format(table=table, name=name),
                        "code": code.splitlines()[line - 1].strip()[:90]})
    return out



# locals in every plugin here, never client globals
_MODULE_LIKE = ("json", "cmdargs", "comps", "bag", "auth")


def _code_only(text):
    """The file with strings and line comments blanked.

    `here = "auth.lua"` read as a use of auth."""
    out = re.sub(r'"[^"\n]*"', '""', text)
    out = re.sub(r"'[^'\n]*'", "''", out)
    return re.sub(r"--[^\n]*", "", out)


def _missing_requires(text):
    """Modules the file calls but never brought in."""
    text = _code_only(text)
    out = []
    for name in _MODULE_LIKE:
        if not re.search(r"\b" + name + r"\.[A-Za-z_]", text):
            continue
        # a multiple assignment declares it too
        if re.search(r"local\s[^\n=]*\b" + name + r"\b[^\n=]*=", text):
            continue
        out.append({"member": name,
                    "problem": "used but never required. It is a local in every plugin here, "
                               "not a global, so it is nil at runtime"})
    return out


def _member_uses(text):
    """Name.member pairs, and the bare names declared local in this file."""
    text = _code_only(text)
    pairs = set(re.findall(r"\b([A-Z][A-Za-z0-9]*)\.([A-Za-z_][A-Za-z0-9_]*)", text))
    locals_ = set(re.findall(r"local\s+([A-Z][A-Za-z0-9]*)\s*=", text))
    return pairs, locals_


def _id_namespaces():
    """Config types: id tables, not API surface.

    Obj.ELDER_LOGS is one of tens of thousands of constants. Ask the database. Inferring it from
    reuse counts did not separate them."""
    hold = _CACHE.setdefault("idns", set())
    if hold:
        return hold
    try:
        from rs3mcp.common import connect
        rows = connect().execute("select distinct type from config").fetchall()
    except Exception:
        return hold
    for r in rows:
        t = str(r[0])
        hold.add(t)
        hold.add(t.capitalize())
        # the client names them in the singular
        if t.endswith("s"):
            hold.add(t[:-1].capitalize())
    hold.update({"Obj", "Loc", "Npc", "NPC", "Enum", "Struct", "Seq", "SpotAnim", "Param",
                 "Category", "AlignMode", "Hook", "Achievement", "MapElement",
                 "Sprite", "Component"})
    return hold


def _corpus():
    """(member -> files using it, the set of client globals).

    A client global: used with a member in two or more plugins, never declared local. Derived, so
    there is no list to maintain."""
    hold = _CACHE.setdefault("api", {})
    if hold:
        return hold["members"], hold["globals"]
    members, owners, declared = {}, {}, set()
    if PLUGINS and os.path.isdir(PLUGINS):
        for dp, dn, fn in os.walk(PLUGINS):
            # a test folder stubs the client with locals
            dn[:] = [d for d in dn if not d.startswith(".") and d != "node_modules" and d not in TEST_DIRS]
            plugin = os.path.relpath(dp, PLUGINS).split(os.sep)[0]
            for f in fn:
                if not f.endswith(".lua"):
                    continue
                try:
                    src = io.open(os.path.join(dp, f), encoding="utf-8",
                                  errors="replace").read()
                except OSError:
                    continue
                pairs, loc = _member_uses(src)
                declared |= loc
                for base, member in pairs:
                    members[base + "." + member] = members.get(base + "." + member, 0) + 1
                    owners.setdefault(base, set()).add(plugin)
    globals_ = {b for b, ps in owners.items()
                if len(ps) >= 2 and b not in declared and b not in _id_namespaces()}
    hold["members"], hold["globals"] = members, globals_
    hold["typed"] = _typed_members()
    return members, globals_


def _typed_members():
    """Every identifier the typings declare, flat.

    A second opinion. They miss things that ship, so this only clears a member, never flags one."""
    out = set()
    root = os.path.join(TYPINGS, "rs-client-api")
    if not os.path.isdir(root):
        return out
    for f in os.listdir(root):
        if not f.endswith(".d.ts"):
            continue
        try:
            src = io.open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        out |= set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", src))
    return out


def _documented():
    """Every Name.member pair the docs mirror spells out.

    The only source that knew Event.InventoryUpdated. Pairs, not bare names: bare matching cleared
    Event.Tick, which does not exist. The docs talk about ticks everywhere."""
    hold = _CACHE.setdefault("documented", set())
    if hold:
        return hold
    try:
        pages = addons.first("doc_pages")
        for text in (pages() if pages else {}).values():
            hold |= set(re.findall(r"\b([A-Z][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\b", text))
    except Exception:
        pass
    return hold


def _progress_values(files):
    """Observed against keyed, for the checked files that are quest classes.

    Only the ones with something to say."""
    observed = addons.first("quest_progress")
    if not observed:
        return None
    out = []
    for path in files:
        seen = observed(path)
        if seen is not None and (seen.get("unconfirmed") or seen.get("observedButNotKeyed")):
            out.append(seen)
    return out or None


def _quest_uses(files):
    """Quest classes naming something src/ does not define. Silent when they all behave."""
    uses = addons.first("quest_uses")
    if not uses:
        return None
    out = [q for q in (uses(p) for p in files) if q]
    return out or None


def _unknown_api(path):
    """API members this file uses that nothing else does, plus modules it forgot to require."""
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    out = _missing_requires(text)
    members, globals_ = _corpus()
    if not globals_:
        return out
    pairs, loc = _member_uses(text)
    for base, member in sorted(pairs):
        if base not in globals_ or base in loc:
            continue
        name = base + "." + member
        # the typings or the docs vouching is enough
        if member in _CACHE.get("api", {}).get("typed", ()) or name in _documented():
            continue
        # a corpus file counts its own use
        if members.get(name, 0) - 1 <= 0:
            out.append({"member": name,
                        "problem": "no other installed plugin uses this. Either it is new, or it "
                                   "does not exist and the client will fail at load"})
    return out



def _reads_as(files, root):
    """Comments stacking three register tells, and where.

    Not a verdict. Under the sixty rs3_comment_style needs there is no rate worth quoting, but
    seeing the lines is the only thing that has changed what gets written."""
    if not addons.first("register_tells"):
        return None

    hits, seen = [], 0
    for path in files:
        try:
            src = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        buf, start = [], 0
        for n, ln in enumerate(src.split("\n"), 1):
            s = ln.strip()
            if s.startswith("--") and not s.startswith("---@"):
                if not buf:
                    start = n
                t = s.lstrip("-").strip()
                if t:
                    buf.append(t)
                continue
            if buf:
                _weigh(" ".join(buf), rel, start, hits)
                seen += 1
            buf = []
        if buf:
            _weigh(" ".join(buf), rel, start, hits)
            seen += 1

    if not hits:
        return None
    return {"comments": seen, "stacked": len(hits), "worst": hits[:6],
            "note": "comments carrying three register tells at once. Counts, not a verdict -- a "
                    "file is too small to grade. rs3_lua_write has the rules and "
                    "rs3_comment_style grades a whole plugin."}


def _weigh(text, rel, line, hits):
    BAND, TELLS, _fires = addons.first("register_tells")

    if not (BAND[0] <= len(text.split()) <= BAND[1]):
        return
    on = [n for n, pat, _w, _f in TELLS if not n.startswith("no ") and _fires(n, pat, text)]
    if len(on) >= 3:
        hits.append({"at": "%s:%d" % (rel, line), "tells": on, "comment": text[:120]})



# a guard on something already nil-safe
_FIELD_GUARD = re.compile(
    r"pcall\(\s*function\s*\(\s*\)\s*"
    r"(?:local\s+)?[A-Za-z_][\w.\[\]\"']*\s*=\s*"
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[[^\]()]*\])+\s*"
    r"end\s*\)")


def _iteration(path):
    """Where the iterator and the data disagree.

    ipairs stops at the first hole and never sees a string key; pairs sees everything in no
    promised order. Which is right depends on what the table holds, so a table written one way and
    walked the other is the bug: a price cache keyed by tostring(id) walked with ipairs comes back
    empty, and a list with a slot set to nil loses everything past it.
    """
    try:
        text = _code_only(io.open(path, encoding="utf-8", errors="replace").read())
    except OSError:
        return []
    out = []
    for loop in re.finditer(r"\bfor\s+([A-Za-z_]\w*)\s*(?:,\s*[A-Za-z_]\w*\s*)?in\s+ipairs\s*\(\s*([A-Za-z_][\w.]*)\s*\)", text):
        index, name = loop.group(1), loop.group(2)
        # the whole dotted name, not its last part
        bare = re.escape(name)
        # clearing the loop's slot or popping stays dense
        safe = (index, "#" + name.split(".")[-1], "#" + name)
        holes = [m for m in re.finditer(r"\b%s\s*\[([^\]]+)\]\s*=\s*nil\b" % bare, text)
                 if m.group(1).strip() not in safe]
        strings = re.search(r"\b%s\s*\[\s*(?:tostring\s*\(|[\"'])" % bare, text)
        if holes:
            out.append({"name": name, "problem": "walked with ipairs, but a slot is set to nil "
                                                 "somewhere; ipairs stops at that hole"})
        elif strings:
            out.append({"name": name, "problem": "walked with ipairs, but written with a string "
                                                 "key; ipairs never reaches those"})
    return out


def _field_guards(files, root):
    """pcall closures wrapping a bare field read, and where.

    Zero across their 25,000 lines, one per thousand in mine. A code tell, so the comment measures
    never see it."""
    out = []
    for path in files:
        try:
            src = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        # line numbers need raw text, so per statement
        lines = src.split("\n")
        for i in range(len(lines)):
            window = " ".join(x.strip() for x in lines[i:i + 4])
            if _FIELD_GUARD.match(window.strip()):
                out.append({"at": "%s:%d" % (rel, i + 1),
                            "code": " ".join(window.split())[:90]})
    if not out:
        return None
    return {"found": len(out), "where": out[:10], "problem": FIELD_GUARD_PROBLEM,
            "measured": "zero of these across 25,000 lines by the three lua authors here"}



def _purpose_of(files, root):
    """The comment purpose mix for what was just checked.

    Here because this runs on every write and rs3_comment_purpose has to be remembered."""
    offered = addons.first("comment_purpose")
    if not offered:
        return None
    blocks, purpose = offered
    got = []
    for p in files:
        for n, t in blocks(p):
            got.append((os.path.relpath(p, root).replace("\\", "/"), n, t, purpose(t)))
    if len(got) < 8:
        return None
    total = float(len(got))
    share = {k: 100.0 * sum(1 for g in got if g[3] == k) / total
             for k in ("label", "fact", "trap", "rationale")}
    arguing = [{"at": "%s:%d" % (f, n), "comment": t[:110]}
               for f, n, t, k in got if k == "rationale"]
    if share["rationale"] <= 20.0 and share["label"] >= 25.0:
        return None
    return {"comments": int(total),
            "labels": "%.0f%%" % share["label"],
            "rationale": "%.0f%%" % share["rationale"],
            "human": "labels 27% and up, rationale under 28%, from the three lua authors here",
            "arguing": arguing[:6],
            "problem": "most of these argue for the code where a person would name it. Counts, "
                       "not a verdict, on something this size."}


def tool_lua_check(args):
    target = str(args.get("path") or "").strip()
    if not target:
        raise ValueError("give a path to a lua file or a folder")
    if not os.path.exists(target):
        raise ValueError("no such path: " + target)

    luac = _luac()
    files = _lua_files(target)
    if not files:
        return {"checked": 0, "note": "no lua files under " + target}

    root = target if os.path.isdir(target) else os.path.dirname(target)
    broken = _syntax_all(files, luac) if luac else []
    for b in broken:
        if b.get("file"):
            b["file"] = os.path.relpath(b["file"], root).replace("\\", "/")
    bad = {b.get("file") for b in broken}

    # what the live client did on these
    from . import factcheck
    facts = factcheck.load()

    from . import callshape
    warned, clean, unknown, measured, iteration, shapes = [], 0, [], [], [], []
    for path in files:
        rel = os.path.relpath(path, root).replace("\\", "/")
        # typings checks are for client lua
        if _in_client(path):
            shapes += [dict(g, file=rel) for g in callshape.check(path)]
        soft = _sandbox(path)
        odd = _unknown_api(path)
        measured += factcheck.check(path, facts, root) if facts else []
        for row in _iteration(path):
            iteration.append(dict(row, file=rel))
        if odd:
            unknown.append({"file": rel, "members": odd[:8]})
        if soft:
            warned.append({"file": rel, "found": soft[:8]})
        elif rel not in bad and not any(s["file"] == rel for s in shapes):
            clean += 1

    # without luac nothing was compiled, so no count of what compiles
    out = {"checked": len(files), "compiles": len(files) - len(broken) if luac else None, "clean": clean,
           "willNotCompile": broken[:20],
           "sandbox": warned[:20],
           # calls read against their declarations
           "callShape": shapes[:20] or None,
           "unknownApi": unknown[:20] or None,
           # the client's own measurements, each quoting its run
           "measured": measured[:20] or None,
           # the iterator and the data have to agree
           "iteration": iteration[:20] or None,
           "measuredFrom": facts["build"] if facts and measured else None,
           # written where the work already passes
           "readsAs": _reads_as(files, root),
           # a code tell, which everything here passed
           "guardsNothing": _field_guards(files, root),
           # what the comments are for, caught by reading
           "commentPurpose": _purpose_of(files, root),
           # luac compiles, this says the fields exist
           "languageServer": lsp.note_for(target),
           # a value nobody watched looks like one watched
           "progressValues": _progress_values(files),
           # and that it only names what Quest_Helper has
           "usesOnlyWhatExists": _quest_uses(files)}
    if not luac:
        out["note"] = ("luac is not on PATH, so only the sandbox checks ran. Install it and the "
                       "syntax half costs nothing.")
    else:
        out["note"] = ("luac here is %s. The client targets 5.4 and this box runs a later lua, so "
                       "a file that compiles can still be refused for version reasons; that is "
                       "the one thing this cannot tell you." % os.path.basename(luac))
    return out


TOOLS = [
    {"name": "rs3_lua_check", "fn": tool_lua_check, "examples": [{"path": "RS3Plugins/my-plugin/main.lua"}],
     "description": "Compile a lua file or folder with luac and flag the traps that compile fine "
                    "and fail in the client. Run it before deploying.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "a .lua file or a folder to walk"}}}},
]


if __name__ == "__main__":
    # exit 1 on a finding, so shells hear
    import json
    import sys
    if len(sys.argv) < 2:
        print("python -m rs3api.luacheck <file or folder>")
        sys.exit(2)
    got = tool_lua_check({"path": sys.argv[1]})
    print(json.dumps({k: v for k, v in got.items() if v not in (None, [], {})}, indent=1, default=str))
    sys.exit(1 if got.get("willNotCompile") or got.get("sandbox") or got.get("callShape") else 0)
