"""What the add-on's modules imported before the core and add-on were split apart.

Several modules here were restored from a copy taken on 2026-09-15, before rs3api existed as its
own package, and they still say `from .common import TYPINGS` or `from .common import DB`. The
names moved: the paths went to rs3api.paths and the database helpers stayed in rs3mcp.common.
Everything public from both is re-exported here under the old name so those modules load.

A bridge, not a home. A module restored from that copy is eight days behind whatever it was when
it was deleted, and moving its imports to rs3api.paths or rs3mcp.common is part of bringing it
back up to date.
"""

from rs3mcp import common as _core
from rs3api import paths as _paths

# named one by one, so gone names raise
for _src in (_core, _paths):
    for _name in dir(_src):
        if not _name.startswith("_"):
            globals()[_name] = getattr(_src, _name)

del _src, _name
