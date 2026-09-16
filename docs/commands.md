# Commands

[All docs](README.md) › Using it

On this page
- [rs3](#rs3)
- [The server](#the-server)
- [Building the database](#building-the-database)
- [Inside the decoders](#inside-the-decoders)
- [Pictures and models](#pictures-and-models)
- [Looking things up from the shell](#looking-things-up-from-the-shell)
- [Testing](#testing)
- [Keeping copies in step](#keeping-copies-in-step)

Every command rs3cache has. Most of the time you'll only need `rs3` ([the rs3 command](cli.md)). The
rest are for building by hand, digging into how the cache is read, testing, and drawing pictures.

Run them all from the rs3cache folder. Scripts in `rs3mcp` run either as a file or with `python -m`,
as shown.

## rs3

| Command | What it does |
|---|---|
| `rs3` | shows where to start |
| `rs3 tools [group]` | lists every tool, or one group |
| `rs3 help <tool>` | explains a tool |
| `rs3 <tool> [arguments]` | runs a tool. Add `--json` for the raw answer |
| `rs3 doctor` | checks your install |
| `rs3 rebuild [--force\|--check\|--no-swap]` | builds the database |
| `rs3 setup [app\|all]` | adds rs3cache to AI apps |
| `rs3 pool` | opens the scrying pool |
| `rs3 docs [folder]` | rewrites the tools and database pages, and every page's links |

## The server

```
python rs3-mcp.py                          starts the server. This is what AI apps run
python rs3-mcp.py <tool> key=value         runs one tool and prints json, for other programs
python rs3-mcp.py --selftest               tries most tools, then runs the tests
python rs3-mcp.py --test                   just the tests
python rs3-mcp.py --golden                 saves every tool's answers
python rs3-mcp.py --diff                   shows which answers changed (-v for every detail)
```

[Testing](testing.md) says which to run when.

## Building the database

```
python rebuild-cache.py                    same as rs3 rebuild: builds if the game has updated
python rebuild-cache.py --check            exits with 1 if a rebuild is due, 0 if not
python rebuild-cache.py --force            builds even if the game hasn't updated
python rebuild-cache.py --no-swap          builds and checks, but doesn't install
```

To run one step of the rebuild at a time ([how the database is built](rebuild.md)):

```
python -m rs3mcp.extract <stage> [type ...]                        reads configs and models into a folder
python build_db.py <stage> <csvdir> <out.db> [--carry <old.db>]    builds a database from that folder
python rs3mcp/buildindex.py                                        rebuilds the search index
python rs3-areas.py                                                redoes loc areas in the database
```

`rs3-areas.py` is handy when you change the area rules in `rs3mcp/areas.py` and don't want a full
rebuild.

To download lots of wiki pages at once, into the same store `rs3_wiki` uses:

```
python rs3mcp/wikiharvest.py --list                  lists the categories it knows
python rs3mcp/wikiharvest.py quests                  downloads one category, or several
```

It's polite to the wiki: one request a second, 50 pages a request. Pages you already have are
skipped, so you can stop it and pick up later.

## Inside the decoders

Config specs ([config specs](specs.md)):

```
python rs3mcp/ops.py verify <type>             reads every config of a type
python rs3mcp/ops.py decode <type> <id>        reads one
python rs3mcp/ops.py solve <type> <opcode>     suggests shapes for an unknown opcode
```

Models, sprites and map placements:

```
python -m rs3mcp.models <id> [...]             prints a model's summary
python -m rs3mcp.models verify [n]             reads every model, or the first n
python -m rs3mcp.sprites <id> [...] [-o folder]   saves sprites as png files
python -m rs3mcp.sprites verify [n]            reads every sprite, or the first n
python -m rs3mcp.maplocs <x> <z>               lists what's placed on one map square
python -m rs3mcp.maplocs verify                reads every map square
```

Clientscripts ([clientscripts](clientscripts.md)):

```
python rs3mcp/clientscript.py verify           checks the map can read every script
python rs3mcp/clientscript.py dis <id>         shows one script's raw instructions
python rs3mcp/csstack.py verify                checks the stack adds up in every script
python rs3mcp/csstack.py infer --save          works out unknown instructions and saves them
python rs3mcp/csstack.py trace <id> [at]       shows the stack before each instruction
python rs3mcp/csdecomp.py <id>                 turns one script back into code
python rs3mcp/csdecomp.py check                checks every script
python rs3mcp/csdecomp.py write <folder>       writes every script to files
python rs3mcp/cscalibrate.py check             checks the map still matches the cache
python rs3mcp/cscalibrate.py run               works out new instruction numbers and saves them
python rs3mcp/cscalibrate.py snapshot          makes the current cache the reference
python rs3mcp/cscalibrate.py simulate [seed] [share]   fakes a game update and checks it gets worked out
```

## Pictures and models

`rs3-icon.py` and `model_png.py` need Pillow and numpy: `pip install pillow numpy`. `model_obj.py`
doesn't need anything.

```
python rs3-icon.py <item id or name> [more...] [-o out.png] [-size 128] [-sheet] [-fit [margin]] [-extra x,y,z]
python model_png.py <model id> <out.png> [size] [label]
python model_obj.py <model id> <out.obj>
```

- `rs3-icon.py` draws an item the way it looks in your inventory. Give it several items, or `-sheet`,
  to get them all on one labelled picture. `-fit` makes the item fill the picture, and `-extra` turns
  it further.
- `model_png.py` draws a model from four sides.
- `model_obj.py` saves a model as an obj file you can open in 3D software.

You can also give these an rsmv model json file instead of an id.

## Looking things up from the shell

`rs3-cache.py` gives quick answers as plain text. Most of what it does is also a tool now, but it's
still the easiest way to make sprite sheets.

```
python rs3-cache.py npc|loc|obj|struct|enum|quest|seq|iface <id | CONFIG_NAME | display name> [-v]
python rs3-cache.py find [kind] <text>          searches everything, or one kind
python rs3-cache.py var <VARNAME | id>
python rs3-cache.py shop <INVNAME | id>         what a shop sells
python rs3-cache.py param <id | NAME>           a param, its type and what uses it
python rs3-cache.py anim <id | NAME>            an animation, its frames and what plays it
python rs3-cache.py sprite <id | text>          a sprite and what uses it
python rs3-cache.py grab <ids | text> [-show] [-o folder]           saves sprites as png files
python rs3-cache.py sheet <ids | label=id | text> [-show] [-copy] [-o out.png]   puts sprites on one labelled picture
python rs3-cache.py sql <select ...>
```

`-v` also shows the raw cache fields. `-show` opens the picture when it's done. `-copy` also saves
each sprite on its own. If the first word isn't one of these, it's searched for.

## Testing

```
python rs3mcp/test/soak.py [--deep] [--seed N]       calls every tool hundreds of times with real ids
python rs3mcp/test/bench.py [--runs N] [--json]      times every tool
python rs3mcp/test/latency.py                        compares each tool's first call with its second
python rs3mcp/test/sandbox.py new <name>             makes a copy to try a change in
python rs3mcp/test/sandbox.py check <name>           tests the copy and compares its answers with yours
python rs3mcp/test/sandbox.py files <name>           shows what's different
python rs3mcp/test/sandbox.py promote <name>         copies it back, if the tests pass
python rs3mcp/test/sandbox.py drop <name>            throws it away
```

## Keeping copies in step

```
python sync.py [--pull | --push]           compares two copies of rs3cache, or copies one onto the other
```

`sync.py` is in the `cache-tools` copy kept in the repo. On its own, it just shows what's different.
`--pull` copies your working copy into the repo, and `--push` copies the repo back.

---

Previous: [The rs3 command](cli.md) · Next: [AI apps](apps.md)
