"""Lua typings for the client api, generated from the public docs mirror (docsfetch.py).

Reads data/api-docs and writes LuaLS definition files, one per namespace, to data/api-typings:
signatures become functions with ---@param and ---@return, property tables ---@field, Name|Value
tables ---@enum, and event payload tables a payload class for each Event.<Name>.

Members the docs leave out go in data/api-typings/extras/*.lua, ---@meta files of their own that
LuaLS merges with these; this never writes there, so a refresh can't lose them.

Every run keeps a snapshot of each member's line and prints what was added, removed or changed
since the last one.

  python rs3api/docstypes.py           regenerate and report the changes
"""

import glob
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rs3mcp.common import ROOT  # noqa: E402  run as a script, rs3cache isn't on the path

DOCS = os.path.join(ROOT, "api-docs")
OUT = os.path.join(ROOT, "api-typings")
SNAPSHOT = os.path.join(OUT, ".members.json")
MODEL = os.path.join(OUT, ".model.json")

DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
WORD = re.compile(r"^[A-Za-z_]\w*$")
SIGNATURE = re.compile(r"^function\s+([\w.]+)([.:])(\w+)\s*\((.*?)\)\s*(?:->\s*(.+))?$")
# a global: function log(format : String, args : ...), owner _G
GLOBAL_SIG = re.compile(r"^function\s+()()(\w+)\s*\((.*?)\)\s*(?:->\s*(.+))?$")
# a page's own section names, never a type (## Inv is the type Inv)
SECTIONS = {"overview", "functions", "properties", "methods", "examples", "example", "enumerations",
            "constructors", "types", "resources", "objectives", "events", "fields", "members", "notes",
            "usage", "info", "introduction", "summary", "requirements", "setup", "testing"}
# a ui page's overview: ui.Component -> ui.ComponentCollection -> ui.ModelGroup
CHAIN = re.compile(r"^[A-Za-z_][\w.]*(?: -> [A-Za-z_][\w.]*)+$")

PRIMITIVE = {
    "integer": "integer", "int": "integer", "uint": "integer", "long": "integer",
    "int8": "integer", "int16": "integer", "int32": "integer", "int64": "integer",
    "uint8": "integer", "uint16": "integer", "uint32": "integer", "uint64": "integer",
    "float": "number", "double": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean", "string": "string", "nil": "nil",
    "function": "function", "table": "table", "any": "any", "void": "nil",
}


def lua_type(t):
    """A docs type as LuaLS writes it: `Int?` is `integer?`, `A | B` stays a union."""
    t = t.strip().strip("`")
    if not t:
        return "any"
    parts = [p.strip() for p in t.split("|")]
    out = []
    for p in parts:
        optional = p.endswith("?")
        core = p.rstrip("?").strip()
        core = PRIMITIVE.get(core.lower(), core)
        out.append(core + ("?" if optional else ""))
    return "|".join(out)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_page(text, members, owners):
    """Adds the page's members to `members`: (owner, kind, name) -> details."""
    lines = text.splitlines()
    typed = {m.group(1) for m in (SIGNATURE.match(s) for s in signatures(lines)) if m}
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
    short = {title.rsplit(".", 1)[1]: title} if DOTTED.match(title) else {}
    owner = title if DOTTED.match(title) or WORD.match(title) else None
    page_owner = owner
    section = ""
    event = None
    # the level of the Methods or Functions heading we're under, else 0
    pending, fns = None, 0
    code = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            if code is None:
                code = []
            else:
                for c in signatures(code):
                    sig = SIGNATURE.match(c)
                    if sig:
                        add_signature(sig, members, owners, short)
                        continue
                    glob_sig = GLOBAL_SIG.match(c)
                    if glob_sig:
                        add_signature(glob_sig, members, owners)
                code = None
            i += 1
            continue
        if code is not None:
            code.append(line)
            i += 1
            continue
        if CHAIN.match(line.strip()):
            chain = line.strip().split(" -> ")
            for parent, child in zip(chain, chain[1:]):
                owners.setdefault(child, "class")
                members[(child, "extends", "")] = {"parent": parent}
        head = re.match(r"^(#{2,6})\s+(.*)$", line)
        if head:
            level, name = len(head.group(1)), head.group(2).strip()
            if level == 2:
                section, event, owner = name.lower(), None, page_owner
            if name.lower() in ("methods", "functions"):
                fns = level
            elif fns and level <= fns:
                fns = 0
            pending = name if fns and level > fns and WORD.match(name) and name != "Constructor" else None
            if DOTTED.match(name) or (level == 2 and WORD.match(name) and name.lower() not in SECTIONS):
                # ## RemoveRoof is World.RemoveRoof when the page's signatures say so
                owner = next((t for t in sorted(typed) if t.endswith("." + name)), name)
                owners.setdefault(owner, "class")
            elif section == "supported events" and level == 3 and WORD.match(name):
                event = name
                # an event with no payload table still exists (ServerLogic)
                members.setdefault(("Event", "event", name), {"payload": None})
                owners.setdefault("Event", "class")
            elif section == "enumerations" and WORD.match(name) and name[:1].isupper():
                owner = name
            elif level == 2 and " " in name:
                # "## Plugin Resources" is PluginResource when the page's signatures say so
                joined = "".join(w[:1].upper() + w[1:] for w in name.split())
                for cand in (joined, joined[:-1] if joined.endswith("s") else None):
                    if cand in typed:
                        owner = cand
            i += 1
            continue
        if line.startswith("| ") and i + 1 < len(lines) and lines[i + 1].startswith("| ---"):
            header = [h.lower() for h in cells(line)]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            add_table(header, rows, owner, event, members, owners)
            # a heading and its parameters with no signature (#### Remove): a function by that name
            mine = [k[1] for k, d in members.items() if k[0] == owner and not d.get("fromHeading")]
            if (pending and owner in typed and header[:2] == ["parameter", "type"]
                    and "function" in mine and "method" not in mine
                    and not any((owner, k, pending) in members for k in ("function", "method"))):
                args = [(r[0].rstrip("?"), lua_type(r[1])) for r in rows if len(r) >= 2 and WORD.match(r[0].rstrip("?"))]
                kind = "method" if owners.get(owner) == "class" and "." not in owner else "function"
                members[(owner, kind, pending)] = {"variants": [{"args": args, "returns": []}], "fromHeading": True}
            pending = None
            continue
        i += 1


def signatures(code):
    """A code block's signature lines, one each: a signature that wraps is joined until its
    brackets close; `PluginResource<T>` is PluginResource, and `game.options.<option name>` is
    the class every option is, game.Option."""
    out, cur = [], None
    for line in code:
        s = line.strip()
        if cur is None and s.startswith("function "):
            cur = s
        elif cur is not None:
            cur += " " + s
        if cur is not None and cur.count("(") <= cur.count(")"):
            cur = cur.replace("game.options.<option name>", "game.Option")
            out.append(re.sub(r"\(\s+", "(", re.sub(r"<\w+>", "", cur)))
            cur = None
    return out


def add_signature(sig, members, owners, short=None):
    owner, dot, name, params, ret = sig.groups()
    # `Component:SetPos` on the ui.Component page is ui.Component's
    owner = (short or {}).get(owner, owner) or "_G"
    dot = dot or "."
    args = []
    for p in [p.strip() for p in params.split(",") if p.strip()]:
        if ":" not in p:
            # an untyped parameter is example code (a plugin's own lib:ReportAndIncrementValue)
            return
        n, t = p.split(":", 1)
        # `args : ...` is Lua's varargs
        args.append(("...", "any") if t.strip() == "..." else (n.strip(), lua_type(t)))
    owners.setdefault(owner, "table" if dot == "." else "class")
    rets = [lua_type(r) for r in ret.split(",")] if ret else []
    # each overload its own definition: LuaLS reads them as overloads
    variants = members.setdefault((owner, "method" if dot == ":" else "function", name), {"variants": []})["variants"]
    if {"args": args, "returns": rets} not in variants:
        variants.append({"args": args, "returns": rets})


def add_table(header, rows, owner, event, members, owners):
    if header[:2] == ["payload variable", "type"] and event:
        cls = "Event.%sPayload" % event
        owners.setdefault(cls, "class")
        members[("Event", "event", event)] = {"payload": cls}
        for r in rows:
            if len(r) >= 2 and WORD.match(r[0]):
                members[(cls, "field", r[0])] = {"type": lua_type(r[1]), "doc": r[-1]}
        return
    if not owner:
        return
    if header[:2] == ["name", "value"]:
        owners[owner] = "enum"
        for r in rows:
            if len(r) >= 2 and WORD.match(r[0]):
                members[(owner, "enum", r[0])] = {"value": r[1], "doc": r[2] if len(r) > 2 else ""}
    elif header[:2] == ["value", "description"]:
        # names without their numbers (ui.TriggerMode): known names, no invented values
        owners[owner] = "enum"
        for r in rows:
            if len(r) >= 2 and WORD.match(r[0]):
                members[(owner, "enum", r[0])] = {"value": "", "doc": r[1]}
    elif header[:2] in (["name", "type"], ["property", "type"]):
        owners.setdefault(owner, "class")
        for r in rows:
            if len(r) >= 2 and WORD.match(r[0]):
                members[(owner, "field", r[0])] = {"type": lua_type(r[1]), "doc": r[-1]}


def render(owners, members):
    """The definition files, one per namespace: `ui.Component` goes in ui.lua, `Inventory` in globals."""
    files, blocks = {}, {}
    by_owner = {}
    for (owner, kind, name), d in members.items():
        by_owner.setdefault(owner, []).append((kind, name, d))
    every = set(owners) | set(by_owner)
    for owner in sorted(every):
        ns = owner.split(".")[0] if "." in owner else "globals"
        if ns not in files:
            files[ns] = ["---@meta", "-- generated by rs3api/docstypes.py from the public docs; don't edit", ""]
            # the namespace's own table, when nothing documents it as an owner
            if ns != "globals" and ns not in every:
                files[ns] += ["%s = {}" % ns, ""]
        out = files[ns]
        # tables between the namespace and the owner (game.options)
        parts = owner.split(".")
        for k in range(2, len(parts)):
            mid = ".".join(parts[:k])
            if mid not in every and "%s = {}" % mid not in out:
                out += ["%s = {}" % mid, ""]
        parent = members.get((owner, "extends", ""), {}).get("parent")
        items = sorted((m for m in by_owner.get(owner, []) if m[0] != "extends"), key=lambda m: (m[0], m[1]))
        start = len(out)
        # values alone are an enum; values beside other members are the class's constants
        if items and all(k == "enum" and d["value"] for k, _, d in items):
            out.append("---@enum %s" % owner)
            out.append("%s = {" % owner)
            for _, name, d in items:
                value = d["value"] if re.match(r"^-?\d+$", d["value"]) else json.dumps(d["value"])
                out.append("    %s = %s,%s" % (name, value, (" -- " + d["doc"]) if d["doc"] else ""))
            out += ["}", ""]
            blocks[owner] = (ns, start + 1, "\n".join(out[start:]).rstrip())
            continue
        if owner != "_G":
            out.append("---@class %s%s" % (owner, (" : " + parent) if parent else ""))
        for k, name, d in items:
            if k == "field":
                out.append("---@field %s %s %s" % (name, d["type"], d["doc"]))
            elif k == "enum":
                out.append("---@field %s integer %s" % (name, d["value"] or d["doc"]))
            elif k == "event":
                f = "fun(payload: %s)" % d["payload"] if d["payload"] else "fun()"
                out.append("---@field %s { Subscribe: fun(ref: string, f: %s), Unsubscribe: fun(ref: string) }" % (name, f))
        if owner != "_G":
            out += ["%s = {}" % owner, ""]
        for k, name, d in items:
            if k not in ("function", "method"):
                continue
            for v in d["variants"]:
                for n, t in v["args"]:
                    out.append("---@param %s %s" % (n, t))
                for r in v["returns"]:
                    out.append("---@return %s" % r)
                names = ", ".join(n.rstrip("?") for n, _ in v["args"])
                where = "" if owner == "_G" else owner + (":" if k == "method" else ".")
                out.append("function %s%s(%s) end" % (where, name, names))
                out.append("")
        blocks[owner] = (ns, start + 1, "\n".join(out[start:]).rstrip())
    return files, blocks


def model(owners, members, blocks):
    """What the checking tools read (rs3_api, the call-shape check): each type's kind, parent and
    members, a member called or not, by dot or colon, and its type; and its block of the Lua."""
    types = {}
    for owner in sorted(set(owners) | {k[0] for k in members}):
        ns, line, text = blocks.get(owner, ("globals", 0, ""))
        types[owner] = {"kind": "enum" if owners.get(owner) == "enum" else "class",
                        "extends": members.get((owner, "extends", ""), {}).get("parent"),
                        "file": ns + ".lua", "line": line, "text": text, "members": {}}
    kinds = {}
    for (owner, kind, _) in members:
        kinds.setdefault(owner, set()).add(kind)
    for owner, ks in kinds.items():
        # no colon methods, only dot functions or events: a table of them (Event, Inventory, World)
        if owner in types and "method" not in ks and ks & {"function", "event"}:
            types[owner]["kind"] = "namespace"
    for (owner, kind, name), d in members.items():
        if kind == "extends":
            continue
        info = {"function": {"call": True, "dot": True}, "method": {"call": True, "dot": False}}.get(kind, {"call": False})
        if kind == "field":
            info["type"] = d["type"].rstrip("?")
        elif kind == "event":
            info["type"] = "EventChannel"
        types[owner]["members"][name] = info
    # a nested table is its parent's member: World.RemoveRoof
    for q in list(types):
        if "." in q and any(i.get("dot") for i in types[q]["members"].values()):
            parent, child = q.rsplit(".", 1)
            if parent in types:
                types[parent]["members"].setdefault(child, {"call": False, "type": q})
    # every Event.<Name> is one of these: Event.Logic.Subscribe(ref, f), a dot, never a colon
    types["EventChannel"] = {"kind": "class", "extends": None, "file": "Event.lua", "line": 0, "text": "",
                             "members": {"Subscribe": {"call": True, "dot": True}, "Unsubscribe": {"call": True, "dot": True}}}
    return {"types": types}


EXTRA_CLASS = re.compile(r"^---@class\s+([\w.]+)(?:\s*:\s*([\w.]+))?")
EXTRA_FIELD = re.compile(r"^---@field\s+(\w+)\??\s+(\S+)")
EXTRA_FUNC = re.compile(r"^function\s+([\w.]+)([.:])(\w+)\s*\(")


def extras():
    """What the extras files add, in the model's shape: their ---@class lines (with a parent),
    ---@field lines, and function definitions by dot or colon."""
    types = {}
    for path in sorted(glob.glob(os.path.join(OUT, "extras", "*.lua"))):
        cls = None
        for line in io.open(path, encoding="utf-8", errors="replace"):
            c, f, fn = EXTRA_CLASS.match(line), EXTRA_FIELD.match(line), EXTRA_FUNC.match(line)
            if c:
                cls = c.group(1)
                t = types.setdefault(cls, {"members": {}, "extends": None})
                t["extends"] = c.group(2) or t["extends"]
            elif f and cls:
                types[cls]["members"][f.group(1)] = {"call": False, "type": f.group(2).rstrip("?")}
            elif fn:
                types.setdefault(fn.group(1), {"members": {}, "extends": None})["members"][fn.group(3)] = {
                    "call": True, "dot": fn.group(2) == "."}
            if not line.startswith("---"):
                cls = None
    return types


def extras_files():
    return sorted(glob.glob(os.path.join(OUT, "extras", "*.lua")))


def line_of(key, d):
    return json.dumps(d, sort_keys=True)


def main():
    pages = sorted(glob.glob(os.path.join(DOCS, "**", "*.md"), recursive=True))
    if not pages:
        print("no docs at %s: run rs3api/docsfetch.py first" % DOCS)
        return 1
    members, owners = {}, {}
    for p in pages:
        parse_page(io.open(p, encoding="utf-8").read(), members, owners)
    os.makedirs(os.path.join(OUT, "extras"), exist_ok=True)
    try:
        before = json.load(io.open(SNAPSHOT, encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    files, blocks = render(owners, members)
    with io.open(MODEL, "w", encoding="utf-8", newline="\n") as f:
        json.dump(model(owners, members, blocks), f, sort_keys=True)
    for old in glob.glob(os.path.join(OUT, "*.lua")):
        if os.path.splitext(os.path.basename(old))[0] not in files:
            os.remove(old)
    for ns, out in files.items():
        with io.open(os.path.join(OUT, ns + ".lua"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out).rstrip() + "\n")
    now = {"%s %s %s" % k: line_of(k, d) for k, d in members.items()}
    with io.open(SNAPSHOT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(now, f, indent=0, sort_keys=True)
    kinds = {}
    for (_, k, _) in members:
        kinds[k] = kinds.get(k, 0) + 1
    print("%d pages -> %d files, %d owners: %s" % (len(pages), len(files), len(owners),
          ", ".join("%d %s" % (n, k) for k, n in sorted(kinds.items()))))
    if before:
        added = sorted(set(now) - set(before))
        removed = sorted(set(before) - set(now))
        changed = sorted(k for k in set(now) & set(before) if now[k] != before[k])
        print("since last run: %d added, %d removed, %d changed" % (len(added), len(removed), len(changed)))
        for label, keys in (("added", added), ("removed", removed), ("changed", changed)):
            for k in keys[:40]:
                print("  %s %s" % (label, k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
