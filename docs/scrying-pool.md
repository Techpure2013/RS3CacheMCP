# The scrying pool

[All docs](README.md) › Using it

The scrying pool is a page in your browser that shows what your AI apps are doing with rs3cache, as
it happens. You can see every tool call, the answer, how big it was, and which calls failed.

It starts on its own whenever an AI app starts rs3cache. Open http://127.0.0.1:7331 to see it.

If no app is running, you can start it yourself:

```
rs3 pool
```

Keep that window open while you use the page. Press Ctrl+C to stop it.

## What it shows

- **Claude Code sessions, in full.** You see your prompts, every file read or edited, shell commands,
  web fetches, subagents and rs3cache calls, from the last 24 hours.
- **Other apps** like Cursor, VS Code and LM Studio. For these you only see their rs3cache calls and
  answers, because that's all rs3cache can see. Each session is named after the app.
- **Tools run from a terminal.** If a session runs `rs3 <tool>`, it shows up as a call to that tool.
- **Test runs**, if you have an add-on that provides them ([add-ons](addons.md)). The page reruns the
  test and shows each check pass or fail. Without one, this part is hidden.

Click a call to see what was asked and what came back. Very long answers are cut off at 8,000
characters.

## Privacy

Nothing is uploaded anywhere. The page only answers requests from your own computer, so other
websites can't read it.

For apps other than Claude Code, rs3cache keeps a log of their calls in `data\pool`. You can delete
that folder whenever you like.

## Settings

| Variable | What it does |
|---|---|
| `RS3_MONITOR=off` | turns the page off, and stops the logs |
| `RS3_MONITOR_PORT` | uses a different port instead of 7331 |
| `RS3MCP_USER` | the name the page calls you. Otherwise it uses your git name |

---

Previous: [The database](database.md) · Next: [Settings](settings.md)
