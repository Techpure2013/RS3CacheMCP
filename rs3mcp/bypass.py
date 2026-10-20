"""Notice when a shell command asks the cache something a tool already answers.

Going round the tools is how they stay unexercised. Every raw sqlite query against rs3-cache.db is
a question rs3_lookup or rs3_var would have answered, and a chance to find out that it answers it
badly. The point is not tidiness, it is that a tool nobody uses is a tool nobody debugs.

Warns, never blocks. Building the server means querying it directly, and a hook that refused would
be wrong most of the day. It names the tool that would have answered instead, so the useful case
costs nothing to ignore and the lazy one is one copy-paste from being a real call.
"""

import re

# (what the command does, the tool answering it)
ROUTES = [
    (re.compile(r"type\s*=\s*['\"]npcs['\"]", re.I), "rs3_lookup kind=npc"),
    (re.compile(r"type\s*=\s*['\"]objects['\"]", re.I), "rs3_lookup kind=loc"),
    (re.compile(r"type\s*=\s*['\"]items['\"]", re.I), "rs3_lookup kind=obj"),
    (re.compile(r"type\s*=\s*['\"](structs|enums|seq\w*)['\"]", re.I), "rs3_lookup"),
    (re.compile(r"\bfrom\s+varbit\b|\bvarbit\s+where\b", re.I), "rs3_var"),
    (re.compile(r"\bmorph_edge\b|\bmorphs(Wide)?\b", re.I), "rs3_family, or rs3_lookup for parents"),
    (re.compile(r"\bloc_section\b", re.I), "rs3_lookup, which reports placement"),
    (re.compile(r"\bparam_use\b|\bparam_type\b", re.I), "rs3_trace"),
    (re.compile(r"\benum_entry\b", re.I), "rs3_lookup kind=enum"),
    (re.compile(r"\bdbrows\b|\bdbtables\b", re.I), "rs3_dbtable"),
    (re.compile(r"\bscript_ref\b|\bscript_token\b", re.I), "rs3_script_refs"),
    (re.compile(r"\binterfaces['\"]?\s*(and|where|$)", re.I), "rs3_iface"),
]


def _routes():
    # an add-on names its own tables and tools
    from . import addons
    extra = [(re.compile(rx, re.I), tool) for offered in addons.every("bypass_routes")
             for rx, tool in offered]
    return extra + ROUTES

# what counts as going round the tools
DIRECT = re.compile(
    r"rs3-cache\.db|rs3-index\.db"
    r"|from\s+rs3mcp[.\s]|import\s+rs3mcp\b"
    r"|rs3mcp\.common\s+import\s+connect",
    re.I)

# building or testing the server is the job
BUILDING = re.compile(
    r"rs3-mcp\.py\s+--(test|golden|diff|selftest)"
    r"|rs3mcp[/\\]test[/\\]"
    r"|buildindex|rebuild-cache|build_db|rs3mcp[./\\]extract|rs3mcp[/\\]ops"
    r"|--record|--push|--pull",
    re.I)


def advise(command):
    """What to say about this command, or None when there is nothing to say."""
    if not command or not DIRECT.search(command):
        return None
    if BUILDING.search(command):
        return None

    for rx, tool in _routes():
        if rx.search(command):
            return ("This reads the cache directly. %s answers it, and using it is how its bugs "
                    "get found." % tool)
    return ("This reads the cache directly. There is probably a tool for it: rs3_lookup, "
            "rs3_var or rs3_trace, and using one is how its bugs get found.")
