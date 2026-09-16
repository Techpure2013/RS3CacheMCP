# Add-ons

[All docs](README.md) › How it works

An add-on lets you add your own tools to rs3cache without changing rs3cache itself. It's useful for
tools that only make sense for you, like ones that read your own project or your own database.

rs3cache doesn't come with any add-ons.

## How it works

An add-on is a Python package in the rs3cache folder, next to `rs3mcp`. Its name has to start with
`rs3`. Drop the folder in and the server loads it the next time it starts.

To choose which add-ons load, set `RS3MCP_ADDONS` to their names, separated by commas. Set it to
`none` to load none.

An add-on package has:

- `FAMILIES`: a list of modules, each with a `TOOLS` list like the built-in ones
  ([adding a tool](mcp.md#adding-a-tool)).
- `ABSENT` (optional): set this to a reason if what the add-on needs isn't on this computer. The
  add-on then isn't loaded.
- `PRIVATE` (optional): set this to a reason to keep its tools off the [tools](tools.md) page.
- Offers: things it hands to rs3cache by name, with `rs3mcp.addons.offer(name, value)`. rs3cache
  asks for them by name, and works fine if nothing was offered.

```python
# in the add-on
from rs3mcp import addons
addons.offer("toolsets", {"cache": ("mymodule",)})

# in rs3cache
sets = addons.every("toolsets")      # empty if there are no add-ons
```

## Offers

For the server and the tests:

| Offer | What it's for |
|---|---|
| `toolsets` | which group each of its tools goes in |
| `side_effects` | marks its tools that write files or go online |
| `resources` | extra MCP resources |
| `flows_before`, `flows_after`, `rules` | extra sections for the guide apps get |
| `tests`, `claims`, `selftest` | its own tests |
| `golden_calls`, `golden_home`, `no_baseline` | answers to save for `--diff`, where to keep them, and tools to leave out |
| `path_roots` | folders its saved answers mention, so they match on another computer |
| `soak`, `soak_skip` | its part of the soak test, and tools to leave out of it |
| `needs` | what its tests need, so they're skipped when it's missing |
| `programs` | extra programs for `rs3 doctor` to look for |
| `scripts` | its own scripts in the rs3cache folder, so the docs test doesn't expect them on [commands](commands.md) |
| `bypass_routes` | database tables its tools answer questions about |

For the tools, which use them to add to their answers:

| Offer | Used by |
|---|---|
| `name_lists` | the rebuild and `rs3 doctor`. A folder of `Vars - *.csv` name lists, like `RS3MCP_NAMES` |
| `api_names` | the rebuild, to fill the database's `api` column |
| `interface_dumps` | `rs3_iface`, to show interfaces as they looked in the game |
| `client_fields`, `npc_seen` | `rs3_trace`, for details from outside the cache |
| `quest_vars`, `quest_var`, `quests_using` | `rs3_var` and `rs3_trace`, for which quests use a var or a thing |
| `pool_sandbox` | the scrying pool's test runs. Without it, the page hides them |

---

Previous: [The MCP server](mcp.md) · Next: [Testing](testing.md)
