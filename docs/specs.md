# Config specs

[All docs](README.md) › How it works

Each kind of config has a spec file in `rs3mcp/specs/<type>.ops`. The spec says how to read that
config from the cache, one line per opcode. `rs3mcp/ops.py` does the reading.

The field names in a spec are the same names you'll see in the database's `data` column. So if you
want to know what a config holds, its spec is the place to look.

## A spec

A config is a list of opcodes, each followed by a value, and ends with `0x00`. A spec lists those
opcodes:

```
type npcs
index 18                      which cache index it's in. Use `group N` if one group holds them all

0x02 name        string
0x0C size        u8
0x28 recolour    list<{u16 from; u16 to}>
0xBB morphs      {u8 _flags; u32 var -> var; list[varu16]<u16 -> npcs> ids; u16 fallback -> npcs}
0xF9 params      params
```

Some configs aren't a list of opcodes, just one fixed layout. For those, write `record` and the
layout:

```
type mapzones
index 23
group 0
record {string name; string title; ...}
```

A few other options:

- `layout groups` makes each cache group one config, with the group number as its id.
- `layout files` gives each file the id `group << 16 | file`.
- Without either, ids come from whole groups, like items and npcs.
- `end N` changes which opcode ends a config.
- Anything after `#` on a line is a comment.

## Values

| Write | To read |
|---|---|
| `u8 i8 u16 i16 u24 u32 i32` | a whole number of that size, big endian |
| `varu16 vari16` | one byte, or two if the top bit is set |
| `varu32 vari32` | two bytes, or four if the top bit is set |
| `leb128` | seven bits per byte, lowest first, while the top bit is set |
| `string` | text ending in a zero byte |
| `pstring` | a zero byte, then text |
| `bool` | one byte, 0 or 1 |
| `true` `false` | nothing. The opcode being there is the value |
| `f32` | a big endian float |
| `bytes[N]` | N bytes, saved as hex |
| `bytes[n]` | as many bytes as field `n` says |
| `bytes[rest]` | every byte left |
| `params` | the param list: a count, then a key and a number or text for each |
| `list<T>` | a varu16 count, then that many T |
| `list[u8]<T>` | the count is a u8 |
| `list[4]<T>` | exactly 4 |
| `list[n]<T>` | the count is field `n` |
| `list[n-1]<T>` | field `n` minus one. `n&15` and `n>>4` work too |
| `list[end 0xFF]<T>` | T until the next byte is 0xFF |
| `dbvalues[t]` | a row count, then one value per type in list `t` per row: text for type 36, a number otherwise |
| `{T name; T name}` | a group of fields. A name starting with `_` is read but not saved |
| `T name if f==N` | only there if field `f` is N. `!=`, `>=`, `<`, `&` and `!&` work too |
| `T name if f==N or g==M` | there if either is true |
| `T -> type` | marks the value as an id of that type, so the tools can follow it |

## Rules

Only add what the current cache actually uses.

If a config has an opcode its spec doesn't know, it's read up to that point and reported. The
rebuild counts it as a failure. If a whole type starts failing, the new database isn't installed
(see [how the database is built](rebuild.md)).

## Working on one

```
python rs3mcp/ops.py verify npcs          reads every npc and counts how often each opcode appears
python rs3mcp/ops.py decode npcs 798      reads one
python rs3mcp/ops.py solve npcs 0x2A      suggests shapes for an opcode that fit every npc using it
```

After changing a spec, run `rs3 rebuild --force` to rebuild with it. The `opcode_specs` tests check
that every spec reads the real cache.

## Status

All 39 types read every config, including all 104,285 interface components.

One kind of component, kind 16, only has 8 examples in the cache, and its long form only 3. Its field
names are guesses, so don't rely on them.

[The database](database.md) lists each type with how many rows and opcodes it has.

`invs` is the one type that isn't in the `config` table. It goes into the `inv` and `inv_stock`
tables instead, because `config` already lists inventories as `Inv`.

---

Previous: [How the database is built](rebuild.md) · Next: [Clientscripts](clientscripts.md)
