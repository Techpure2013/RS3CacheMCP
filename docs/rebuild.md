# How the database is built

[All docs](README.md) › How it works

On this page
- [When it runs](#when-it-runs)
- [Only what changed](#only-what-changed)
- [Stages](#stages)
  - [Extract](#extract)
  - [Build](#build)
  - [Verify](#verify)
  - [Install](#install)
  - [Index](#index)
- [Running the stages alone](#running-the-stages-alone)

`rs3 rebuild` reads your game cache and builds a new database next to the old one. It checks the new
one, and only swaps it in if it passes. Each step prints how long it took.

## When it runs

First it asks the [OpenRS2 archive](https://archive.openrs2.org) for the newest RS3 cache, which it
pulls straight from Jagex. The game only downloads a file when it needs one, so your cache can be
behind after an update. Only the files that differ from yours are fetched, checked against their
checksums, and kept in `data/openrs2/`. Your game's own files are never touched. After the
2026-10-26 update that was 118 files in 33 seconds.

Then it checks whether anything moved since the last build. If nothing did, it stops there.

| Flag | What it does |
|---|---|
| `--force` | builds everything from scratch, even if nothing moved |
| `--check` | only checks, fetching nothing. Exits with 1 if a rebuild is due, 0 if not |
| `--offline` | leaves OpenRS2 out of it and uses your game cache as it is |
| `--no-swap` | builds and checks, but doesn't install the new database |

`python -m rs3mcp.openrs2 --check` lists what your cache is behind on, and
`python -m rs3mcp.changed` lists what moved since the last build.

## Only what changed

Every build writes the checksum of every file it was built from to `data/rs3-cache-built.json`. The
next build compares against it and only reads the configs in files that moved. Everything else is
copied from the current database, which holds it exactly as it was read last time. Clientscripts
and sprites are left alone unless theirs moved, and only the sprites that moved are redrawn.

The database file itself is still written out whole, which takes under a minute. The first build,
or one with `--force`, reads everything.

## Stages

These times are from a 32 core PC. On a slower PC, the clientscript and index steps take longest.

| Stage | What it does | Time |
|---|---|---|
| extract: configs | reads every config from the cache | ~3s |
| extract: models | reads a summary of all 145,735 models | ~14s |
| extract: clientscripts | turns every clientscript back into readable code | ~40s |
| extract: sprites | saves all 36,425 sprites as png files | ~25s |
| build | puts it all into a new database | ~10s |
| verify | checks the new database against the old one | <1s |
| install | swaps the new database in | <1s, or ~15s if an app has the old one open |
| assets | moves the new scripts and sprites into place, keeping the old ones as `.bak` | ~5s |
| index | builds the search index | ~20s |
| docs | rewrites `tools.md` and `database.md` | <1s |

That's two to three minutes. After that it spends about a minute running tests, which report what
changed but never stop the install.

Everything is built in `data/rs3-cache-stage-new` first. If a build fails its checks, or you used
`--no-swap`, that folder is left there so you can look at it.

### Extract

Each kind of config is read using its spec in `rs3mcp/specs/` ([config specs](specs.md)). Models are
read by `rs3mcp/models.py` ([models](models.md)). The work is split across your CPU cores, up to 32.

A config that can't be read all the way to its end is listed in `_failures.json`. Then:

- If it stopped at an opcode the spec doesn't have, the rebuild tries to work it out from every
  record that has it. It only takes an answer when every shape that fits reads the rest of each
  record the same way. Then it's saved to `data/learned-ops.json` as `unkXX`, the build reads with
  it, and it tells you. Hidden from their specs, 24 of 24 opcodes it settled came back right. When
  the records can't decide, as with a single record that reads two ways, it doesn't guess.
- Anything still unread keeps the row the old database had for it, and is named in the output with
  `ATTENTION`. A config new in this update has no old row, so it's left out and named.

So an update that adds an opcode doesn't stop the build, and nothing you could look up yesterday
disappears today. A change to how an existing opcode is laid out still needs a person to fix the
spec, but until then you keep the old rows.

A file the game holds but that can't be unpacked counts as unread too, record by record, so it can
never just quietly drop out.

Clientscripts are turned back into code by the decompiler ([clientscripts](clientscripts.md)). Every
game update shuffles the script instructions, so the rebuild works out the new order first, which
takes about five seconds. If that doesn't work, it skips the scripts, tells you, and builds
everything else. Your old scripts stay.

Sprites are saved as `sprites-<id>.png`, each at the size the game draws it. They take up about
2.8 GB, mostly loading screens. The old set is kept as a backup, so the data folder ends up around
7 GB. When only some sprites moved, just those are redrawn and written over the installed set, and
the ones they replace are kept in `sprites.bak`.

### Build

`build_db.py` puts every config into the `config` table, and builds these tables from it:

| Table | What's in it |
|---|---|
| `config` | every config, with its name if the name lists have it |
| `param_use` | every param on every config, and what it points at |
| `param_type` | the type of each param that has one |
| `enum_entry` | every key and value of every enum |
| `varbit` | the player varbits, named from the name lists |
| `model_use` | interface components that show a model |
| `sprite_use` | everything that shows a sprite |
| `sprite_hash` | the hashed sprite names the cache stores |
| `inv` | every inventory's size and how many items it starts with |
| `inv_stock` | what each shop starts with, slot by slot |
| `loc_section` | the area each loc is in, worked out from where it's placed on the map |

The cache only stores sprite names as hashes. The rebuild guesses names and checks them against the
hashes, and keeps any names found in earlier builds. Inventory names are kept from earlier builds
the same way.

The name lists are a folder of `Vars - <type>.csv` files. A set ships in `rs3mcp/names` and is used
without you doing anything; `RS3MCP_NAMES` ([settings](settings.md)) points at a newer one instead.

They matter more than they sound. The cache stores no name at all for an interface, a var or a
quest, so with no list those build as bare ids and nothing can look one up by name. Items, npcs and
locs keep the name you see in game but lose their `CONFIG_NAME`.

### Verify

The new database is only installed if all of these are true:

- No row the old database had has gone missing because it couldn't be read.
- No table has shrunk by more than 1 in 50. Game updates don't remove that much.

If the game really did remove something, that's fine. It's reported, and the install goes ahead.
Configs that couldn't be read are reported too, but they keep their old rows, so they don't stop it.

### Install

The old database is renamed to `rs3-cache.db.bak` and the new one takes its place. `rs3 cache_diff`
shows what changed between them.

If an AI app has the database open, Windows won't let it be renamed. In that case the rebuild copies
the new data into the open file instead. The app sees the new data on its next question. The index
is swapped the same way.

### Index

`rs3mcp/buildindex.py` builds the search index, `rs3-index.db`. It's what lets `rs3 script_refs` find
every script that uses a varbit, var, interface or item straight away.

The first time Windows opens a new file, antivirus scans it, which is slow. The rebuild has just
written about 21,100 scripts, so the index reads many at once to keep it to seconds. If you want it
faster still, you can exclude the rs3cache `data` folder from Windows Defender. That's your call.

## Running the stages alone

```
python -m rs3mcp.extract <stage> [type ...]     read configs and models into a folder
python rs3mcp/csdecomp.py write <folder>         write out every clientscript
python build_db.py <stage> <csvdir> <out.db> [--carry <old.db>]
python rs3mcp/buildindex.py
```

---

Previous: [Settings](settings.md) · Next: [Config specs](specs.md)
