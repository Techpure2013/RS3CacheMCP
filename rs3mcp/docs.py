"""The parts of the documentation that are written from the code, so they cannot drift from it.

    rs3 docs          writes docs/tools.md and docs/database.md, and every page's navigation

tools.md comes from the tool definitions: descriptions, parameters, examples, whether a tool only
reads. database.md comes from the database as built: tables, columns, row counts, and each config
type with the spec that decodes it. The rebuild runs this after installing, so the database page
matches the database. The other pages are written by hand.

The navigation is the line under each title back to the index, an "On this page" list on the longer
pages, and previous and next at the bottom. The order is the docs README's tables, so adding a page
there is all it takes.
"""

import io
import json
import os
import re
import sqlite3
import time

from . import common, ops

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = os.path.join(BASE, "docs")
INDEX = "README.md"
# a page with at least this many headings gets an "On this page" list
CONTENTS_FROM = 6


def _cell(text):
    return " ".join(str(text or "").split()).replace("|", "\\|")


def slug(heading, seen):
    """The anchor GitHub and VS Code give a heading. A repeat gets -1, -2 on the end."""
    s = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else "%s-%d" % (s, n)


def headings(text):
    """[(level, text, anchor)] for every heading outside a code block."""
    out, seen, fenced = [], {}, False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"(#{1,6}) (.+)", line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), slug(m.group(2), seen)))
    return out


def pages(folder=None):
    """[(file, title, section)] in the order the index lists them."""
    folder = folder or FOLDER
    out, section = [], None
    with io.open(os.path.join(folder, INDEX), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## "):
                section = line[3:].strip()
            m = re.match(r"\| \[([^\]]+)\]\(([\w-]+\.md)\)", line)
            if m and section:
                out.append((m.group(2), m.group(1), section))
    return out


def _strip_nav(lines):
    body = list(lines)
    if len(body) > 2 and body[2].startswith("[All docs](%s)" % INDEX):
        del body[2]
        while len(body) > 2 and body[2] == "":
            del body[2]
        if len(body) > 2 and body[2] == "On this page":
            del body[2]
            while len(body) > 2 and (body[2].startswith("- ") or body[2].startswith("  - ") or body[2] == ""):
                del body[2]
    while body and body[-1] == "":
        body.pop()
    if body and (body[-1].startswith("Previous: [") or body[-1].startswith("Next: [")):
        body.pop()
        while body and body[-1] == "":
            body.pop()
        if body and body[-1] == "---":
            body.pop()
        while body and body[-1] == "":
            body.pop()
    return body


def with_nav(name, text, order):
    """The page with its navigation put in, or brought up to date."""
    at = [i for i, p in enumerate(order) if p[0] == name]
    if not at or not text.startswith("# "):
        return text
    i = at[0]
    body = _strip_nav(text.splitlines())
    top = ["[All docs](%s) › %s" % (INDEX, order[i][2]), ""]
    marks = [h for h in headings("\n".join(body)) if h[0] in (2, 3)]
    if name != "tools.md" and len(marks) >= CONTENTS_FROM:
        top += ["On this page"] + ["%s- [%s](#%s)" % ("  " * (level - 2), title.replace("`", ""), anchor)
                                   for level, title, anchor in marks] + [""]
    steps = []
    if i > 0:
        steps.append("Previous: [%s](%s)" % (order[i - 1][1], order[i - 1][0]))
    if i + 1 < len(order):
        steps.append("Next: [%s](%s)" % (order[i + 1][1], order[i + 1][0]))
    return "\n".join(body[:2] + top + body[2:] + ["", "---", "", " · ".join(steps)]) + "\n"


def tools_md(server):
    from . import addons, cli
    # an add-on marked PRIVATE stays out of what is written down, since these pages are shared
    private = addons.private_modules()
    by_set = {}
    for t in server.TOOLS:
        if private and t["fn"].__module__.startswith(private):
            continue
        by_set.setdefault(server.toolset_of(t), []).append(t)
    out = ["# Tools", "",
           "Every tool, in %d groups. There are %d in all. AI apps and the `rs3` command use the same "
           "tools. Apps see the full name, like `rs3_lookup`. In `rs3` you can leave off the `rs3_`."
           % (len(by_set), sum(map(len, by_set.values()))),
           "",
           "This page is written automatically by `rs3 docs`, so don't edit it by hand.",
           "",
           "Every tool also takes `pick`, to get just one part of an answer (`pick=counts.npcs`), and "
           "`outDir`, to save the answer to a file. To show an app only some groups, see "
           "[settings](settings.md).", ""]
    # anchors in the order the headings come, so a tool named like its group (wiki) links to the tool
    seen, anchor = {}, {}
    slug("Tools", seen)
    for name in sorted(by_set):
        anchor[name] = slug(name, seen)
        for t in sorted(by_set[name], key=lambda t: t["name"]):
            anchor[t["name"]] = slug(cli.short(t["name"]), seen)
    out.append("| Group | Tools |")
    out.append("|---|---|")
    for name in sorted(by_set):
        out.append("| [%s](#%s) | %s |" % (name, anchor[name], ", ".join(
            "[%s](#%s)" % (cli.short(t["name"]), anchor[t["name"]]) for t in sorted(by_set[name], key=lambda t: t["name"]))))
    for name in sorted(by_set):
        out += ["", "## %s" % name]
        for t in sorted(by_set[name], key=lambda t: t["name"]):
            short = cli.short(t["name"])
            notes = server.SIDE_EFFECTS.get(t["name"], server.READS_ONLY)
            kind = []
            if notes.get("readOnlyHint"):
                kind.append("reads only")
            else:
                kind.append("writes or runs something")
            if notes.get("openWorldHint"):
                kind.append("reaches the network")
            addon = t["fn"].__module__.split(".")[0] != "rs3mcp"
            if addon:
                kind.append("from the %s add-on" % t["fn"].__module__.split(".")[0])
            out += ["", "### %s" % short, "", "`%s`, %s." % (t["name"], ", ".join(kind)), "",
                    " ".join(str(t.get("description") or "").split())]
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            if props:
                out += ["", "| Argument | Type | | What |", "|---|---|---|---|"]
                for k in list(required) + [k for k in props if k not in required]:
                    p = props[k]
                    out.append("| `%s` | %s | %s | %s |" % (k, p.get("type") or "", "required" if k in required else "",
                                                           _cell(p.get("description"))))
            else:
                out += ["", "Takes no arguments."]
            ex = cli.examples(server, t["name"])
            if ex:
                out += ["", "```"]
                for args in ex:
                    out.append(cli.command_line(t["name"], args))
                out.append("```")
                out += ["", "Over MCP: `%s`" % json.dumps({"name": t["name"], "arguments": ex[0]})]
    return "\n".join(out) + "\n"


def database_md():
    db = common.DB
    out = ["# The database", ""]
    if not os.path.exists(db):
        return "\n".join(out + ["Not built yet. `rs3 rebuild` builds it, and this page with it."]) + "\n"
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(db)))
    out += ["Every table in the database, `data/rs3-cache.db`. It's a normal SQLite file, built on %s." % built,
            "",
            "This page is written from the database itself by `rs3 docs`, which runs after every rebuild, "
            "so the numbers are always current.", "",
            "To ask the database something, use `rs3 query` with a SELECT. `rs3 schema` shows the same "
            "tables, plus common mistakes to avoid.", "", "## Tables", ""]
    tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' "
                                      "and name not like 'sqlite_%' order by name")]
    for t in tables:
        n = c.execute('select count(*) from "%s"' % t).fetchone()[0]
        cols = c.execute('pragma table_info("%s")' % t).fetchall()
        idx = [r[1] for r in c.execute('pragma index_list("%s")' % t) if not r[1].startswith("sqlite_")]
        out += ["### %s" % t, "", "%s rows." % format(n, ","), "",
                "| Column | Type | Key |", "|---|---|---|"]
        for cid, name, ctype, notnull, _dflt, pk in cols:
            out.append("| `%s` | %s | %s |" % (name, ctype or "", "primary" if pk else ""))
        if idx:
            out += ["", "Indexed: " + ", ".join("`%s`" % i for i in idx)]
        out.append("")
    out += ["## Config types", "",
            "`config` holds one row per config. `data` is the decoded record as json, its field names "
            "taken from the type's spec in `rs3mcp/specs/` (see [config specs](specs.md)). Types with no "
            "spec come from the name lists alone, or from `rs3mcp/models.py` for models.", "",
            "| Type | Rows | Named | Spec | Opcodes |", "|---|---|---|---|---|"]
    specs = {os.path.splitext(f)[0] for f in os.listdir(ops.SPECS) if f.endswith(".ops")}
    for ctype, n, named in c.execute("select type, count(*), sum(name is not null) from config "
                                     "group by type order by type"):
        if ctype in specs:
            spec = ops.load(ctype)
            shape = "one record shape" if spec.get("record") else "%d" % len(spec["ops"])
            out.append("| %s | %s | %s | `specs/%s.ops` | %s |" % (ctype, format(n, ","), format(named or 0, ","), ctype, shape))
        else:
            out.append("| %s | %s | %s | | |" % (ctype, format(n, ","), format(named or 0, ",")))
    c.close()
    return "\n".join(out) + "\n"


def write(folder=None, server=None, say=print):
    folder = folder or FOLDER
    os.makedirs(folder, exist_ok=True)
    if server is None:
        from . import cli
        server = cli._server()
    order = pages(folder) if os.path.exists(os.path.join(folder, INDEX)) else []
    for name, text in (("tools.md", tools_md(server)), ("database.md", database_md())):
        with io.open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(with_nav(name, text, order))
        say("wrote %s" % os.path.join(folder, name))
    for name, _title, _section in order:
        path = os.path.join(folder, name)
        if name in ("tools.md", "database.md") or not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        new = with_nav(name, text, order)
        if new != text:
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
            say("navigation in %s" % path)
