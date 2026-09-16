# AI apps

[All docs](README.md) › Using it

On this page
- [What setup does to a file](#what-setup-does-to-a-file)
- [The apps](#the-apps)
  - [Claude Code](#claude-code)
  - [Claude Desktop](#claude-desktop)
  - [Cursor, Windsurf, Cline, Gemini CLI, LM Studio](#cursor-windsurf-cline-gemini-cli-lm-studio)
  - [VS Code](#vs-code)
  - [Zed](#zed)
  - [Continue](#continue)
  - [Codex CLI, and the ChatGPT desktop app](#codex-cli-and-the-chatgpt-desktop-app)
  - [Goose](#goose)
- [Any other app](#any-other-app)
- [Without an app](#without-an-app)

`rs3 setup` adds rs3cache to your AI apps for you:

```
rs3 setup              shows which apps you have, and which already have rs3cache
rs3 setup vscode       adds it to one app
rs3 setup all          adds it to every app you have
```

Restart the app afterwards.

The rest of this page shows what setup writes for each app. You only need it if you want to check
the change or add rs3cache by hand. These were checked against each app's own docs on 2026-09-13.
If an app stops finding rs3cache after an update, its config format may have changed.

## What setup does to a file

- It only adds or updates the `rs3cache` entry. Nothing else in the file changes.
- Before it changes a file for the first time, it saves a copy next to it called
  `<file>.before-rs3cache`.
- Running it twice is safe. The second time does nothing.
- Comments in a settings file are kept. Zed's settings often have them.
- If it can't edit a file safely, it leaves the file alone and prints the entry for you to paste in.
  Goose configs written as `extensions: {}` on one line are the usual case.
- If you've set `RS3MCP_GAME_CACHE`, `RS3MCP_ADDONS` or `RS3MCP_DATA`, setup copies them into the entry.

## The apps

`~` means your user folder. `%APPDATA%` is `~\AppData\Roaming`.

In the examples, swap in your own Python and your own rs3cache folder.

### Claude Code

Setup runs this for you:

```
claude mcp add --scope user rs3cache -- <python> <rs3-mcp.py>
```

If the `claude` command isn't installed, it adds the entry to `~\.claude.json` under `mcpServers`
instead.

### Claude Desktop

File: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{ "mcpServers": { "rs3cache": { "command": "C:/Python312/python.exe", "args": ["C:/path/rs3cache/rs3-mcp.py"] } } }
```

### Cursor, Windsurf, Cline, Gemini CLI, LM Studio

These use the same format as Claude Desktop, in these files:

| App | File |
|---|---|
| Cursor | `~\.cursor\mcp.json` |
| Windsurf | `~\.codeium\windsurf\mcp_config.json` |
| Cline | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |
| Gemini CLI | `~\.gemini\settings.json` |
| LM Studio | `~\.lmstudio\mcp.json` |

In LM Studio, only models that support tool use can use rs3cache. Small local models also get
confused by a long list of tools. You can show them fewer with `RS3_TOOLSETS` (see
[settings](settings.md)).

### VS Code

File: `%APPDATA%\Code\User\mcp.json`. Copilot uses it in agent mode.

VS Code's format is a little different. The list is called `servers`, and each entry needs a `type`:

```json
{ "servers": { "rs3cache": { "type": "stdio", "command": "C:/Python312/python.exe", "args": ["C:/path/rs3cache/rs3-mcp.py"] } } }
```

You can also put the same thing in a project's `.vscode/mcp.json` to use it in just that project.

### Zed

File: `%APPDATA%\Zed\settings.json` on Windows, `~/.config/zed/settings.json` on Mac and Linux.
The entry goes under `context_servers`:

```json
{ "context_servers": { "rs3cache": { "command": "C:/Python312/python.exe", "args": ["C:/path/rs3cache/rs3-mcp.py"] } } }
```

### Continue

Continue gets a file of its own: `~\.continue\mcpServers\rs3cache.yaml`. It only uses tools in agent
mode.

```yaml
name: rs3cache
version: 0.0.1
schema: v1
mcpServers:
  - name: rs3cache
    command: "C:/Python312/python.exe"
    args:
      - "C:/path/rs3cache/rs3-mcp.py"
```

If Continue doesn't pick it up, copy the file into a `.continue/mcpServers/` folder in your project.
That's where Continue's own docs put it.

### Codex CLI, and the ChatGPT desktop app

File: `~\.codex\config.toml`. The ChatGPT desktop app reads the same file, so this covers both.

```toml
[mcp_servers.rs3cache]
command = "C:/Python312/python.exe"
args = ["C:/path/rs3cache/rs3-mcp.py"]
```

ChatGPT in a browser can't use rs3cache. It can only reach servers on the internet, and rs3cache runs
on your computer.

### Goose

File: `%APPDATA%\Block\goose\config\config.yaml` on Windows, `~/.config/goose/config.yaml` on Mac and
Linux. The entry goes under `extensions`:

```yaml
extensions:
  rs3cache:
    name: rs3cache
    type: stdio
    cmd: "C:/Python312/python.exe"
    args: ["C:/path/rs3cache/rs3-mcp.py"]
    enabled: true
    timeout: 300
```

## Any other app

Give it the same command: your Python, then the path to `rs3-mcp.py`. Most apps use the
`mcpServers` format shown for Claude Desktop. For the technical details, see
[the MCP server](mcp.md).

## Without an app

You can use every tool from a terminal too. See [the rs3 command](cli.md).

---

Previous: [Commands](commands.md) · Next: [Tools](tools.md)
