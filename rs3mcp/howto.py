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
    ("a var behaving oddly", [
        ("rs3_var", "the varbit, its base, and its bits"),
        ("rs3_script_refs", "kind=varbit: every script that touches it"),
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

# habits that have cost time, kept to one line each
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


def instructions():
    """The text handed to a client at initialize."""
    out = ["rs3cache: the RS3 game cache, decoded, and the checks that keep it honest.",
           "", "What to do, in order:"]
    for flow in _all_flows():
        what, steps = flow[0], flow[1]
        out.append("")
        out.append("  " + what)
        for tool, why in steps:
            out.append("    %-20s %s" % (tool or "-", why))
        if len(flow) > 2:
            out.append("    %-20s %s" % ("avoid", flow[2]))
    # Two arguments every tool takes, handled in the dispatcher rather than in 49 schemas. Said
    # once here because a listing is paid per session and this is not worth 49 copies of.
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
