"""What a check needs beyond RuneScape and a built database.

A clean install has the game cache and what `rs3 rebuild` makes of it. Everything here is something
a machine can have on top of that. A check that reads one names it, and where it is missing the check
is skipped with the reason printed, instead of failing on a machine that was never going to have it.
Where it is present the check runs, so nothing here turns a check off on the machine that has it.

An add-on offers the needs of its own checks under "needs", in the same shape.
"""

import os

from rs3mcp import addons, common

NEEDS = {
    "name lists": (lambda: common.name_lists() is not None,
                   "the config name lists (RS3MCP_NAMES, a folder of `Vars - <type>.csv`)"),
    "add-on": (lambda: bool(addons.load()), "an add-on (RS3MCP_ADDONS)"),
    "previous build": (lambda: os.path.exists(common.DB + ".bak"),
                       "a previous database, which only a second rebuild leaves"),
}


def _all():
    out = dict(NEEDS)
    for offered in addons.every("needs"):
        out.update(offered)
    return out


def missing(*names):
    """What of these is not on this machine, as sentences. Empty when all of it is."""
    known = _all()
    return [known[n][1] if n in known else "%s, which an add-on provides" % n
            for n in names if n not in known or not known[n][0]()]
