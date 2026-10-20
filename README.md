# rs3cache

rs3cache puts RuneScape 3's game data on your computer: every npc, item, loc, interface, var, model
and clientscript. It builds it all from the cache the game has already downloaded.

You can look things up yourself from a terminal. Or you can add it to an AI app like Claude, Cursor,
VS Code or LM Studio, and the AI can look things up for you.

Everything stays on your computer. The only thing that goes online is `rs3_wiki`, and only when you
ask it for a wiki page.

The full docs are in [docs](docs/README.md).

## What you need

- Windows, with RuneScape 3 installed. Log in at least once so the game downloads its cache.
- Python 3.10 or newer. When you install it, tick "Add python.exe to PATH".

That's it. `rs3 doctor` tells you about anything optional you might want.

## Setting it up

Open a terminal in this folder and run:

    rs3 doctor      checks everything is ready
    rs3 rebuild     builds the database (takes a few minutes the first time)
    rs3 setup       shows the AI apps on your computer

Then add rs3cache to your app, and restart the app:

    rs3 setup cursor
    rs3 setup all   adds it to every app it found

In PowerShell, type `.\rs3` instead of `rs3`. To run `rs3` from any folder, add this folder to your
PATH: open Start, search "Edit environment variables for your account", select Path, click New and
paste the folder's path.

When the game updates, run `rs3 rebuild` again. `rs3 doctor` tells you when it's time.

## Looking things up yourself

    rs3                          shows where to start
    rs3 tools                    lists every tool
    rs3 help lookup              explains a tool
    rs3 lookup npc Wolf          looks up an npc, loc, item, struct or enum by name
    rs3 lookup obj 4151          or by id
    rs3 var 12                   shows what a var holds
    rs3 script IF_SETHIDE        searches the clientscripts
    rs3 query "select id, display from config where type='items' limit 5"

Plain words usually just work. Add `--json` if you want the raw answer.

The database is a normal SQLite file at `data/rs3-cache.db`, so you can also open it in DB Browser
for SQLite or your own scripts.

## AI apps

| App | `rs3 setup` name | Where it's added |
|---|---|---|
| Claude Code | `claude-code` | through `claude mcp add` |
| Claude Desktop | `claude-desktop` | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `cursor` | `~\.cursor\mcp.json` |
| Windsurf | `windsurf` | `~\.codeium\windsurf\mcp_config.json` |
| VS Code (Copilot agent mode) | `vscode` | `%APPDATA%\Code\User\mcp.json` |
| Cline | `cline` | Cline's `cline_mcp_settings.json` |
| Continue | `continue` | `~\.continue\mcpServers\rs3cache.yaml` |
| Zed | `zed` | `%APPDATA%\Zed\settings.json` |
| Gemini CLI | `gemini` | `~\.gemini\settings.json` |
| Codex CLI and the ChatGPT desktop app | `codex` | `~\.codex\config.toml` |
| Goose | `goose` | `%APPDATA%\Block\goose\config\config.yaml` |
| LM Studio | `lmstudio` | `~\.lmstudio\mcp.json` |

Setup only adds the rs3cache entry. Nothing else in the file changes, and it saves a backup the
first time, called `<file>.before-rs3cache`. If it can't edit a file safely, it prints the entry for
you to paste in.

For any other app, give it your Python and the path to `rs3-mcp.py`:

    { "command": "python", "args": ["C:/path/to/rs3cache/rs3-mcp.py"] }

Small local models in LM Studio can get confused by a long list of tools. You can show them fewer
with `RS3_TOOLSETS` (see Settings below).

## The scrying pool

A page that shows what your AI apps are asking rs3cache, live. It's at http://127.0.0.1:7331 while an
app is using rs3cache, or run `rs3 pool` to open it yourself. Only your own computer can see it.

## Settings

You don't need any of these. They're environment variables. If you set the first three before
running `rs3 setup`, setup copies them into your app's config.

| Variable | What it does |
|---|---|
| `RS3MCP_GAME_CACHE` | where your game cache is, if it's not in `C:\ProgramData\Jagex\RuneScape` |
| `RS3MCP_DATA` | where to put the database, instead of the `data` folder here |
| `RS3MCP_ADDONS` | which add-ons to load, separated by commas, or `none` |
| `RS3MCP_NAMES` | a folder of `Vars - <type>.csv` name lists, to fill in config names |
| `RS3_TOOLSETS` | shows apps only some groups of tools, e.g. `cache,scripts` |
| `RS3_MONITOR` | `off` turns the scrying pool off |
| `RS3_MONITOR_PORT` | the scrying pool's port. The default is 7331 |

## For developers

rs3cache runs from a working copy in an `rs3cache` folder beside this repo (or wherever
`RS3CACHE_LIVE` says). This repo is the copy kept on GitHub, and `sync.py` keeps the two matching:

    python sync.py          shows what's different
    python sync.py --pull   copies the working copy in here
    python sync.py --push   copies this back to the working copy

To test, run `python rs3-mcp.py --selftest`. Tests that need something you don't have are skipped,
and it tells you which.

A rebuild won't install a new database if it reads less than the old one. Right now every config,
model, clientscript and sprite reads.

Every game update shuffles the clientscript instruction numbers. The rebuild works out the new ones
itself. See [clientscripts](docs/clientscripts.md).
