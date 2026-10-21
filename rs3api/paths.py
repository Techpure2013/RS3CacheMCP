"""Where the plugin project's pieces are, for the add-ons that read them.

The plugin project holds the client typings and the configs harvested off a live client. The
plugins folder is the one the client loads from. All have somebody's defaults and all can be moved.
"""

import os

from rs3mcp.common import BASE

# the folder of projects around rs3cache
WORKSPACE = os.environ.get("RS3MCP_WORKSPACE") or os.path.dirname(BASE)

def _project():
    """RS3MCP_PROJECT, else the folder beside rs3cache that holds the harvests (RS3_API_INFO)."""
    if os.environ.get("RS3MCP_PROJECT"):
        return os.environ["RS3MCP_PROJECT"]
    try:
        beside = sorted(os.listdir(WORKSPACE))
    except OSError:
        beside = []
    for d in beside:
        if os.path.isdir(os.path.join(WORKSPACE, d, "RS3_API_INFO")):
            return os.path.join(WORKSPACE, d)
    return os.path.join(WORKSPACE, "plugin-project")


PROJECT = _project()
TYPINGS = os.path.join(PROJECT, "shared", "types")
API_INFO = os.path.join(PROJECT, "RS3_API_INFO")
PLUGINS = os.environ.get("RS3_PLUGINS") or os.path.join(os.path.expanduser("~"), "RS3Plugins")
QUEST_HELPER = os.path.join(WORKSPACE, "Quest_Helper")

