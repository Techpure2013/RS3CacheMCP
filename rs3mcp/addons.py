"""Room for tools that belong to one machine and not to everyone.

The shared tools never import an add-on by name. An add-on is a package beside rs3mcp whose name
starts with rs3. It registers its tool families and offers extra answers under
agreed names. A shared tool asks for a name and carries on when nobody offered.

    from rs3mcp import addons
    addons.offer("npc_seen", npc_seen)               in the add-on
    fn = addons.first("npc_seen")                    in a shared tool, None when absent

Every such package is loaded unless RS3MCP_ADDONS names which, comma separated, or "none". An add-on
that finds what it needs missing sets ABSENT to why and is left out. One that sets PRIVATE is kept
out of the written docs.
"""

import importlib
import os

_OFFERS = {}
_STATE = {"loaded": False, "packages": []}
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def offer(name, value):
    """Make a value available under a name. Offering twice keeps both, in order."""
    _OFFERS.setdefault(name, []).append(value)


def named():
    """The add-on packages to load: RS3MCP_ADDONS when set, else every rs3 package beside rs3mcp."""
    raw = os.environ.get("RS3MCP_ADDONS")
    if raw is not None and raw.strip():
        if raw.strip().lower() in ("none", "off"):
            return []
        return [n for n in raw.replace(",", " ").split() if n]
    return sorted(d for d in os.listdir(BASE)
                  if d.startswith("rs3") and d != "rs3mcp" and os.path.isfile(os.path.join(BASE, d, "__init__.py")))


def load():
    """Import the add-on packages once. Returns the ones that loaded, which may be none."""
    if _STATE["loaded"]:
        return _STATE["packages"]
    _STATE["loaded"] = True
    for name in named():
        try:
            pkg = importlib.import_module(name)
        except ModuleNotFoundError as e:
            # absent is fine, a failed import is not
            if e.name != name:
                raise
            continue
        if not getattr(pkg, "ABSENT", None):
            _STATE["packages"].append(pkg)
    return _STATE["packages"]


def first(name):
    load()
    got = _OFFERS.get(name)
    return got[0] if got else None


def every(name):
    load()
    return list(_OFFERS.get(name, ()))


def families():
    """The tool families the add-ons register, empty without any."""
    return [fam for pkg in load() for fam in getattr(pkg, "FAMILIES", ())]


def private_modules():
    """Module name prefixes of the add-ons marked PRIVATE."""
    return tuple(pkg.__name__ + "." for pkg in load() if getattr(pkg, "PRIVATE", None))
