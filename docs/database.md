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

Every table in the database, `data/rs3-cache.db`. It's a normal SQLite file, built on 2026-10-20 22:07.

This page is written from the database itself by `rs3 docs`, which runs after every rebuild, so the numbers are always current.

To ask the database something, use `rs3 query` with a SELECT. `rs3 schema` shows the same tables, plus common mistakes to avoid.

## Tables

### config

864,212 rows.

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

160,820 rows.

| Column | Type | Key |
|---|---|---|
| `enum_id` | INTEGER |  |
| `key` | TEXT |  |
| `value` | TEXT |  |
| `key_type` | INTEGER |  |
| `value_type` | INTEGER |  |

Indexed: `ix_enum_key`, `ix_enum_entry`

### inv

957 rows.

| Column | Type | Key |
|---|---|---|
| `inv_id` | INTEGER | primary |
| `name` | TEXT |  |
| `size` | INTEGER |  |
| `entries` | INTEGER |  |

Indexed: `ix_inv_name`

### inv_stock

6,329 rows.

| Column | Type | Key |
|---|---|---|
| `inv_id` | INTEGER |  |
| `slot` | INTEGER |  |
| `item_id` | INTEGER |  |
| `quantity` | INTEGER |  |

Indexed: `ix_inv_stock_item`, `ix_inv_stock`

### loc_section

137,909 rows.

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

6,385 rows.

| Column | Type | Key |
|---|---|---|
| `model_id` | INTEGER |  |
| `iface_id` | INTEGER |  |
| `comp_id` | INTEGER |  |
| `comp_name` | TEXT |  |

Indexed: `ix_model_use`

### param_type

6,042 rows.

| Column | Type | Key |
|---|---|---|
| `param_id` | INTEGER | primary |
| `name` | TEXT |  |
| `vartype` | INTEGER |  |
| `valuetype` | TEXT |  |
| `target` | TEXT |  |

### param_use

780,961 rows.

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

36,569 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER | primary |
| `namehash` | INTEGER |  |

### sprite_name

672 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER | primary |
| `name` | TEXT |  |
| `namehash` | INTEGER |  |

Indexed: `ix_sprite_name_n`

### sprite_use

39,039 rows.

| Column | Type | Key |
|---|---|---|
| `sprite_id` | INTEGER |  |
| `source` | TEXT |  |
| `owner_type` | TEXT |  |
| `owner_id` | INTEGER |  |
| `owner_name` | TEXT |  |

Indexed: `ix_sprite_use`, `ix_sprite_owner`

### varbit

51,467 rows.

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
| VarClient | 7,821 | 7,681 | | |
| VarPlayer | 64,953 | 60,146 | | |
| VarPlayerGroup | 3 | 3 | | |
| achievements | 5,119 | 4,901 | `specs/achievements.ops` | 33 |
| animgroupconfigs | 4,935 | 4,892 | `specs/animgroupconfigs.ops` | 24 |
| cutscenes | 9 | 0 | `specs/cutscenes.ops` | one record shape |
| dbrows | 20,366 | 15,340 | `specs/dbrows.ops` | 2 |
| dbtables | 391 | 242 | `specs/dbtables.ops` | 1 |
| enums | 16,791 | 8,501 | `specs/enums.ops` | 12 |
| environments | 198 | 0 | `specs/environments.ops` | 4 |
| fontmetrics | 225 | 0 | `specs/fontmetrics.ops` | one record shape |
| identitykit | 1,096 | 0 | `specs/identitykit.ops` | 6 |
| interfaces | 104,423 | 103,978 | `specs/interfaces.ops` | one record shape |
| items | 63,936 | 61,292 | `specs/items.ops` | 88 |
| maplabels | 5,843 | 0 | `specs/maplabels.ops` | 24 |
| mapscenes | 150 | 0 | `specs/mapscenes.ops` | 5 |
| mapzones | 771 | 0 | `specs/mapzones.ops` | one record shape |
| materials | 25,329 | 0 | `specs/materials.ops` | one record shape |
| models | 146,817 | 0 | | |
| npcs | 32,972 | 32,679 | `specs/npcs.ops` | 74 |
| objects | 141,395 | 136,951 | `specs/objects.ops` | 98 |
| overlays | 609 | 0 | `specs/overlays.ops` | 12 |
| params | 9,502 | 5,980 | `specs/params.ops` | 2 |
| particles0 | 4,521 | 0 | `specs/particles0.ops` | 33 |
| particles1 | 57 | 0 | `specs/particles1.ops` | 7 |
| quickchatcats | 270 | 0 | `specs/quickchatcats.ops` | 3 |
| quickchatlines | 3,057 | 0 | `specs/quickchatlines.ops` | 5 |
| sequences | 38,297 | 37,834 | `specs/sequences.ops` | 23 |
| spotanims | 9,480 | 0 | `specs/spotanims.ops` | 12 |
| structs | 53,611 | 35,611 | `specs/structs.ops` | 1 |
| underlays | 732 | 0 | `specs/underlays.ops` | 5 |
| varbits | 62,288 | 0 | `specs/varbits.ops` | 3 |
| varcampaign | 3,798 | 0 | `specs/varcampaign.ops` | 1 |
| varclan | 1,634 | 0 | `specs/varclan.ops` | 2 |
| varclansettings | 2,857 | 0 | `specs/varclansettings.ops` | 2 |
| varclient | 7,801 | 0 | `specs/varclient.ops` | 3 |
| varnpc | 994 | 0 | `specs/varnpc.ops` | 1 |
| varobject | 5 | 0 | `specs/varobject.ops` | 2 |
| varplayer | 13,477 | 0 | `specs/varplayer.ops` | 5 |
| varplayergroup | 648 | 0 | `specs/varplayergroup.ops` | 1 |
| varregion | 464 | 0 | `specs/varregion.ops` | 1 |
| varworld | 1,724 | 0 | `specs/varworld.ops` | 1 |

---

Previous: [Tools](tools.md) · Next: [The scrying pool](scrying-pool.md)
