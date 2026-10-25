# rs3cache documentation

rs3cache reads the RuneScape 3 game cache on your computer and turns it into a database you can ask
questions. You can use it yourself with the `rs3` command, or let an AI app like Claude or Cursor use
it.

Everything stays on your computer. Two things reach the internet: `rs3_wiki`, when you ask it for a
wiki page, and a check twice an hour asking GitHub whether there is a newer version of rs3cache. The
check sends nothing about you, and `RS3_UPDATES=off` stops it.

## Using it

| Page | What's in it |
|---|---|
| [Installing](install.md) | what you need, setting it up, updating after a game update, removing it |
| [The rs3 command](cli.md) | using the tools from a terminal |
| [Commands](commands.md) | every other script, for building by hand, testing and drawing pictures |
| [AI apps](apps.md) | adding rs3cache to Claude, Cursor, VS Code, LM Studio and others |
| [Tools](tools.md) | every tool and what you can give it |
| [The database](database.md) | every table in the database |
| [The scrying pool](scrying-pool.md) | a live page showing what your AI apps are asking |
| [Settings](settings.md) | environment variables, and showing apps fewer tools |

## How it works

| Page | What's in it |
|---|---|
| [How the database is built](rebuild.md) | what a rebuild does, step by step |
| [Config specs](specs.md) | the files that describe how each kind of config is read |
| [Clientscripts](clientscripts.md) | turning the game's scripts back into readable code |
| [Models](models.md) | reading 3D models |
| [The MCP server](mcp.md) | what AI apps get from rs3cache |
| [Add-ons](addons.md) | adding your own tools |
| [Testing](testing.md) | the tests, and which to run when |
| [Known gaps](known-gaps.md) | what doesn't work yet |

## What's in the folder

```
rs3cache/
  rs3.py, rs3.cmd, rs3       the rs3 command
  rs3-mcp.py                 the server AI apps run
  rebuild-cache.py           builds the database
  build_db.py                one step of the build
  rs3-cache.py               quick lookups and sprite sheets
  rs3-icon.py                draws item icons
  rs3-areas.py               redoes loc areas without a full rebuild
  model_png.py, model_obj.py   saves a model as a picture or an obj file
  rs3mcp/                    the tools and the code that reads the cache
    specs/                   how each kind of config is read
    opcodes/                 what the clientscript reader needs
    test/                    the tests
  data/                      everything rs3cache builds: the database, scripts, logs
  docs/                      these pages
```

[Commands](commands.md) shows how to run each script.

`tools.md` and `database.md` are written automatically by `rs3 docs`, which also runs after every
rebuild. If you edit them by hand, your changes will be overwritten.
