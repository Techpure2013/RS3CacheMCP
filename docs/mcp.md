# The MCP server

[All docs](README.md) › How it works

`rs3-mcp.py` is the server AI apps run. It only needs Python. Apps talk to it over MCP (protocol
version 2025-06-18), sending JSON-RPC messages through stdin and stdout.

It never changes your game data. The database is opened read-only, and `rs3_query` only accepts a
single SELECT. Any tool that runs a program, saves a file or goes online is marked as doing so.

## What apps get

**A short guide.** When an app connects, the server sends a guide to which tool to use for common
jobs. It lives in `rs3mcp/howto.py`. Every tool it names is checked against the real tool list, so
renaming a tool can't leave the guide pointing at nothing.

**Tools.** Apps get every tool in the groups `RS3_TOOLSETS` allows, with a description, its
arguments, and hints about whether it only reads. `rs3_wiki` is marked as going online.
[Tools](tools.md) lists them all.

**Resources.** Some apps attach context instead of calling tools. For those there's:

| URI | What it is |
|---|---|
| `rs3://guide/tools` | the same guide as above |
| `rs3://schema/cache` | the database's tables and how many rows each has |
| `rs3://script/{id}` | one clientscript, as readable code |

Add-ons can add more ([add-ons](addons.md)).

## Keeping answers small

Every tool takes two extra arguments:

- `pick` gets just one part of an answer. `pick=counts.npcs` returns a single number instead of the
  whole answer. Use a number to pick from a list. If nothing is there, it tells you what is.
- `outDir` saves the answer to `<outDir>/<tool>.json`. The app gets back the file's path and a quick
  outline of what's in it, so it can decide whether to open it.

Answers leave out empty values. Long answers come in pages, and tell you how to get the next one.

## Errors

If a tool fails, the app gets the reason back as the answer. For protocol errors, the server uses the
standard codes: -32601 for a method it doesn't have, -32002 for a resource that doesn't exist, and
-32603 for anything else.

## Adding a tool

Tools come in families, one module per family in `rs3mcp/`. Each module has a `TOOLS` list:

```python
TOOLS = [
    {"name": "rs3_example", "fn": tool_example,
     "description": "One sentence saying what it answers. More when a wrong guess is likely.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "what the id is of"}}},
     "examples": [{"id": "4151"}]},
]
```

Then:

1. Import the module in `rs3-mcp.py` and add it to `FAMILIES`.
2. Add its name to a group in `SETS`. If you don't, it ends up in `meta` and a test reminds you.
3. If the tool writes files, runs a program or goes online, add it to `SIDE_EFFECTS`.
4. Run `rs3 docs` to update [tools](tools.md).
5. Run the tests ([testing](testing.md)).

`fn` gets the arguments as a dict and returns anything that can be turned into json. `examples` are
used by `rs3 help` and the tools page, and aren't sent to apps.

The full tool list apps receive has a size limit, and a test checks it. If a new tool doesn't fit,
raise the limit on purpose.

---

Previous: [Models](models.md) · Next: [Add-ons](addons.md)
