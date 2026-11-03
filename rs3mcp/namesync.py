"""The config name lists brought up to what a running client calls things.

    python -m rs3mcp.namesync [harvest folder] [--check]

The lists (`Vars - <type>.csv`) are where a build gets every name the cache does not carry: vars,
varbits, interfaces, quests. They were a snapshot, so a quest released after it came out nameless:
`QUEST_WENDLE_SECRETS_MAIN` was in the client and in none of the lists by 2026-11-02, and rs3_var
and rs3_quest could not find Secrets of Amberfell by name.

The config-harvest plugin posts every config the client has, configName included, through
quest-harvest-server.py into config_<type>.json. This adds each id the lists lack and renames an id
the client now calls something else, in the shipped lists and in the folder a build reads when that
is another one. The client is the authority on names. Nothing is removed: an id the client no
longer lists may only not have been walked.

Varbits come down the VarPlayer and VarClient sheets as negative ids, with the base var they pack
into and their mask. A value type the harvest gives as a number is written by the name the lists
already use for that number, learned from the ids both sides have.
"""

import csv
import io
import json
import os
import sys

from . import common

# harvested types that have a list
LISTS = ["Inv", "Cursor", "Font", "BaseAnimSet", "Param", "DBTable", "Struct", "Enum", "Seq",
         "NPC", "Obj", "Loc", "VarPlayer", "VarClient"]
# the value type column, by list
VALUE_COLUMN = {"VarPlayer": "ValueType", "VarClient": "ValueType", "Param": "INPUT_VALUETYPE"}


def _list_file(folder, typ):
    """The list for a type, `Vars - Obj.csv` or a copy saved as `Vars - VarPlayer (1).csv`."""
    for f in sorted(os.listdir(folder)):
        if f.startswith("Vars - ") and f.endswith(".csv") and f[len("Vars - "):-4].split(" (")[0] == typ:
            return os.path.join(folder, f)
    return None


def _read(path):
    with io.open(path, encoding="utf-8", errors="replace", newline="") as fh:
        rows = list(csv.reader(fh))
    return (rows[0], rows[1:]) if rows else ([], [])


def _harvest(folder, typ):
    path = os.path.join(folder, "config_%s.json" % typ.lower())
    if not os.path.exists(path):
        return None
    got = json.load(io.open(path, encoding="utf-8"))
    rows = got.get("rows") if isinstance(got, dict) else got
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("id"), int) and r.get("configName")]


def merge(typ, harvested, path):
    """The list at path with the harvest folded in: (header, rows, added, renamed)."""
    head, rows = _read(path)
    col = {h: i for i, h in enumerate(head)}
    by_id = {}
    for r in rows:
        if r and r[0].lstrip("-").isdigit():
            by_id[int(r[0])] = r
    vcol = col.get(VALUE_COLUMN.get(typ))
    # value type number to name
    names_for = {}
    if vcol is not None:
        for h in harvested:
            r = by_id.get(h["id"])
            if r is not None and isinstance(h.get("valueType"), int) and len(r) > vcol and r[vcol]:
                names_for.setdefault(h["valueType"], r[vcol])
    base_names = {h["id"]: h["configName"] for h in harvested if h["id"] >= 0}
    base_names.update({int(r[0]): r[1] for r in rows if r and r[0].isdigit() and len(r) > 1})
    added, renamed = [], []
    for h in sorted(harvested, key=lambda h: h["id"]):
        cid, name = h["id"], h["configName"]
        r = by_id.get(cid)
        if r is not None:
            if len(r) > 1 and r[1] != name:
                renamed.append((cid, r[1], name))
                r[1] = name
            continue
        new = [""] * len(head)
        new[0], new[1] = str(cid), name
        if vcol is not None and h.get("valueType") in names_for:
            new[vcol] = names_for[h["valueType"]]
        if cid < 0 and "BaseVar ID" in col and isinstance(h.get("baseVarId"), int):
            new[col["BaseVar ID"]] = str(h["baseVarId"])
            if "CONFIG_NAME - BaseVar" in col:
                new[col["CONFIG_NAME - BaseVar"]] = base_names.get(h["baseVarId"], "")
            if "Varbit Mask" in col and isinstance(h.get("varBitMask"), int):
                new[col["Varbit Mask"]] = "0x%X" % h["varBitMask"]
        rows.append(new)
        added.append((cid, name))
    return head, rows, added, renamed


def folders():
    """The shipped lists, and the folder a build reads when that is another one."""
    out = [common.NAMES]
    other = common.name_lists()
    if other and os.path.normcase(os.path.abspath(other)) != os.path.normcase(os.path.abspath(common.NAMES)):
        out.append(other)
    return out


def sync(harvest_folder=None, check=False, say=print):
    """{list file: (added, renamed)} across every folder() for every type harvested.

    The harvest folder is where quest-harvest-server.py writes, which the add-on names."""
    if harvest_folder is None:
        try:
            from rs3api.paths import API_INFO as harvest_folder
        except ImportError:
            harvest_folder = None
    if not harvest_folder or not os.path.isdir(harvest_folder):
        raise SystemExit("no harvest folder: name one, or run quest-harvest-server.py with the add-on")
    report = {}
    for typ in LISTS:
        harvested = _harvest(harvest_folder, typ)
        if not harvested:
            continue
        for folder in folders():
            path = _list_file(folder, typ)
            if path is None:
                continue
            head, rows, added, renamed = merge(typ, harvested, path)
            report[path] = (added, renamed)
            if (added or renamed) and not check:
                with io.open(path, "w", encoding="utf-8", newline="") as fh:
                    csv.writer(fh).writerows([head] + rows)
            if added or renamed:
                say("%-34s %5d added, %4d renamed%s, e.g. %s" % (
                    os.path.basename(path), len(added), len(renamed),
                    " (would be)" if check else "", (added or renamed)[:2]))
    return report


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sync(args[0] if args else None, check="--check" in sys.argv)
