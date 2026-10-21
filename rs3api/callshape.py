"""Client calls checked against the typings: dot or colon, a field called, a member that is not there.

The 40-run breeding pass on 2026-09-24 found every one of 20 agents writing Event.Logic:Subscribe,
with the declaration saying this: void in front of half of them. A colon hands the channel itself in
as the first argument. Six of the other ten invented a lower-case subscribe. lua_check passed all of
them, because nothing read a call against its declaration. This does.

A receiver is typed two ways: a dotted path from a declared global (Event.Logic is EventChannel),
or a local made by a declared constructor (local grid = ui.Grid.new(...) is ui.Grid). Anything else
is left alone rather than guessed at.
"""

import io
import re

LUA_STD = {"string", "table", "math", "coroutine", "debug", "utf8", "os", "io", "package", "bit32", "_G"}
CALL = re.compile(r"((?<![\w.:])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)([.:])([A-Za-z_]\w*)\s*\(")
MADE = re.compile(r"\blocal\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_][\w.]*)\.new\s*\(")
DECL = re.compile(r"^\s*(?:declare\s+)?(?:export\s+)?(class|interface|namespace|enum)\s+([A-Za-z_]\w*)")
MEMBER = re.compile(r"^\s*((?:static\s+|readonly\s+|const\s+|let\s+|function\s+)*)([A-Za-z_]\w*)\??\s*(<[^(]*>)?\s*([(:])(.*)$")
RULE = ("calls are read against the typings: a function (not a method) takes a dot "
        "(Event.Logic.Subscribe), a method takes a colon (grid:SetCellSize), and a member the typings "
        "lack is named")
VOID_FIRST = re.compile(r"\s*this\s*:\s*void\b")
GLOBAL = re.compile(r"^declare\s+(const|let|var|function)\s+([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_][\w.]*))?", re.M)
_CACHE = {}


def _globals():
    """Top-level names the typings declare outside a type: consts with their type, and functions."""
    if "globals" in _CACHE:
        return _CACHE["globals"]
    from . import api
    m = api.model()
    if m is not None:
        # a global table is its own type in the generated typings (Inventory, Event)
        _CACHE["globals"] = {q: q for q in m["types"] if "." not in q and q != "_G"}
        _CACHE["globals"].update({n: "function" for n in m["types"].get("_G", {}).get("members", {})})
        return _CACHE["globals"]
    out = {}
    for path in api._files():
        for kind, name, typ in GLOBAL.findall(io.open(path, encoding="utf-8", errors="replace").read()):
            out.setdefault(name, "function" if kind == "function" else typ)
    _CACHE["globals"] = out
    return out


def _types():
    """{qualified: {"kind", "extends", "ns", "decls"}}; members are read on first use, by _members."""
    if "types" in _CACHE:
        return _CACHE["types"]
    from . import api
    m = api.model()
    if m is not None:
        _CACHE["types"] = {
            q: {"kind": t["kind"], "extends": t["extends"], "decls": [],
                "ns": q.rsplit(".", 1)[0] if "." in q else "",
                "members": {n: {"call": i["call"], "void": i.get("dot", False), "static": False, "type": i.get("type")}
                            for n, i in t["members"].items()}}
            for q, t in m["types"].items()}
        return _CACHE["types"]
    out = {}
    for d in api._all():
        q = d["qualified"]
        if q in out:
            # merge a repeated namespace
            out[q]["decls"].append(d)
            out[q]["extends"] = out[q]["extends"] or d.get("extends")
        else:
            out[q] = {"kind": d["kind"], "extends": d.get("extends"), "decls": [d],
                      "ns": q.rsplit(".", 1)[0] if "." in q else ""}
    _CACHE["types"] = out
    return out


def _members(q):
    """{name: info} for one type, parsed once. The id tables run to 137k lines, so only when asked."""
    t = _types().get(q)
    if t is None:
        return {}
    if "members" in t:
        return t["members"]
    target = t["members"] = {}
    for d in t["decls"]:
        depth = 0
        for line in d["text"].split("\n")[1:-1]:
            if depth == 0:
                nested = DECL.match(line)
                if nested:
                    target.setdefault(nested.group(2), {"type": q + "." + nested.group(2), "call": False})
                else:
                    m = MEMBER.match(line)
                    if m:
                        mods, name, sep, rest = m.group(1), m.group(2), m.group(4), m.group(5)
                        info = target.setdefault(name, {"call": False, "void": False, "static": False, "type": None})
                        if sep == "(":
                            info["call"] = True
                            # first parameter only, not callbacks
                            info["void"] = info["void"] or bool(VOID_FIRST.match(rest)) or "function" in mods
                            info["static"] = info["static"] or "static" in mods
                        else:
                            head = re.match(r"\s*([A-Za-z_][\w.]*)", rest)
                            info["type"] = info["type"] or (head.group(1) if head else None)
                            # function-typed field is callable
                            if "=>" in rest.split(";")[0]:
                                inner = re.search(r"\(\s*(.*)", rest)
                                info["call"], info["void"] = True, bool(inner and VOID_FIRST.match(inner.group(1)))
            depth += line.count("{") - line.count("}")
    return target


def _qualify(name, ns):
    types = _types()
    if not name:
        return None
    for q in (ns + "." + name if ns else None, name):
        if q and q in types:
            return q
    return None


def _member(q, name):
    """The member and the type that declares it, following extends."""
    types, seen = _types(), set()
    while q and q not in seen:
        seen.add(q)
        t = types.get(q)
        if not t:
            return None, None
        if name in _members(q):
            return _members(q)[name], q
        q = _qualify(t.get("extends"), t["ns"])
    return None, None


def _members_of(q):
    types, out, seen = _types(), [], set()
    while q and q not in seen:
        seen.add(q)
        t = types.get(q) or {}
        out += sorted(_members(q))
        q = _qualify(t.get("extends"), t.get("ns", ""))
    return out


def resolve(path, locals_):
    """The declared type a dotted receiver has, or None. A missing member on the way is reported."""
    parts = path.split(".")
    g = _globals().get(parts[0])
    q = locals_.get(parts[0]) or _qualify(parts[0], "") or (_qualify(g, "") if g and g != "function" else None)
    if not q:
        return None, None
    for p in parts[1:]:
        m, owner = _member(q, p)
        if m is None:
            nested = q + "." + p
            if nested in _types():
                q = nested
                continue
            return None, (q, p)
        q = _qualify(m.get("type"), _types()[owner]["ns"]) if not m.get("call") else None
        if not q:
            return None, None
    return q, None


def _defined(code):
    """Names the file defines or assigns at any depth, so a plugin's own tables are not client names."""
    names = set(re.findall(r"\blocal\s+function\s+([A-Za-z_]\w*)|\blocal\s+([A-Za-z_]\w*)", code))
    out = {a or b for a, b in names}
    out |= set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=", code))
    out |= set(re.findall(r"\bfunction\s+([A-Za-z_]\w*)\b", code))
    out |= set(re.findall(r"\bfor\s+([A-Za-z_]\w*)\s*[,=]", code))
    out |= set(re.findall(r"\bfor\s+[A-Za-z_]\w*\s*,\s*([A-Za-z_]\w*)", code))
    out |= {p.strip() for ps in re.findall(r"function\s*[\w.:]*\s*\(([^)]*)\)", code)
            for p in ps.split(",") if p.strip()}
    return out


def _bindings(code, name):
    """How many places bind a name: locals, loop variables, parameters, plain assignments."""
    n = len(re.findall(r"\blocal\s+(?:[A-Za-z_]\w*\s*,\s*)*%s\b" % re.escape(name), code))
    n += len(re.findall(r"\bfor\s+(?:[A-Za-z_]\w*\s*,\s*)*%s\b" % re.escape(name), code))
    n += sum(1 for ps in re.findall(r"function\s*[\w.:]*\s*\(([^)]*)\)", code)
             if name in [p.strip() for p in ps.split(",")])
    n += len(re.findall(r"(?m)^\s*%s\s*=[^=]" % re.escape(name), code))
    return n


BLOCK = re.compile(r"\b(function|if|for|while|do|repeat|end|until)\b")


def _blocks(lines):
    """(open line, close line) for every block, lines numbered from 1."""
    stack, out, loop = [], [], False
    for n, line in enumerate(lines, 1):
        for m in BLOCK.finditer(line):
            w = m.group(1)
            if w in ("for", "while"):
                stack.append(n)
                loop = True
            elif w == "do" and loop:
                loop = False
            elif w in ("function", "if", "do", "repeat"):
                stack.append(n)
            elif stack:
                out.append((stack.pop(), n))
    return out + [(o, len(lines)) for o in stack]


def _typed_locals(code, lines):
    """{line: {name: type}} for locals made by a declared constructor, over the block they live in."""
    types, blocks, out = _types(), _blocks(lines), {}
    for n, line in enumerate(lines, 1):
        for name, ctor in MADE.findall(line):
            q = _qualify(ctor, "")
            if not q or types[q]["kind"] not in ("class", "interface"):
                continue
            inner = [(o, c) for o, c in blocks if o < n <= c]
            end = min((c for _, c in inner), default=len(lines))
            # rebound inside its block, it could be anything
            if _bindings("\n".join(lines[n - 1:end]), name) != 1:
                continue
            for k in range(n, end + 1):
                out.setdefault(k, {})[name] = q
    return out


def _vouched(owner, member):
    """Whether the docs mirror names this member, where the typings are known to have gaps."""
    try:
        from .luacheck import _documented
        docs = _documented()
    except Exception:
        return False
    short = owner.rsplit(".", 1)[-1]
    return short + "." + member in docs or any(d.startswith(member + ".") for d in docs)


def check(path):
    """Findings for one lua file: [{line, problem, code}]."""
    try:
        raw = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    from .lualex import strip
    code = strip(raw)
    from . import api
    now = api.stamp()
    if _CACHE.get("stamp") != now:
        _CACHE.clear()
        _CACHE["stamp"] = now
    types = _types()
    defined = _defined(code)
    out, lines = [], code.split("\n")
    typed = _typed_locals(code, lines)
    for n, line in enumerate(lines, 1):
        locals_ = typed.get(n, {})
        for m in CALL.finditer(line):
            recv, sep, name = m.group(1), m.group(2), m.group(3)
            base = recv.split(".")[0]
            if base in LUA_STD or (base in defined and base not in locals_):
                continue
            q, missing = resolve(recv, locals_)
            where = raw.split("\n")[n - 1].strip()[:90]
            if missing and _vouched(*missing):
                continue
            if missing:
                have = _members_of(missing[0])
                near = [x for x in have if x.lower() == missing[1].lower()]
                out.append({"line": n, "code": where, "problem": "%s has no member %s%s" % (
                    missing[0], missing[1], "; did you mean %s" % near[0] if near else "")})
                continue
            if q is None:
                if base[:1].isupper() and base not in types and base not in defined and "." not in recv:
                    out.append({"line": n, "code": where,
                                "problem": "%s is not a global the client declares" % base})
                continue
            info, owner = _member(q, name)
            if info is None and _vouched(q, name):
                continue
            if info is None:
                have = _members_of(q)
                near = [x for x in have if x.lower() == name.lower()]
                out.append({"line": n, "code": where, "problem": "%s has no member %s%s" % (
                    q, name, "; did you mean %s" % near[0] if near else "")})
                continue
            if not info.get("call"):
                out.append({"line": n, "code": where, "problem": "%s.%s is a field, not a function" % (owner, name)})
            elif sep == ":" and (info.get("void") or info.get("static")):
                out.append({"line": n, "code": where,
                            "problem": "%s.%s is a function, not a method, so call it with a dot; a colon passes %s "
                                       "in as the first argument" % (owner, name, recv)})
            elif sep == "." and not (info.get("void") or info.get("static")):
                first = re.match(r"\s*([A-Za-z_][\w.]*)", line[m.end():])
                if not first or first.group(1) != recv:
                    out.append({"line": n, "code": where,
                                "problem": "%s.%s needs its object: call it with a colon, %s:%s(...)" % (
                                    owner, name, recv, name)})
    return out
