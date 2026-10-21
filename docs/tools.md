# Tools

[All docs](README.md) › Using it

Every tool, in 6 groups. There are 30 in all. AI apps and the `rs3` command use the same tools. Apps see the full name, like `rs3_lookup`. In `rs3` you can leave off the `rs3_`.

This page is written automatically by `rs3 docs`, so don't edit it by hand.

Every tool also takes `pick`, to get just one part of an answer (`pick=counts.npcs`), and `outDir`, to save the answer to a file. To show an app only some groups, see [settings](settings.md).

| Group | Tools |
|---|---|
| [cache](#cache) | [cache_diff](#cache_diff), [family](#family), [lookup](#lookup), [query](#query), [schema](#schema), [sprite](#sprite), [trace](#trace) |
| [docs](#docs) | [api](#api), [api_gotchas](#api_gotchas), [docs_check](#docs_check), [wiki](#wiki) |
| [game](#game) | [dbtable](#dbtable), [iface](#iface), [var](#var) |
| [lua](#lua) | [lab_query](#lab_query), [lab_run](#lab_run), [lsp](#lsp), [lua](#lua-1), [lua_ast](#lua_ast), [lua_check](#lua_check), [lua_example](#lua_example), [plugin_lint](#plugin_lint) |
| [meta](#meta) | [collision](#collision), [collision_export](#collision_export), [drift](#drift), [research](#research), [transport_candidates](#transport_candidates) |
| [scripts](#scripts) | [script](#script), [script_read](#script_read), [script_refs](#script_refs) |

## cache

### cache_diff

`rs3_cache_diff`, reads only.

What a game update changed between the current cache database and the one the rebuild replaced: ids that went away, display names that moved, config names that moved, and what was added. Run it after a rebuild to find out whether an id something relies on has quietly stopped resolving.

| Argument | Type | | What |
|---|---|---|---|
| `type` | string |  | one config type, e.g. items or objects |
| `limit` | integer |  | rows of each kind, 25 by default |
| `current` | string |  | a database path; defaults to rs3-cache.db |
| `previous` | string |  | defaults to rs3-cache.db.bak |

```
rs3 cache_diff limit=10
rs3 cache_diff type=items
```

Over MCP: `{"name": "rs3_cache_diff", "arguments": {"limit": 10}}`

### family

`rs3_family`, reads only.

The whole config family for a loc, npc or item, from any member, and which id to match on. Locs and npcs morph and the world carries the parent id, so matching a child finds nothing. Items link instead: notes, lent and bought copies are separate ids.

| Argument | Type | | What |
|---|---|---|---|
| `kind` | string | required | loc, npc or obj |
| `id` | string |  | config id, as rs3_lookup or rs3_query returns |
| `name` | string |  | config name or display name, if no id |

```
rs3 family kind=loc id=545
```

Over MCP: `{"name": "rs3_family", "arguments": {"kind": "loc", "id": "545"}}`

### lookup

`rs3_lookup`, reads only.

One config by kind and name or id, with params, multiloc varbit and shop appearances resolved, and the parents when it is one appearance of a multi. rs3_family for the rest of the family, rs3_trace for everything that points at it.

| Argument | Type | | What |
|---|---|---|---|
| `kind` | string | required | npc, loc, obj, struct, enum, quest, seq or iface |
| `name` | string |  | config name or display name |
| `id` | string |  | config id, same as passing it as name |

```
rs3 lookup kind=npc name=Wolf
rs3 lookup kind=obj id=4151
```

Over MCP: `{"name": "rs3_lookup", "arguments": {"kind": "npc", "name": "Wolf"}}`

### query

`rs3_query`, reads only.

Run a read-only SQL SELECT against the RS3 cache database and get rows as JSON.

| Argument | Type | | What |
|---|---|---|---|
| `sql` | string | required | a single SELECT or WITH statement |
| `limit` | integer |  | max rows, default 200, cap 500 |

```
rs3 query sql="select id,name,display from config where type='items' and display='Coins' order by id" limit=3
```

Over MCP: `{"name": "rs3_query", "arguments": {"sql": "select id,name,display from config where type='items' and display='Coins' order by id", "limit": 3}}`

### schema

`rs3_schema`, reads only.

Tables, columns, row counts and the gotchas needed to write correct SQL against the RS3 cache database. Call this first.

Takes no arguments.

```
rs3 schema
```

Over MCP: `{"name": "rs3_schema", "arguments": {}}`

### sprite

`rs3_sprite`, reads only.

A sprite by id or by name, with the png path on disk and what points at it.

| Argument | Type | | What |
|---|---|---|---|
| `name` | string |  | part of its name, or its user's |
| `id` | string |  | sprite id |

```
rs3 sprite name=necrosis
```

Over MCP: `{"name": "rs3_sprite", "arguments": {"name": "necrosis"}}`

### trace

`rs3_trace`, reads only.

Everything connected to one config, both directions: params with resolved targets, configs whose params name it, enums it appears in, shops, sprites, map sections, and its multiloc or note family. Beats guessing which table to join.

| Argument | Type | | What |
|---|---|---|---|
| `kind` | string | required | obj/item, npc, loc, struct, seq, enum, iface |
| `id` | string |  | config id, as rs3_lookup or rs3_query returns |
| `name` | string |  | config or display name, if no id |
| `limit` | integer |  | max rows per section, default 40 |

```
rs3 trace kind=obj id=2
```

Over MCP: `{"name": "rs3_trace", "arguments": {"kind": "obj", "id": "2"}}`

## docs

### api

`rs3_api`, reads only, from the rs3api add-on.

Find a class, interface, namespace or enum in the RS client API typings, or find everything that has a given member. Returns the declaration verbatim plus anything known to be wrong about it. Use member= to answer questions like which entities expose aabb.

| Argument | Type | | What |
|---|---|---|---|
| `name` | string |  | type name, qualified or not, e.g. Sprite or ui.Sprite |
| `member` | string |  | a property or method name to search for across all types |
| `kind` | string |  | narrow to class, interface, namespace or enum |
| `full` | boolean |  | return full declarations even for a wide search |

### api_gotchas

`rs3_api_gotchas`, reads only, from the rs3api add-on.

Everything known to be wrong or surprising about the RS client API: properties that exist but are not declared, one that is documented and does not exist, units that differ from the rest of the api, and the crash shapes. Each with what was actually observed. Worth reading before trusting a typing.

| Argument | Type | | What |
|---|---|---|---|
| `subject` | string |  | filter, e.g. aabb, sprite, facing, crash |

### docs_check

`rs3_docs_check`, reads only, from the rs3api add-on.

Check the local docs mirror against the typings: which identifiers a page names that the typings do not declare, and how many pages record no source url so cannot be re-checked at all. Run before trusting a mirrored page for something that matters.

| Argument | Type | | What |
|---|---|---|---|
| `page` | string |  | part of a page name, e.g. npclist |

### wiki

`rs3_wiki`, reads only, reaches the network.

The RS3 wiki, cached locally: a page's infobox and drop table from its templates, or a search. Covers what the cache cannot -- drop rates, mechanics, requirements, anything a person wrote down. One page per call, and never the same page twice.

| Argument | Type | | What |
|---|---|---|---|
| `page` | string |  | exact page title, e.g. Abyssal demon |
| `search` | string |  | find a page by words |
| `raw` | boolean |  | include the wikitext source |
| `refresh` | boolean |  | re-fetch even if stored |

```
rs3 wiki
```

Over MCP: `{"name": "rs3_wiki", "arguments": {}}`

## game

### dbtable

`rs3_dbtable`, reads only.

The game's data tables and their rows: currencies, farm paddocks, invention materials and 383 others. Columns with types and defaults, rows, search by name, or splits a packed dbcolumn id. text= searches the 26,535 strings no other tool reaches.

| Argument | Type | | What |
|---|---|---|---|
| `id` | string |  | table id |
| `name` | string |  | table name, exact or partial |
| `search` | string |  | list tables whose name contains this |
| `rows` | string |  | true to include the table's rows |
| `column` | string |  | a packed dbcolumn id to split |
| `text` | string |  | find rows whose stored text contains this |
| `drops` | string |  | true to list weighted drop tables, found by shape |
| `all` | string |  | true to include what drops= and text= hide: monetised, event schedule, tutorial prose |
| `limit` | integer |  | max tables or rows, default 40 |

```
rs3 dbtable search=ACHIEVEMENT
rs3 dbtable id=314 rows=true
```

Over MCP: `{"name": "rs3_dbtable", "arguments": {"search": "ACHIEVEMENT"}}`

### iface

`rs3_iface`, reads only.

Interfaces and their components, decoded. Splits a packed interface<<16|component id, lists a component's kind, parent, position and right click options, or searches by name. text= searches what is drawn on screen, the fastest route from something visible to its id. referencedByScripts names the clientscripts that build it.

| Argument | Type | | What |
|---|---|---|---|
| `id` | string |  | 284, "284:5", or a raw packed id |
| `name` | string |  | interface name, exact or partial |
| `search` | string |  | list interfaces whose name contains this |
| `text` | string |  | find components drawing this text |
| `limit` | integer |  | max components or hits, default 80 |

```
rs3 iface id=18612224
```

Over MCP: `{"name": "rs3_iface", "arguments": {"id": "18612224"}}`

### var

`rs3_var`, reads only.

A VarPlayer or VarClient decoded: for a varbit, its base var with mask, shift and range; for a base var, its varbits and free bits. quest is set when a quest tracks it, possibleQuest when only the name says so.

| Argument | Type | | What |
|---|---|---|---|
| `name` | string |  | var name, exact or partial |
| `id` | string |  | var id, negative for a varbit |
| `domain` | string |  | VarPlayer or VarClient |
| `search` | string |  | list every var whose name contains this |
| `limit` | integer |  | max slices or hits, default 60 |

```
rs3 var id=-60893
```

Over MCP: `{"name": "rs3_var", "arguments": {"id": "-60893"}}`

## lua

### lab_query

`rs3_lab_query`, reads only, from the rs3api add-on.

Ask a traced lab run: events, state, diff, why (exact line by replay), coverage, compare, break.

| Argument | Type | | What |
|---|---|---|---|
| `run` | string | required | id from rs3_lab_run trace=true |
| `op` | string | required | events\|state\|diff\|why\|coverage\|compare\|break |
| `at` | integer |  | event number |
| `path` | string |  | eg core/x.state.kills |
| `from` | integer |  | event number |
| `to` | integer |  | event number |
| `file` | string |  | eg core/x.lua |
| `other` | string |  | run id |
| `when` | string |  | Lua condition, lab.get(path) reads state |
| `show` | array |  | Lua expressions |
| `start` | integer |  | paging |

### lab_run

`rs3_lab_run`, reads only, from the rs3api add-on.

Run a Lua probe against a sandboxed copy of a plugin, loaded as the client loads it; lab.fire sends events.

| Argument | Type | | What |
|---|---|---|---|
| `path` | string | required | the plugin folder |
| `entry` | string |  | file to load first |
| `script` | string |  | Lua; its return value is the result |
| `seed` | integer |  | default 1 |
| `seconds` | number |  | limit, default 5 |
| `mb` | integer |  | limit, default 256 |
| `trace` | boolean |  | for rs3_lab_query |
| `model` | boolean |  | use the measured client |
| `gaps` | boolean |  | list all gaps |

```
rs3 lab_run path=C:/Users/you/RS3Plugins/my-plugin script="return lab.require('core/textfmt').fmt(1234567)"
```

Over MCP: `{"name": "rs3_lab_run", "arguments": {"path": "C:/Users/you/RS3Plugins/my-plugin", "script": "return lab.require('core/textfmt').fmt(1234567)"}}`

### lsp

`rs3_lsp`, writes or runs something, from the rs3api add-on.

What the language server says about a lua file, folder or workspace: fields that do not exist, nils never checked, unused locals. The half luac cannot see. Reports at Hint, because every higher level drops rows the editor shows and still returns a number that reads as an answer.

| Argument | Type | | What |
|---|---|---|---|
| `path` | string | required | a .lua file or folder; checked from the nearest .luarc.json, rows filtered to it |
| `level` | string |  | lowest level reported, Hint by default |
| `refresh` | boolean |  | re-run even if nothing has changed |
| `limit` | integer |  | rows to return, 60 by default |

```
rs3 lsp path=RS3Plugins/my-plugin
```

Over MCP: `{"name": "rs3_lsp", "arguments": {"path": "RS3Plugins/my-plugin"}}`

### lua

`rs3_lua`, reads only, from the rs3api add-on.

Writing lua for the RS client: what the sandbox provides and withholds, the traps that have actually cost time, and working recipes for windows, events, world drawing, walking an interface and plugin resources. Call with no topic for the index.

| Argument | Type | | What |
|---|---|---|---|
| `topic` | string |  | e.g. sandbox, pcall, events, window, drawing, resources, truthiness |

### lua_ast

`rs3_lua_ast`, reads only, from the rs3api add-on.

Search lua by shape, not text. `pcall(function() $$$B end)` matches the call however the body is written. $X is one node, $$$X is several. rewrite= previews a replacement; only apply=true writes.

| Argument | Type | | What |
|---|---|---|---|
| `pattern` | string | required | ast-grep pattern, $X and $$$X are holes |
| `path` | string | required | a lua file or a folder |
| `rewrite` | string |  | replacement, reusing the same holes |
| `apply` | boolean |  | write it. Off by default, so you see first |
| `limit` | integer |  | default 40 |
| `start` | integer |  | pass the nextStart a paged answer gives |

```
rs3 lua_ast pattern="pcall(function() $$$B end)" path=RS3Plugins/my-plugin
```

Over MCP: `{"name": "rs3_lua_ast", "arguments": {"pattern": "pcall(function() $$$B end)", "path": "RS3Plugins/my-plugin"}}`

### lua_check

`rs3_lua_check`, reads only, from the rs3api add-on.

Compile a lua file or folder with luac and flag the traps that compile fine and fail in the client. Run it before deploying.

| Argument | Type | | What |
|---|---|---|---|
| `path` | string | required | a .lua file or a folder to walk |

```
rs3 lua_check path=RS3Plugins/my-plugin/main.lua
```

Over MCP: `{"name": "rs3_lua_check", "arguments": {"path": "RS3Plugins/my-plugin/main.lua"}}`

### lua_example

`rs3_lua_example`, reads only, from the rs3api add-on.

Every real use of a symbol across the installed plugins, with surrounding lines. Use it to see how something is actually called before calling it.

| Argument | Type | | What |
|---|---|---|---|
| `symbol` | string | required | e.g. ShapeList.Draw, RibbonBar.Register, aabb |
| `limit` | integer |  | max examples, default 12 |
| `context` | integer |  | lines either side, default 3 |

### plugin_lint

`rs3_plugin_lint`, reads only, from the rs3api add-on.

Check a plugin.json against the manifests that are installed and working: keys the client will silently ignore, a dependency that is not installed so its global never appears, and an http permission with no urls block. Omit path to see the key vocabulary and the permissions actually in use.

| Argument | Type | | What |
|---|---|---|---|
| `path` | string |  | a plugin.json, or the plugin folder holding one |

```
rs3 plugin_lint path=RS3Plugins/my-plugin
```

Over MCP: `{"name": "rs3_plugin_lint", "arguments": {"path": "RS3Plugins/my-plugin"}}`

## meta

### collision

`rs3_collision`, reads only.

An area's walkability as a grid, with doors, openings, transports and sealed doors marked on it. Use it to see why a route is blocked, or what a tile is shut in by.

| Argument | Type | | What |
|---|---|---|---|
| `x` | integer | required | world x at the centre |
| `z` | integer | required | world z at the centre |
| `plane` | integer |  | 0 to 3, default 0 |
| `size` | integer |  | tiles a side, 1 to 128, default 32 |
| `destinations` | boolean |  | look each transport's far end up in the questbuddy transports table. Off by default: it is 200ms over the tunnel, and the cache itself does not hold a far end |
| `root` | string |  | a cache folder other than the live one |

```
rs3 collision x=3222 z=3218
rs3 collision x=3222 z=3218 plane=1 size=48
```

Over MCP: `{"name": "rs3_collision", "arguments": {"x": 3222, "z": 3218}}`

### collision_export

`rs3_collision_export`, reads only.

One mapsquare's collision in a form a project can carry: one byte a tile, run-length encoded, plus the doors, openings, transports and sealed doors on it. Pass outDir to write it instead of returning the bytes.

| Argument | Type | | What |
|---|---|---|---|
| `square_x` | integer | required | mapsquare x, world x divided by 64 |
| `square_z` | integer | required | mapsquare z, world z divided by 64 |
| `plane` | integer |  | 0 to 3, default 0 |
| `outDir` | string |  | write a json file here and leave out the bytes |
| `destinations` | boolean |  | look each transport's far end up in the questbuddy transports table, 200ms over the tunnel |
| `root` | string |  | a cache folder other than the live one |

```
rs3 collision_export square_x=50 square_z=50
rs3 collision_export square_x=50 square_z=50 outDir=out/collision
```

Over MCP: `{"name": "rs3_collision_export", "arguments": {"square_x": 50, "square_z": 50}}`

### drift

`rs3_drift`, reads only.

Check every number this server states about the data against the data, plus the count of unchecked numbers. Run it after a rebuild, or when a description's figure looks surprising.

Takes no arguments.

```
rs3 drift
```

Over MCP: `{"name": "rs3_drift", "arguments": {}}`

### research

`rs3_research`, writes or runs something, reaches the network.

Research paid once: q finds what is known; fetch stores a page (refresh needs a reason); add keeps a quoted finding; show and review are for checking one.

| Argument | Type | | What |
|---|---|---|---|
| `q` | string |  |  |
| `fetch` | string |  |  |
| `refresh` | string |  |  |
| `add` | object |  | claim, url, quote, topic |
| `show` | string |  |  |
| `review` | object |  | id, verdict (holds\|overclaims\|wrong\|unsupported), why, better |

```
rs3 research q="how LSP reports a file edit"
```

Over MCP: `{"name": "rs3_research", "arguments": {"q": "how LSP reports a file edit"}}`

### transport_candidates

`rs3_transport_candidates`, reads only.

Work out where each stair, ladder and trapdoor in a box of mapsquares takes you, from the mate on the far plane and the model's own slope. Answers are candidates for a person to check, and say which were unclear.

| Argument | Type | | What |
|---|---|---|---|
| `square_x` | integer | required | mapsquare x, world x divided by 64 |
| `square_z` | integer | required | mapsquare z, world z divided by 64 |
| `to_square_x` | integer |  | far corner x, default the same square |
| `to_square_z` | integer |  | far corner z, default the same square |
| `outDir` | string |  | write a json file here instead of sampling |
| `root` | string |  | a cache folder other than the live one |

```
rs3 transport_candidates square_x=50 square_z=50
rs3 transport_candidates square_x=49 square_z=49 to_square_x=51 to_square_z=51 outDir=out/transports
```

Over MCP: `{"name": "rs3_transport_candidates", "arguments": {"square_x": 50, "square_z": 50}}`

## scripts

### script

`rs3_script`, reads only.

Search the 21,101 decompiled clientscripts for a pattern, with surrounding lines. These settle what the cache cannot: what writes a varbit, what reads it, what a value means. About a second. rs3_script_read takes the id this returns; rs3_script_refs goes the other way, from a varbit or item id.

| Argument | Type | | What |
|---|---|---|---|
| `pattern` | string | required | literal text, or a regex with regex=true |
| `regex` | boolean |  | treat the pattern as a regex |
| `name` | string |  | only scripts whose header name contains this |
| `limit` | integer |  | max hits, default 20 |
| `start` | integer |  | pass the nextStart a truncated answer gives |
| `context` | integer |  | lines either side, default 2 |

```
rs3 script pattern=IF_SETHIDE limit=5
```

Over MCP: `{"name": "rs3_script", "arguments": {"pattern": "IF_SETHIDE", "limit": 5}}`

### script_read

`rs3_script_read`, reads only.

One clientscript whole, by its number or by a name from its header comment.

| Argument | Type | | What |
|---|---|---|---|
| `script` | string | required | an id from rs3_script, or a name like quicksort |

```
rs3 script_read script=0
```

Over MCP: `{"name": "rs3_script_read", "arguments": {"script": "0"}}`

### script_refs

`rs3_script_refs`, reads only.

Which clientscripts touch a given varbit, var, item, npc, interface, sequence or script, by id rather than by text. The decompiler types its constants, so this is exact where a text search would match a coordinate sharing the number. kind= alone gives the most-referenced ids.

| Argument | Type | | What |
|---|---|---|---|
| `kind` | string |  | varbit, varp, varclient, obj, npc, seq, struct, inv, quest, iface, script and others |
| `id` | string |  | the id; a varbit may be given either sign |
| `limit` | integer |  | scripts to return, 40 by default |

```
rs3 script_refs kind=varbit id=27169
rs3 script_refs kind=iface id=1188
```

Over MCP: `{"name": "rs3_script_refs", "arguments": {"kind": "varbit", "id": "27169"}}`

---

Previous: [AI apps](apps.md) · Next: [The database](database.md)
