# Commands

[All docs](README.md) › Using it

On this page
- [rs3](#rs3)
- [The server](#the-server)
- [Building the database](#building-the-database)
- [Inside the decoders](#inside-the-decoders)
- [Pictures and models](#pictures-and-models)
- [Looking things up from the shell](#looking-things-up-from-the-shell)
- [Research, paid for once](#research-paid-for-once)
- [Testing](#testing)
- [Measuring the checks](#measuring-the-checks)
- [Handing work to an agent](#handing-work-to-an-agent)
- [Proving a test catches its bug](#proving-a-test-catches-its-bug)
- [What it has cost](#what-it-has-cost)
- [Where everything stands](#where-everything-stands)
- [Checking an agent's work](#checking-an-agents-work)
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
python -m rs3mcp.maptiles <x> <z>              sums up one map square's ground
python -m rs3mcp.maptiles verify               reads every map square's ground
python -m rs3mcp.maptiles png <x0> <z0> <x1> <z1> <out.png>   draws those squares as a map
python -m rs3mcp.mapwalk path <x> <z> <x> <z> [plane] [--doors]   the shortest walk between two tiles
python -m rs3mcp.mapwalk png <x0> <z0> <x1> <z1> <out.png>      the map with walls and blocked tiles
python -m rs3mcp.mapnear <x> <z> [level] [radius]               the named things around a tile, doors with their state
python -m rs3mcp.mapscene <x> <z> <out.glb> [level] [radius]    a 3D scene of the ground and locs around a tile
python -m rs3mcp.worldaudit [rec-world.jsonl]                   the sandbox world against qd worldscan's recording
python -m rs3mcp.maprender <x0> <z0> <x1> <z1> <out.png> [level] [zoom]   a detailed map, drawn as the minimap is
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
python -m rs3mcp.scriptnames [folder]          names the written scripts, checked against the code
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

## Research, paid for once

Every web page goes through the research store in `data/research/`, the same one `rs3_research` uses.
A page fetched before is read from disk; a plain curl of it is refused by the hook.

```
python -m rs3mcp.research find "<question>"                   what is known already, and what reuse saved
python -m rs3mcp.research fetch <url> [--refresh "<why>"]      stores a page, or says where it is saved
python -m rs3mcp.research add --claim "..." --url <url> --quote "<the page's own words>" [--topic t]
python -m rs3mcp.research show <finding id>                     a finding, its review and the page round its quote
python -m rs3mcp.research review <id> --verdict holds|overclaims|wrong|unsupported --why "..." [--better <id>]
```

A review is a checker's ruling. A finding whose latest review is anything but `holds` is sent back
at the handback gate, pointing at the better finding when the review names one.

`add` keeps a finding only when its quote is on the stored page, or the local NLI finds the claim
there. Set `RS3_WHO` to say who is asking.

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

## Measuring the checks

```
python -m rs3api.mutate <plugin> --tests "<command>" [--only core/,src/] [--limit 60]
```

Writes one small bug at a time into a copy of the plugin, then checks it the way real work is
checked: `luac -p`, `rs3_lua_check`, and the plugin's own tests. A bug nobody notices is a survivor.
The percentage it prints is how much of a real mistake the checks would actually catch, counted
against each file's own baseline so an existing warning cannot claim the credit.

## Handing work to an agent

```
python -m rs3api.brief --goal "..." --done "<command that decides it>" --scope <folder>
                       [--file <path>] [--check "<command>"] [--plugin <name>]
```

Builds the brief instead of writing it: the goal, the one command that decides it, the folders a
hook will hold it to, every check confirmed to exist here, what the client was measured doing with
the api those files touch, the lessons anything already enforces, and the handback block to fill in.
A check that is not there is named rather than left out.

## Proving a test catches its bug

```
python -m rs3api.verify <plugin> --tests "<command>" --bug file.lua:"old":"new"
```

Runs the tests three times on a copy: as the code stands, with the bug written back in, and with it
taken out again. A test that passes with the bug in does not catch it, whatever its name says. This
is the step between fixing something and calling it fixed.

## What it has cost

```
python cost.py                                       hours and tokens so far
python cost.py --add "<note>" --tokens N --hours N   record a session
```

Hours come from the transcripts, an hour with any activity counting once. Tokens are what a session
reported holding, which is not what it was billed: a turn re-sends the context and caching pulls it
back. The weekly percentage in the app is the only figure that is the bill.

## Where everything stands

```
python state.py                                      every measured number, with the command behind it
```

One screen: whether the suite passes, how many client facts there are and which run they came from,
what the facts check can act on and what it finds on real code, the agent gate's bounce rate, and
how many lessons anything actually enforces. A number nobody measures is printed as NOT MEASURED
rather than left out, so the gaps stay visible.

## Checking an agent's work

When Claude Code hands work to a subagent, you only see the agent's report. `rs3mcp/handback.py`
checks that report against what the agent actually did before the agent is allowed to finish:

```
python rs3mcp/handback.py <agent transcript .jsonl>    checks one finished agent
python rs3mcp/handback.py --hook                       the same, as a Claude Code hook
```

The agent has to end with a short handback: its status, the files it changed, the checks it ran,
and where each finding comes from. If a check it lists never ran or failed, or a file it lists is
missing or broken, the agent is sent back to fix it. So when it says done, it has been checked.

To turn it on, add the `--hook` command to `~/.claude/settings.json` twice: under `SubagentStop`,
and under `PreToolUse` with the matcher `SubagentHandback|Edit|Write|MultiEdit|NotebookEdit`. The
second one also keeps an agent inside the folders its brief lists as `scope:` lines.

The handbacks real agents wrote are kept with their transcripts, and judged again whenever the
checker changes, so a change that would send a good report back shows up before an agent meets it:

```
python -m rs3mcp.replay            what moved since the verdicts were recorded
python -m rs3mcp.replay --record   record them, once you have read every change
```

---

Previous: [The rs3 command](cli.md) · Next: [AI apps](apps.md)
