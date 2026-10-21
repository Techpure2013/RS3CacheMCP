"""The RS3 client's plugin api: its docs and typings, and the lua tools that read plugins against them.

The docs come from developer.runescape.com (docsfetch.py), and the Lua typings are generated from
them (docstypes.py), both refreshed after every rebuild. The tools: the api and its gotchas
(rs3_api, rs3_api_gotchas), the docs against the typings (rs3_docs_check), writing lua for the client
(rs3_lua, rs3_lua_example), checking it (rs3_lua_check, rs3_lsp, rs3_plugin_lint), searching it by
shape (rs3_lua_ast), and running it in a sandbox shaped like the client (rs3_lab_run, rs3_lab_query).

The shared tools never import this. They ask rs3mcp.addons for what it offers and carry on without.
personal.py, when it's here, adds what only one machine has (harvests, recordings, lessons); it
never ships.
"""

import os
import shutil

from rs3mcp import addons

from . import api, docscheck, lab, labtrace, lsp, lua, luaast, luacheck, paths, pluginlint, sandbox

# anything with a TOOLS list belongs in here
FAMILIES = [api, docscheck, lua, luacheck, pluginlint, lsp, luaast, lab, labtrace]

HERE = os.path.dirname(os.path.abspath(__file__))


def known_docs():
    """Each page's resource name: its path with dots, core.events, since a resource name can't
    hold a slash and no page's path holds a dot."""
    from . import docsfetch
    return sorted(p.replace("/", ".") for p in docsfetch.pages())


def _doc_page(name):
    from rs3mcp.resources import MD, read_file
    from . import docsfetch
    return read_file(os.path.join(docsfetch.OUT, *name.split(".")) + ".md", MD)


def refresh_api_docs():
    """The api docs mirror and its Lua typings, fetched again where they changed. Without
    beautifulsoup4 it says so and leaves them as they are: the database is installed by then."""
    import importlib.util
    if importlib.util.find_spec("bs4") is None:
        print("the api docs and Lua typings were not refreshed: they need beautifulsoup4 "
              "(pip install beautifulsoup4)")
        return 1
    from . import docsfetch
    return docsfetch.main()


addons.offer("after_rebuild", refresh_api_docs)

# where the modules sit among the server's toolsets
addons.offer("toolsets", {
    "docs": ("api", "docscheck"),
    "lua": ("lua", "luacheck", "pluginlint", "lsp", "luaast", "lab", "labtrace"),
})
addons.offer("no_baseline", {
    "rs3_lab_run": "its answers are the known answers in test_lab_runner, on a fixture made for them",
    "rs3_lab_query": "asks about a run recorded moments before; test_lab_recorder holds its answers",
    "rs3_lsp": "runs the language server over a whole workspace, which is the better part of a "
               "minute and reports on whatever lua is in the tree that day. Both make a fixed "
               "baseline noise. The parsing is what could break, and the add-on's tests drive that "
               "on a fixture instead",
})
addons.offer("side_effects", {
    "rs3_lsp": {"readOnlyHint": False, "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False},
})
addons.offer("soak_skip", {
    "rs3_lsp": "a run is most of a minute and spawns LuaLS. The add-on's tests drive its parsing "
               "on recorded output instead, which is where the false-zero traps live",
    "rs3_lab_run": "runs a plugin against the lab's client, which takes seconds and writes a run",
    "rs3_lab_query": "answers about the last lab run, so there is nothing to ask until one has gone",
})

# what the shared tools ask for
addons.offer("pool_sandbox", sandbox)
addons.offer("known_docs", known_docs)
addons.offer("typing_decls", api._all)
addons.offer("path_roots", [paths.PLUGINS])


def _lua_compiles(path):
    """For the handback check: a lua file an agent wrote has to compile."""
    luac = luacheck._luac()
    if not luac:
        return None
    found = luacheck._syntax_all([path], luac)
    return ("line %s: %s" % (found[0].get("line"), found[0].get("problem"))) if found else None


addons.offer("file_checks", {".lua": _lua_compiles})
addons.offer("programs", [("node", "rs3_lua_ast"), ("luac", "rs3_lua_check"),
                          ("lua-language-server", "rs3_lsp")])

addons.offer("flows_before", lambda: [
    ("shipping a plugin", [
        ("rs3_plugin_lint", "keys the client ignores, missing deps, http with no urls"),
        ("rs3_lua_check", "it has to compile, and the sandbox traps luac cannot see"),
    ]),
])
addons.offer("flows_after", lambda: [("the client api doing something the typings don't say", [
    ("rs3_api_gotchas", "read-vs-event asymmetry and the rest, each one observed"),
    ("rs3_api", "the declaration, with any gotcha attached"),
    ("rs3_lua", "sandbox traps and working recipes, by topic"),
])])


def _resources():
    from rs3mcp.resources import MD, tool_text
    return {
        "static": {
            "rs3://guide/api-gotchas": (
                "api gotchas", "Where the typings are wrong",
                "Every entry is something a running client did, with what was observed. When one "
                "contradicts the typings, the observation wins.",
                MD, lambda: tool_text("rs3_api_gotchas")),
        },
        "templates": [
            ("rs3://docs/{page}", "client api docs",
             "One page of the RuneScape client API docs, fetched from developer.runescape.com, by "
             "its path with dots, eg core.events or game.inventory.", MD,
             "rs3://docs/", _doc_page),
        ],
    }


addons.offer("resources", _resources)


def _typings():
    from . import docstypes
    return os.path.isfile(docstypes.MODEL)


def _plugins():
    if not os.path.isdir(paths.PLUGINS):
        return False
    return any(os.path.isfile(os.path.join(paths.PLUGINS, d, "plugin.json")) for d in os.listdir(paths.PLUGINS))


addons.offer("needs", {
    "typings": (_typings, "the Lua typings, generated from the docs (rs3 rebuild, or rs3api/docsfetch.py)"),
    "plugins": (_plugins, "installed plugins (RS3_PLUGINS)"),
    "lua": (lambda: bool(shutil.which("lua")), "lua on PATH"),
    "node": (lambda: bool(shutil.which("node")), "node on PATH"),
})

# the owner's half: harvests, recordings, lessons, worlds; not in a shipped copy
if os.path.isfile(os.path.join(HERE, "personal.py")):
    from . import personal  # noqa: E402
    PRIVATE_MODULES = personal.PRIVATE_MODULES
