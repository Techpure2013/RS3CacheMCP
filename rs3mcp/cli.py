"""rs3: every rs3cache tool from a terminal, for people rather than AI apps.

    rs3                              what this is and where to start
    rs3 tools                        every tool, grouped
    rs3 help lookup                  what one tool does and what it takes
    rs3 lookup npc Wolf              run one: plain words, name=value, or --name value
    rs3 setup                        add rs3cache to an AI app (Cursor, VS Code, LM Studio, ...)
    rs3 doctor                       check everything is in place
    rs3 rebuild                      build the database from your game cache
    rs3 pool                         open the scrying pool in a browser
    rs3 docs                         write docs/tools.md and docs/database.md from the code

Asking the cache:

    rs3 lookup item Coins            one item, npc, loc, enum, struct... with params resolved
    rs3 var QUEST_OBJECTIVE          a var or varbit: its base, bits and who reads it
    rs3 sprite necrosis              sprites by name
    rs3 schema                       the cache's tables (config, inv_stock, varbit, model_use...)
    rs3 query sql="select ..."       any SELECT over those tables

db_query and config read other sources (questbuddy, a live client), not the cache.

Answers print as text and tables. Add --json to any tool for the raw answer.
"""

import difflib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import textwrap

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_FILE = os.path.join(BASE, "rs3-mcp.py")

# words a tool is most often asked about
TEXTY = ("name", "q", "query", "pattern", "search", "text", "sql", "script", "symbol", "component",
         "path", "var", "id")


def _server():
    spec = importlib.util.spec_from_file_location("rs3server", SERVER_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _style(code, s):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return "\x1b[%sm%s\x1b[0m" % (code, s)


def bold(s):
    return _style("1", s)


def dim(s):
    return _style("2", s)


def width():
    return max(60, min(140, shutil.get_terminal_size((100, 20)).columns))


def short(name):
    return name[4:] if name.startswith("rs3_") else name


def first_sentence(text):
    text = " ".join(str(text or "").split())
    for end in (". ", "? "):
        at = text.find(end)
        if 0 < at < 150:
            return text[:at + 1]
    return text[:150]


# tool arguments

def resolve(server, word):
    """The tool a typed name means, or None. rs3_ is optional and dashes read as underscores."""
    w = word.strip().lower().replace("-", "_")
    for cand in (w, "rs3_" + w):
        if cand in server.BY_NAME:
            return cand
    return None


def parse(tool, words):
    """(arguments, raw) from what was typed after the tool name."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    args, loose, raw = {}, [], False
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--json":
            raw = True
        elif w.startswith("--") and len(w) > 2:
            key, eq, val = w[2:].partition("=")
            if not eq:
                boolean = (props.get(key) or {}).get("type") == "boolean"
                if not boolean and i + 1 < len(words) and not words[i + 1].startswith("--"):
                    val, i = words[i + 1], i + 1
                else:
                    val = "true"
            args[key] = val
        elif "=" in w and w.split("=", 1)[0].replace("_", "").isalnum():
            key, val = w.split("=", 1)
            args[key] = val
        else:
            loose.append(w)
        i += 1

    slots = [k for k in required if k not in args]
    texty = [k for k in TEXTY if k in props and k not in args and k not in slots]
    while loose and slots:
        k = slots.pop(0)
        # the last required one takes the rest
        if not slots and (k in TEXTY or not texty):
            args[k], loose = " ".join(loose), []
        else:
            args[k] = loose.pop(0)
    # rs3 config npc 81 names the kind first
    if loose and "type" in props and "type" not in args and not loose[0].lstrip("-").isdigit():
        args["type"] = loose.pop(0)
    if loose:
        if len(loose) == 1 and loose[0].lstrip("-").isdigit() and "id" in texty:
            args["id"] = loose[0]
        else:
            k = next((t for t in texty if t != "id"), None) or (texty[0] if texty else None)
            if k is None:
                raise ValueError("%s does not take plain words here: %s" % (short(tool["name"]), " ".join(loose)))
            args[k] = " ".join(loose)
    return {k: _coerce(props.get(k) or {}, v) for k, v in args.items()}, raw


def _coerce(prop, value):
    kind = prop.get("type")
    if not isinstance(value, str):
        return value
    if kind in ("integer", "number") and value.lstrip("-").replace(".", "", 1).isdigit():
        return int(value) if kind == "integer" or value.lstrip("-").isdigit() else float(value)
    if kind == "boolean":
        return value.lower() in ("1", "true", "yes", "y", "on")
    if kind == "array":
        return [x.strip() for x in value.split(",") if x.strip()]
    if kind == "object":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# answers as text

def _scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def _cell(v):
    if v is None:
        return "-"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v)


def _table(rows, room):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    for cap in (40, 24, 14):
        sizes = [min(cap, max([len(c)] + [len(_cell(r.get(c)).split("\n")[0]) for r in rows])) for c in cols]
        if sum(sizes) + 2 * (len(cols) - 1) <= room:
            break
    else:
        return None

    def fit(s, n):
        s = s.split("\n")[0]
        return s if len(s) <= n else s[:max(1, n - 1)] + "~"
    out = ["  ".join(bold(c[:n].ljust(n)) for c, n in zip(cols, sizes))]
    for r in rows:
        out.append("  ".join(fit(_cell(r.get(c)), n).ljust(n) for c, n in zip(cols, sizes)).rstrip())
    return out


def render(value):
    out = []
    _emit(value, 0, None, out)
    return "\n".join(out)


def _emit(v, depth, key, out):
    pad = "  " * depth
    room = width() - len(pad)
    if _scalar(v):
        text = _cell(v)
        if key is None:
            out.append(pad + text)
        elif "\n" in text or len(text) + len(key) + 2 > room:
            if "\n" in text:
                out.append(pad + bold(key) + ":")
                out.extend(pad + "  " + line for line in text.split("\n"))
            else:
                lines = textwrap.wrap(text, room - 2)
                out.append(pad + bold(key) + ": " + (lines[0] if lines else ""))
                out.extend(pad + "  " + line for line in lines[1:])
        else:
            out.append(pad + bold(key) + ": " + text)
        return
    if isinstance(v, dict):
        if key is not None:
            out.append(pad + bold(key) + ":" + (" (none)" if not v else ""))
            depth, pad = depth + 1, pad + "  "
        for k, x in v.items():
            _emit(x, depth, str(k), out)
        return
    # a list
    if not v:
        if key is not None:
            out.append(pad + bold(key) + ": (none)")
        return
    if all(_scalar(x) for x in v):
        _emit(", ".join(_cell(x) for x in v), depth, key, out)
        return
    if key is not None:
        out.append(pad + bold(key) + ": %d" % len(v))
        depth, pad = depth + 1, pad + "  "
    if all(isinstance(x, dict) and all(_scalar(y) for y in x.values()) for x in v):
        got = _table(v, width() - len(pad))
        if got:
            out.extend(pad + line for line in got)
            return
    for x in v:
        if _scalar(x):
            out.append(pad + "- " + _cell(x))
        else:
            sub = []
            _emit(x, 0, None, sub)
            out.append(pad + "- " + (sub[0] if sub else ""))
            out.extend(pad + "  " + line for line in sub[1:])


# commands

def overview(server):
    print(bold("rs3cache") + ": the RuneScape 3 game data on your computer, as tools you can ask.\n")
    print("Try:")
    # every example works on a fresh copy
    have = {t["name"] for t in server.TOOLS}
    quest = [("rs3 quest \"Cook's Assistant\"", "a quest's var and walkthrough")]
    if "rs3_quest" not in have:
        quest = []
    for cmd, what in [("rs3 lookup npc Wolf", "one npc, loc, item, struct or enum"),
                      ("rs3 var 12", "what a var holds, and the varbits packed into it")] + quest + [
                      ("rs3 script IF_SETHIDE", "search the decompiled clientscripts"),
                      ("rs3 tools", "every tool (%d of them)" % len(server.TOOLS)),
                      ("rs3 help lookup", "what one tool takes"),
                      ("rs3 doctor", "check everything is set up"),
                      ("rs3 setup", "add rs3cache to Cursor, VS Code, LM Studio and the rest"),
                      ("rs3 rebuild", "rebuild the database after a game update"),
                      ("rs3 pool", "watch what AI apps are asking, in a browser")]:
        print("  %-34s %s" % (cmd, dim(what)))
    return 0


def tools(server, only=None):
    by_set = {}
    for t in server.TOOLS:
        by_set.setdefault(server.toolset_of(t), []).append(t)
    for name in sorted(by_set):
        if only and only.lower() != name:
            continue
        print(bold(name))
        room = width() - 21
        for t in sorted(by_set[name], key=lambda t: t["name"]):
            line = first_sentence(t.get("description"))
            print("  %-18s %s" % (short(t["name"]), line if len(line) <= room else line[:room - 1] + "~"))
        print()
    print(dim("rs3 help <tool> for what one takes"))
    return 0


def examples(server, name):
    """Worked examples for a tool, as argument dicts: the ones its definition gives, else the one
    the server's selftest runs."""
    given = server.BY_NAME[name].get("examples")
    if given:
        return list(given)
    try:
        return [args for tool, args, _summary in _selftest_calls(server) if tool == name][:1]
    except Exception:
        return []


def command_line(name, args):
    parts = []
    for k, v in args.items():
        v = str(v).lower() if isinstance(v, bool) else str(v)
        parts.append("%s=%s" % (k, '"%s"' % v if " " in v else v))
    return ("rs3 %s %s" % (short(name), " ".join(parts))).rstrip()


def _selftest_calls(server):
    """The worked examples out of the server's selftest: real arguments that are known to answer."""
    import ast
    src = io.open(SERVER_FILE, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "selftest":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and getattr(sub.targets[0], "id", "") == "checks":
                    for el in sub.value.elts:
                        try:
                            yield ast.literal_eval(el.elts[0]), ast.literal_eval(el.elts[1]), None
                        except (ValueError, IndexError):
                            continue


def help_tool(server, word):
    name = resolve(server, word)
    if name is None:
        return unknown(server, word)
    t = server.BY_NAME[name]
    print(bold(short(name)) + dim("  (%s, in %s)" % (name, server.toolset_of(t))))
    print()
    for para in str(t.get("description") or "").split("\n"):
        print(textwrap.fill(para, width()) if para.strip() else "")
    schema = t.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if props:
        print("\n" + bold("Takes"))
        for k in list(required) + [k for k in props if k not in required]:
            p = props[k]
            tag = "required" if k in required else p.get("type") or ""
            lines = textwrap.wrap(str(p.get("description") or ""), width() - 30) or [""]
            print("  %-16s %-9s %s" % (k, tag, lines[0]))
            for line in lines[1:]:
                print(" " * 29 + line)
    ex = examples(server, name)
    if ex:
        print("\n" + bold("Example" + ("s" if len(ex) > 1 else "")))
        for args in ex:
            print("  " + command_line(name, args))
    print(dim("\nPlain words fill the required ones first, then the name or id. --json for the raw answer."))
    return 0


def unknown(server, word):
    names = [short(n) for n in server.BY_NAME]
    close = difflib.get_close_matches(word.lower().replace("-", "_"), names, n=4, cutoff=0.5)
    print("There is no tool or command called %s." % word)
    if close:
        print("Did you mean: " + ", ".join(close))
    print(dim("rs3 tools lists them all"))
    return 1


def run_tool(server, name, words):
    t = server.BY_NAME[name]
    try:
        args, raw = parse(t, words)
    except ValueError as e:
        print(str(e))
        return 1
    try:
        got = server.lean(t["fn"](args))
    except Exception as e:
        print("That did not work: %s" % e)
        required = (t.get("inputSchema") or {}).get("required") or []
        missing = [k for k in required if k not in args]
        if missing:
            print("It needs: " + ", ".join(missing))
        print(dim("rs3 help %s for what it takes" % short(name)))
        return 1
    print(json.dumps(got, indent=1, default=str) if raw else render(got))
    return 0


def setup(words):
    from . import clients
    if not words:
        print(bold("AI apps on this computer\n"))
        for key, label, *_ in clients.APPS:
            state = clients.status(key)
            shown = {"set": "rs3cache added", "here": "installed", None: dim("not found")}[state]
            print("  %-15s %-40s %s" % (key, label, shown))
        print("\n" + "rs3 setup <app> adds rs3cache to one, rs3 setup all to every installed app.")
        print(dim("Restart the app afterwards so it starts the server."))
        return 0
    keys = [k for k, *_ in clients.APPS if clients.status(k)] if words[0] == "all" else words
    bad = 0
    for key in keys:
        if key not in clients.BY_KEY:
            print("No app called %s. rs3 setup lists them." % key)
            bad += 1
            continue
        label = clients.BY_KEY[key][1]
        try:
            state, where, snippet = clients.add(key)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print("%s: could not update its config: %s" % (label, e))
            bad += 1
            continue
        if state == "added":
            print("%s: added, in %s. Restart it to pick rs3cache up." % (label, where))
        elif state == "already":
            print("%s: already set up (%s)" % (label, where))
        else:
            print("%s: %s has something in it this cannot safely edit, comments perhaps. Add this by hand:"
                  % (label, where))
            print(textwrap.indent(snippet, "    "))
    return 1 if bad else 0


def doctor():
    from . import doctor as d
    marks = {"ok": _style("32", "ok  "), "warn": _style("33", "warn"), "fail": _style("31", "FAIL")}
    worst = 0
    for state, what, found, fix in d.checks():
        print("%s  %-32s %s" % (marks[state], what, found))
        if fix:
            print("      %s %s" % (" " * 32, dim(fix)))
        worst = max(worst, {"ok": 0, "warn": 1, "fail": 2}[state])
    print()
    print({0: "Everything is in place.", 1: "Works. The warnings above are optional extras or worth a look.",
           2: "Something needs fixing before the tools can work: see FAIL above."}[worst])
    return 1 if worst == 2 else 0


def rebuild(words):
    return subprocess.call([sys.executable, os.path.join(BASE, "rebuild-cache.py")] + words)


def pool(server):
    import socket
    import time
    import webbrowser
    port = int(os.environ.get("RS3_MONITOR_PORT") or 7331)
    url = "http://127.0.0.1:%d/" % port
    with socket.socket() as s:
        s.settimeout(0.5)
        running = s.connect_ex(("127.0.0.1", port)) == 0
    if running:
        print("The scrying pool is already running: " + url)
        webbrowser.open(url)
        return 0
    from . import bypass, journal
    journal.serve([{"name": t["name"], "set": server.toolset_of(t)} for t in server.TOOLS],
                  bypass.advise, retry=0)
    time.sleep(0.5)
    print("Scrying pool at %s. Leave this window open; Ctrl+C stops it." % url)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if argv and argv[0] in ("setup", "doctor", "rebuild"):
        # these work before there is a database
        return {"setup": lambda: setup(argv[1:]), "doctor": doctor, "rebuild": lambda: rebuild(argv[1:])}[argv[0]]()
    if argv and argv[0] in ("-h", "--help") and len(argv) == 1:
        print(__doc__)
        return 0
    server = _server()
    if not argv:
        return overview(server)
    cmd, rest = argv[0], argv[1:]
    if cmd == "tools":
        return tools(server, rest[0] if rest else None)
    if cmd == "help":
        return help_tool(server, rest[0]) if rest else overview(server)
    if cmd == "pool":
        return pool(server)
    if cmd == "docs":
        from . import docs
        docs.write(rest[0] if rest else None, server)
        return 0
    name = resolve(server, cmd)
    if name is None:
        return unknown(server, cmd)
    if rest and rest[0] in ("-h", "--help"):
        return help_tool(server, cmd)
    return run_tool(server, name, rest)
