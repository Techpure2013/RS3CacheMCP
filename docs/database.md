# The database

[All docs](README.md) › Using it

On this page
- [Tables](#tables)
  - [config](#config)
  - [enum_entry](#enum_entry)
  - [inv](#inv)
  - [inv_stock](#inv_stock)
  - [loc_section](#loc_section)
  - [meta](#meta)
  - [model_use](#model_use)
  - [param_type](#param_type)
  - [param_use](#param_use)
  - [sprite_hash](#sprite_hash)
  - [sprite_name](#sprite_name)
  - [sprite_use](#sprite_use)
  - [varbit](#varbit)
- [Config types](#config-types)

Every table in the database, `data/rs3-cache.db`. It's a normal SQLite file, built on 2026-09-22 09:05.

This page is written from the database itself by `rs3 docs`, which runs after every rebuild, so the numbers are always current.

To ask the database something, use `rs3 query` with a SELECT. `rs3 schema` shows the same tables, plus common mistakes to avoid.

## Tables

### config

858,637 rows.

| Column | Type | Key |
|---|---|---|
| `type` | TEXT | primary |
| `id` | INTEGER | primary |
| `name` | TEXT |  |
| `api` | TEXT |  |
| `data` | TEXT |  |
| `display` | TEXT |  |

Indexed: `ix_config_name`, `ix_config_display`, `config_name`

### enum_entry

160,209 rows.

| Column | Type | Key |
|---|---|---|
| `enum_id` | INTEGER |  |
| `key` | TEXT |  |
| `value` | TEXT |  |
| `key_type` | INTEGER |  |
| `value_type` | INTEGER |  |

Indexed: `ix_enum_key`, `ix_enum_entry`

### inv

951 rows.

| Column | Type | Key |
|---|---|---|
| `inv_id` | INTEGER | primary |
| `name` | TEXT |  |
| `size` | INTEGER |  |
| `entries` | INTEGER |  |

Indexed: `ix_inv_name`

### inv_stock

6,305 rows.

| Column | Type | Key |
|---|---|---|
| `inv_id` | INTEGER |  |
| `slot` | INTEGER |  |
| `item_id` | INTEGER |  |
| `quantity` | INTEGER |  |

Indexed: `ix_inv_stock_item`, `ix_inv_stock`

### loc_section

137,443 rows.

| Column | Type | Key |
|---|---|---|
| `loc_id` | INTEGER | primary |
| `section` | TEXT |  |
| `placed` | INTEGER |  |

Indexed: `ix_loc_section`

### meta

8 rows.

| Column | Type | Key |
|---|---|---|
| `k` | TEXT | primary |
| `v` | TEXT |  |

### model_use

6,376 rows.

| Column | Type | Key |
|---|---|---|
| `model_id` | INTEGER |  |
| `iface_id` | INTEGER |  |
| `comp_id` | INTEGER |  |
| `comp_name` | TEXT |  |

Indexed: `ix_model_use`

### param_type

6,026 rows.

| Column | Type | Key |
|---|---|---|
| `param_id` | INTEGER | primary |
| `name` | TEXT |  |
| `vartype` | INTEGER |  |
| `valuetype` | TEXT |  |
| `target` | TEXT |  |

### param_use

776,843 rows.

| Column | Type | Key |
|---|---|---|
| `owner_type` | TEXT |  |
| `owner_id` | INTEGER |  |
| `param_id` | INTEGER |  |
| `int_value` | INTEGER |  |
| `str_value` | TEXT |  |
| `target_type` | TEXT |  |
| `target_name` | TEXT |  |

Indexed: `pu_target`, `pu_str`, `ix_param_pid`, `ix_param_owner`

### sprite_hash

36,497 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER | primary |
| `namehash` | INTEGER |  |

### sprite_name

671 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER | primary |
| `name` | TEXT |  |
| `namehash` | INTEGER |  |

Indexed: `ix_sprite_name_n`

### sprite_use

38,971 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER |  |
| `source` | TEXT |  |
| `owner_type` | TEXT |  |
| `owner_id` | INTEGER |  |
| `owner_name` | TEXT |  |

Indexed: `ix_sprite_use`, `ix_sprite_owner`

### varbit

51,152 rows.

| Column | Type | Key |
|---|---|---|
| `id` | INTEGER | primary |
| `name` | TEXT |  |
| `domain` | TEXT | primary |
| `base_name` | TEXT |  |
| `base_id` | INTEGER |  |
| `mask` | INTEGER |  |
| `shift` | INTEGER |  |

Indexed: `ix_varbit_name`, `ix_varbit_base`

## Config types

`config` holds one row per config. `data` is the decoded record as json, its field names taken from the type's spec in `rs3mcp/specs/` (see [config specs](specs.md)). Types with no spec come from the name lists alone, or from `rs3mcp/models.py` for models.

| Type | Rows | Named | Spec | Opcodes |
|---|---|---|---|---|
| Cursor | 185 | 185 | | |
| Font | 225 | 225 | | |
| Inv | 934 | 934 | | |
| Music | 2,650 | 2,650 | | |
| Quest | 531 | 531 | | |
| StyleSheet | 318 | 318 | | |
| VarClient | 7,749 | 7,681 | | |
| VarPlayer | 64,538 | 60,228 | | |
| VarPlayerGroup | 3 | 3 | | |
| achievements | 5,086 | 4,900 | `specs/achievements.ops` | 33 |
| animgroupconfigs | 4,917 | 4,907 | `specs/animgroupconfigs.ops` | 24 |
| cutscenes | 9 | 0 | `specs/cutscenes.ops` | one record shape |
| dbrows | 20,129 | 15,340 | `specs/dbrows.ops` | 2 |
| dbtables | 387 | 242 | `specs/dbtables.ops` | 1 |
| enums | 16,701 | 8,559 | `specs/enums.ops` | 12 |
| environments | 196 | 0 | `specs/environments.ops` | 4 |
| fontmetrics | 225 | 0 | `specs/fontmetrics.ops` | one record shape |
| identitykit | 1,096 | 0 | `specs/identitykit.ops` | 6 |
| interfaces | 104,199 | 103,909 | `specs/interfaces.ops` | one record shape |
| items | 63,530 | 61,293 | `specs/items.ops` | 88 |
| maplabels | 5,812 | 0 | `specs/maplabels.ops` | 24 |
| mapscenes | 150 | 0 | `specs/mapscenes.ops` | 5 |
| mapzones | 771 | 0 | `specs/mapzones.ops` | one record shape |
| materials | 24,967 | 0 | `specs/materials.ops` | one record shape |
| models | 145,875 | 0 | | |
| npcs | 32,762 | 32,703 | `specs/npcs.ops` | 74 |
| objects | 140,298 | 137,174 | `specs/objects.ops` | 98 |
| overlays | 596 | 0 | `specs/overlays.ops` | 12 |
| params | 9,482 | 5,992 | `specs/params.ops` | 2 |
| particles0 | 4,519 | 0 | `specs/particles0.ops` | 33 |
| particles1 | 57 | 0 | `specs/particles1.ops` | 7 |
| quickchatcats | 269 | 0 | `specs/quickchatcats.ops` | 3 |
| quickchatlines | 3,039 | 0 | `specs/quickchatlines.ops` | 5 |
| sequences | 38,114 | 37,889 | `specs/sequences.ops` | 23 |
| spotanims | 9,284 | 0 | `specs/spotanims.ops` | 12 |
| structs | 53,173 | 35,470 | `specs/structs.ops` | 1 |
| underlays | 728 | 0 | `specs/underlays.ops` | 5 |
| varbits | 61,928 | 0 | `specs/varbits.ops` | 3 |
| varcampaign | 3,759 | 0 | `specs/varcampaign.ops` | 1 |
| varclan | 1,634 | 0 | `specs/varclan.ops` | 2 |
| varclansettings | 2,857 | 0 | `specs/varclansettings.ops` | 2 |
| varclient | 7,749 | 0 | `specs/varclient.ops` | 3 |
| varnpc | 989 | 0 | `specs/varnpc.ops` | 1 |
| varobject | 5 | 0 | `specs/varobject.ops` | 2 |
| varplayer | 13,378 | 0 | `specs/varplayer.ops` | 4 |
| varplayergroup | 646 | 0 | `specs/varplayergroup.ops` | 1 |
| varregion | 464 | 0 | `specs/varregion.ops` | 1 |
| varworld | 1,724 | 0 | `specs/varworld.ops` | 1 |

---

Previous: [Tools](tools.md) · Next: [The scrying pool](scrying-pool.md)
