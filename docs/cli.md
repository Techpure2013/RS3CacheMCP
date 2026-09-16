# The rs3 command

[All docs](README.md) › Using it

On this page
- [Commands](#commands)
- [Naming a tool](#naming-a-tool)
- [Arguments](#arguments)
- [Output](#output)
- [Exit codes](#exit-codes)
- [Scripts](#scripts)

`rs3` lets you use the same tools the AI apps use, from a terminal.

Run it from the rs3cache folder. In PowerShell, type `.\rs3` instead. To run it from any folder, add
the rs3cache folder to your PATH. `python rs3.py` works too.

## Commands

| Command | What it does |
|---|---|
| `rs3` | shows a few examples to get started |
| `rs3 tools` | lists every tool. Add a group name to list just that group |
| `rs3 help <tool>` | explains a tool and what you can give it |
| `rs3 <tool> ...` | runs a tool. `rs3 <tool> --help` is the same as `rs3 help <tool>` |
| `rs3 doctor` | checks your install and tells you what to fix |
| `rs3 rebuild` | builds the database. Add `--force` to build even if the game hasn't updated, `--check` to only check, or `--no-swap` to build without installing it |
| `rs3 setup` | adds rs3cache to your AI apps (see [AI apps](apps.md)) |
| `rs3 pool` | opens the scrying pool in your browser |
| `rs3 docs` | rewrites the tools and database pages of these docs |

`doctor`, `rebuild` and `setup` work before you've built the database. Everything else needs it.

## Naming a tool

You can leave off the `rs3_` at the start, and use dashes or underscores. `rs3 lookup`,
`rs3 rs3_lookup` and `rs3 script-refs` all work. If you get a name wrong, it suggests the closest
ones.

## Arguments

You can write them three ways, and mix them:

```
rs3 lookup npc Wolf
rs3 lookup kind=npc name=Wolf
rs3 lookup --kind npc --name Wolf
```

Plain words usually just work:

- Words fill the arguments the tool needs, in order. `rs3 lookup npc Wolf` means kind `npc`, name
  `Wolf`.
- Extra words become the name or search, so you don't need quotes: `rs3 lookup obj Abyssal whip`
  looks up one item.
- A number on its own becomes the id: `rs3 lookup obj 4151`.

If a value has characters your shell treats specially, put it in quotes. For yes/no arguments,
`true`, `yes` and `1` all mean yes. For a list, separate the values with commas.

Add `--json` to get the answer as json instead of text. `pick=` and `outDir=` also work here; see
[the MCP server](mcp.md).

## Output

Answers come back as readable text:

- simple values on one line each
- lists of records as a table, with long cells cut short and marked with `~`
- anything nested as indented blocks

Empty values are left out. Colour turns off when you send the output to a file, or when `NO_COLOR`
is set.

## Exit codes

`rs3` exits with 0 when it answers. It exits with 1 if the tool doesn't exist, the call fails, or
`rs3 doctor` finds something you have to fix.

## Scripts

To call a tool from another program, `python rs3-mcp.py <tool> key=value` prints the answer as json.

The test commands are on [testing](testing.md), and every other script is on
[commands](commands.md).

---

Previous: [Installing](install.md) · Next: [Commands](commands.md)
