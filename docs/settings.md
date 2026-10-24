# Settings

[All docs](README.md) › Using it

You don't need to change any settings. Everything works out of the box.

If you do want to change something, these are environment variables. AI apps start rs3cache
themselves, so set them before you open the app. If you set `RS3MCP_GAME_CACHE`, `RS3MCP_DATA` or
`RS3MCP_ADDONS` before running `rs3 setup`, setup copies them into the app's config for you.

## Where things are

| Variable | Default | What it's for |
|---|---|---|
| `RS3MCP_GAME_CACHE` | `C:/ProgramData/Jagex/RuneScape` | where your game cache is |
| `RS3MCP_DATA` | the `data` folder in rs3cache | where the database and everything else rs3cache builds goes |
| `RS3MCP_NAMES` | the set in `rs3mcp/names` | a folder of name lists (`Vars - <type>.csv`). The cache stores no name for an interface, a var or a quest, so these fill them in. Set it only to use a newer set than the one that ships |

## Other settings

| Variable | What it does |
|---|---|
| `RS3_TOOLSETS` | shows apps only some groups of tools, e.g. `cache,scripts,game` |
| `RS3MCP_ADDONS` | which add-ons to load, separated by commas. `none` loads none. See [add-ons](addons.md) |
| `RS3_MONITOR` | `off` turns off the scrying pool |
| `RS3_MONITOR_PORT` | the scrying pool's port. The default is 7331 |
| `RS3MCP_USER` | your name on the scrying pool page |
| `RS3MCP_CONTACT` | how the wiki can contact you. The wiki asks tools to include this |
| `NO_COLOR` | set to anything to turn off colour in `rs3` |

## Showing fewer tools

Small local models can struggle with a long list of tools. You can show them fewer.

The easiest way is a file called `.rs3-toolsets` next to `rs3-mcp.py`. Put the groups you want on
one line, then restart the app. You can change it any time without changing your environment.
Lines starting with `#` are ignored.

```
# just what I need for var hunting
cache, game, scripts
```

The groups are `cache`, `docs`, `scripts`, `game` and `meta`, plus any your add-ons bring.
[Tools](tools.md) lists what's in each one.

The file wins over `RS3_TOOLSETS` if you set both.

---

Previous: [The scrying pool](scrying-pool.md) · Next: [How the database is built](rebuild.md)
