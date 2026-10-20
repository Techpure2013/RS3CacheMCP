# Tools

[All docs](README.md) › Using it

Every tool, in 5 groups. There are 16 in all. AI apps and the `rs3` command use the same tools. Apps see the full name, like `rs3_lookup`. In `rs3` you can leave off the `rs3_`.

This page is written automatically by `rs3 docs`, so don't edit it by hand.

Every tool also takes `pick`, to get just one part of an answer (`pick=counts.npcs`), and `outDir`, to save the answer to a file. To show an app only some groups, see [settings](settings.md).

| Group | Tools |
|---|---|
| [cache](#cache) | [cache_diff](#cache_diff), [family](#family), [lookup](#lookup), [query](#query), [schema](#schema), [sprite](#sprite), [trace](#trace) |
| [docs](#docs) | [wiki](#wiki) |
| [game](#game) | [dbtable](#dbtable), [iface](#iface), [var](#var) |
| [meta](#meta) | [drift](#drift), [research](#research) |
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
| `column` | string |  | a packed dbcolumn id, split into table and column |
| `text` | string |  | find rows whose stored text contains this |
| `drops` | string |  | true to list tables holding weighted drop tables, found by shape rather than name |
| `all` | string |  | true to include the tables hidden by default from drops= and text=: monetised content, the rotating event schedule and tutorial prose |
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

## meta

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
