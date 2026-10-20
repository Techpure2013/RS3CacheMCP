# Models

[All docs](README.md) › How it works

The cache has 145,735 3D models. Compressed they're 1.6 GB, and unpacked they're 5 GB, with 183
million vertices between them. `rs3mcp/models.py` reads them, and every rebuild saves a short summary
of each one in the database.

## In the database

Each model is a row in `config` with type `models`. Its `data` looks like this:

```json
{"format": 2, "version": 5, "meshes": 4, "vertices": 3646, "triangles": 2092, "materials": [4543],
 "skinned": false, "bones": true, "alpha": true, "box": {"min": [-131, -2, -406], "max": [131, 573, 696]}}
```

| Field | What it means |
|---|---|
| `meshes` | how many parts it has. Each part has one material |
| `vertices`, `triangles` | totals for the whole model |
| `materials` | the material ids it uses |
| `skinned`, `bones`, `alpha` | whether it has skin weights, bones, and see-through vertices |
| `box` | the space it takes up. Model units are the same as world units |

An npc or loc lists its model ids, so you can look up how big it is:

```
rs3 query "select json_extract(data,'$.box') from config where type='models' and id=95586"
```

That's the wolf. Its box is 262 x 575 x 1102.

## The format

A few things about the format are worth knowing:

- **LZMA.** Model files are compressed with LZMA. `rs3mcp/cachefile.py` unpacks them.
- **Float positions.** Some models store positions as floats instead of whole numbers. In one sample
  of 1,000 models, 166 were like this.
- **Counts that wrap.** A part's triangle count only goes up to 65,535, then starts again from 0.
  When the count doesn't fit, rs3cache adds 65,536 until it does.
- **Very big models.** Three models have more than 65,535 vertices. Those store their triangles
  differently.

A model only counts as read if every byte of it was used. All 145,735 are.

## The mesh

If you need the full shape and not just the summary, pass `mesh=True`. You get each part's positions,
colours and triangles.

```python
from rs3mcp import cachefile, models
files = cachefile.files(47, 95586)
summary, problem = models.decode(files[0], mesh=True)
```

`models.mesh(id)` gives you the same thing in one list, ready to draw: every vertex, every triangle
with its material, and colours and texture coordinates per vertex.

From a terminal:

```
python -m rs3mcp.models <id>          prints one model's summary
python -m rs3mcp.models verify [n]    reads every model, or the first n, and counts failures
```

`model_png.py`, `model_obj.py` and `rs3-icon.py` save models as pictures and obj files
([commands](commands.md)).

There's no 3D viewer yet. See [known gaps](known-gaps.md).

---

Previous: [Clientscripts](clientscripts.md) · Next: [The MCP server](mcp.md)
