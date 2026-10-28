"""
What to do with these tools, in the order to do it.

A list of tools each described on its own says nothing about which to reach for first. This is the part
that was missing: the procedures, sent once per session through the MCP instructions field, which
costs nothing against the tool listing.

Every tool named in a step is checked against the registry at import. A renamed tool breaks the
selftest here rather than leaving an instruction pointing at nothing.

An add-on's flows and rules arrive through rs3mcp.addons.
"""

from . import addons

FLOWS = [
    ("finding something in the cache", [
        ("rs3_lookup", "by id or name, any kind"),
        ("rs3_trace", "which configs point at a thing, and what it points at"),
        ("rs3_iface", "text= finds the component drawing words you can see"),
        ("rs3_dbtable", "text= searches the data tables nothing else reaches"),
        ("rs3_script", "clientscripts, when the cache will not say"),
    ]),
    ("an interface that reads as empty", [
        (None, "it builds its children at runtime, so the cache has only the skeleton"),
        ("rs3_iface", "id=: a dump taken off a running client arrives as a live block"),
        (None, "dump one with quest-dev: `qd ifacedump <id>`, or the Quest Helper editor's "
               "ifacefind, whose dumps need RS3_IFACE_LOG pointing at its rec-iface.jsonl"),
        ("rs3_iface", "text=: runtime text is in no cache, so this is the only way to it"),
    ]),
    ("a var behaving oddly", [
        ("rs3_var", "the varbit, its base, and its bits"),
        ("rs3_script_refs", "kind=varbit: every script that touches it"),
    ]),
    ("which var tracks a quest, and at what values", [
        ("rs3_quest", "by title, config name or config id: the var, and where it starts and completes"),
        ("rs3_quest_var", "the other way round, from a varbit id, a base var id or a name"),
        (None, "the cache holds a quest's id and name only, so the rest is a shipped table; "
               "the mask and shift come from your own cache"),
        ("rs3_var", "the bits themselves, when you are reading the base var live"),
    ]),
    ("after the game cache updates", [
        (None, "rs3 rebuild. It refuses to install a worse database"),
        ("rs3_cache_diff", "what the update moved"),
    ]),
    ("before saying it is done", [
        (None, "python rs3-mcp.py --selftest"),
        (None, "python rs3-mcp.py --diff"),
        ("rs3_drift", "every written number against the data"),
        (None, "python rs3mcp/test/soak.py, twelve minutes, every tool on real ids"),
    ]),
]

# habits that have cost time, one line each
RULES = [
    "Numbers written in prose go stale. rs3_drift checks them; add a claim rather than a comment.",
    "An encoding that reads the same at count 1 validates the wrong spec. Test the multi-valued records.",
    "Byte consumption only proves a parse was self consistent. Check one field against something outside the record.",
    "A tool named in a doc is a dangling reference waiting to happen. These are checked at import.",
]


def _all_flows():
    out = []
    for offered in addons.every("flows_before"):
        out.extend(offered())
    out.extend(FLOWS)
    for offered in addons.every("flows_after"):
        out.extend(offered())
    return out


def _all_rules():
    out = list(RULES)
    for offered in addons.every("rules"):
        out.extend(offered())
    return out


def _tools_named():
    return sorted({t for flow in _all_flows() for t, _ in flow[1] if t})


def _set_names():
    """The toolset names, read off the server rather than listed again here."""
    try:
        from .common import _run_server
        return sorted(_run_server()["SETS"])
    except Exception:
        return []


# Claude Code's cut for server instructions
CLIENT_CUT = 2048
DETAILS = "Details, task by task:"


def index():
    """One line per task naming its tools in order, sized to survive CLIENT_CUT."""
    out = ["rs3cache: the RS3 game cache, decoded, and the checks that keep it honest.",
           "The full procedures follow this index and are the resource rs3://guide/tools.", ""]
    for flow in _all_flows():
        named = []
        for tool, _why in flow[1]:
            if tool and tool not in named:
                named.append(tool)
        out.append("  %s: %s" % (flow[0], ", ".join(named) or "see details"))
    out.append("")
    out.append("Any call takes pick=a.b.c for one part, outDir=<path> to write it to a file.")
    return out


def instructions(compact=False):
    """The text handed to a client at initialize. Compact is the task index and how to reach a tool."""
    if compact:
        return "\n".join(index()[:1] + [
            "Three tools reach the rest: rs3_find {task} names the best, rs3_how {tool} gives its "
            "arguments, rs3_run {tool, args} runs it.", ""] + index()[3:])
    out = index() + ["", DETAILS]
    for flow in _all_flows():
        what, steps = flow[0], flow[1]
        out.append("")
        out.append("  " + what)
        for tool, why in steps:
            out.append("    %-20s %s" % (tool or "-", why))
        if len(flow) > 2:
            out.append("    %-20s %s" % ("avoid", flow[2]))
    # two arguments handled in the dispatcher, said once
    out.append("")
    out.append("Any tool, any call:")
    out.append("  pick=a.b.c           one part of the answer by dotted path, digits index a list. "
               "pick=counts.npcs is a number, not fourteen thousand characters. A miss says what "
               "is there instead")
    out.append("  outDir=<path>        write the answer to a json file there and hand back only "
               "its shape, for the ones that run to thousands of lines")
    out.append("  start=               a paged answer gives nextStart; pass it back for the next "
               "window")
    out.append("")
    out.append("RS3_TOOLSETS in the environment lists only the sets named, which is most of what a "
               "session pays before asking anything: " + ", ".join(_set_names()))
    out.append("")
    out.append("Learned the hard way:")
    for r in _all_rules():
        out.append("  " + r)
    return "\n".join(out)
