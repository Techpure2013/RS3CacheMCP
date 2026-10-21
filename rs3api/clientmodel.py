"""The lab's client, built from what the live client was measured doing.

Three sources, and nothing else:

    facts.db        every call qd groundtruth made: what each kind of argument got back, the
                    message for each that threw, and the exact calls that took the client down
    the harvest     RS3_API_INFO/config_*.json, the fields the live client returned for every id of
                    a config type, under the client's own names; FromID answers from it
    the cache       whether an id exists, for a config type nobody harvested

model.lua holds the calls. The harvests are split into shards of SHARD ids under harvest/<type>/,
because obj alone is 54 MB and a lab run should load only the few ids it asks for.

The model does not invent. A call it has no measurement for, or a field it has no value for, stops
the run as a model gap, reported apart from the plugin's own errors, so a gap is never taken for a
bug in the plugin and a guess is never taken for the client.
"""

import json
import math
import os
import re
import sqlite3

from rs3mcp.common import ROOT

from . import groundtruth, paths

OUT = os.path.join(ROOT, "lab", "model")
MODEL = os.path.join(OUT, "model.lua")
HARVEST = os.path.join(OUT, "harvest")
SHARD = 1000
# cache ids fill harvest holes; tests switch off
CACHE_IDS = True

# the config namespace and its harvest file
HARVESTED = {"config.Obj": "obj", "config.NPC": "npc", "config.Seq": "seq", "config.Param": "param",
             "config.Enum": "enum", "config.Struct": "struct", "config.Inv": "inv",
             "config.Cursor": "cursor", "config.Font": "font", "config.BaseAnimSet": "baseanimset",
             "config.DBTable": "dbtable", "config.MapElement": "mapelement",
             "config.Headbar": "headbar", "config.Hitmark": "hitmark", "config.Quest": "quest"}
# the one harvest not called config_<name>.json
HARVEST_FILES = {"quest": "quest_configs.json"}
IDS = os.path.join(OUT, "ids")
VARS = os.path.join(OUT, "vars")


LUA_KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "goto", "if", "in",
                "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while"}


def _lua(v, depth=0):
    """A value as a Lua literal."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "nil"
        return str(int(v)) if v.is_integer() and abs(v) < 2 ** 53 else repr(v)
    if isinstance(v, str):
        return "[==[" + v.replace("]==]", "]==] .. ']==]' .. [==[") + "]==]" if "\n" in v or '"' in v or "\\" in v \
            else '"' + v + '"'
    if isinstance(v, list):
        return "{" + ", ".join(_lua(x, depth + 1) for x in v) + "}"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if isinstance(k, int):
                key = "[%d]" % k
            elif str(k).isidentifier() and str(k).isascii() and k not in LUA_KEYWORDS:
                key = k
            else:
                # spaced, or a long bracket key opens one
                key = "[ %s ]" % _lua(str(k))
            parts.append("%s = %s" % (key, _lua(x, depth + 1)))
        return "{" + ", ".join(parts) + "}"
    return _lua(str(v))


def _answer(outcome, detail):
    """One measured outcome as the model keeps it."""
    if outcome in ("throws", "skipped"):
        return {"o": outcome, "m": detail or ""}
    if outcome == "crashed":
        return {"o": "crashed"}
    if outcome != "returns":
        return None
    if detail in (None, "", "nothing"):
        return {"o": "returns", "t": "none"}
    try:
        shape = json.loads(detail)
    except ValueError:
        return {"o": "returns", "t": "unknown", "m": detail}
    ans = {"o": "returns", "t": shape.get("type")}
    if "value" in shape:
        ans["v"] = shape["value"]
    if shape.get("fields"):
        ans["fields"] = shape["fields"]
    if shape.get("values"):
        ans["values"] = shape["values"]
    if shape.get("meta"):
        ans["meta"] = shape["meta"]
    return ans


def _typed():
    """Field names by type, from the typings, for the objects lab.world makes that no measured call
    has returned yet (InvSlot: GetSlots was never called on a real inventory). Names only: the
    values are the world's to give."""
    try:
        from rs3api import api
        decls = api._all()
    except Exception:
        return {}
    out = {}
    for d in decls:
        if d.get("kind") in ("class", "interface") and d.get("name") and d.get("members"):
            out.setdefault(d["name"], {}).update({m: "declared" for m in d["members"] if m.isidentifier()})
    return out


def build_model(facts_path=None, build=None):
    """{api: {how: {argsLabel: answer}}} from every measured call."""
    db = sqlite3.connect(facts_path or groundtruth.FACTS)
    sql = "select api, how, args, outcome, detail from facts where kind = 'call'"
    args = []
    if build:
        sql += " and build = ?"
        args.append(build)
    calls = {}
    for api, how, label, outcome, detail in db.execute(sql, args):
        ans = _answer(outcome, detail)
        if ans is not None:
            calls.setdefault(api, {}).setdefault(how, {})[label] = ans
    values = {}
    vsql = "select api, detail from facts where kind = 'value'" + (" and build = ?" if build else "")
    for api, detail in db.execute(vsql, args):
        try:
            values[api] = json.loads(detail).get("value")
        except (ValueError, AttributeError):
            pass
    exists = {api: True for (api,) in db.execute(
        "select api from facts where kind = 'exists'" + (" and build = ?" if build else ""), args)}
    shapes = {}
    for api, detail in db.execute(
            "select api, detail from facts where kind = 'shape'" + (" and build = ?" if build else ""), args):
        try:
            s = json.loads(detail)
        except ValueError:
            continue
        shapes[api] = {"fields": s.get("fields") or {}, "values": s.get("values") or {}, "meta": s.get("meta")}
    # "index config.Quest[nil]" -> keys["config.Quest"]["[nil]"]
    keys = {}
    for api, outcome, detail in db.execute(
            "select api, outcome, detail from facts where kind = 'key'" + (" and build = ?" if build else ""), args):
        m = re.match(r"^index (.+?)(\[.*\])$", api)
        if m:
            keys.setdefault(m.group(1), {})[m.group(2)] = _answer(outcome, detail)
    events, gone = {}, {}
    esql = "select kind, api, detail from facts where kind in ('event', 'gone')" + (" and build = ?" if build else "")
    for kind, api, detail in db.execute(esql, args):
        try:
            shape = json.loads(detail)
        except ValueError:
            continue
        if kind == "event":
            events[api] = {"t": shape.get("type"), "fields": shape.get("fields") or {}, "meta": shape.get("meta")}
        else:
            # gone shapes keyed by their payload field name
            gone[api.rsplit(".", 1)[-1]] = {"fields": shape.get("fields") or {}, "meta": shape.get("meta")}
    db.close()
    idTypes = {os.path.splitext(f)[0]: True for f in os.listdir(IDS)} if os.path.isdir(IDS) else {}
    return {"calls": calls, "values": values, "events": events, "gone": gone, "types": _typed(), "exists": exists,
            "idTypes": idTypes, "idsDir": IDS.replace("\\", "/"), "shapes": shapes,
            "varsDir": VARS.replace("\\", "/"), "keys": keys,
            "harvested": {ns: name for ns, name in HARVESTED.items() if any(a.startswith(ns + ".") for a in calls)}}


def write_model(facts_path=None, build=None, out=None):
    model = build_model(facts_path, build)
    out = out or MODEL
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        # the run it was built from, first line
        fh.write("-- made by rs3api.clientmodel from facts.db; not hand edited\n-- build: %s\nreturn %s\n"
                 % (build or "every build", _lua(model)))
    return {"written": out, "calls": len(model["calls"]), "harvested": sorted(model["harvested"])}


def write_shards(name, out_dir=None, source=None):
    """Split RS3_API_INFO/config_<name>.json into shards of SHARD ids, once per harvest file."""
    source = source or os.path.join(paths.API_INFO, HARVEST_FILES.get(name, "config_%s.json" % name))
    out_dir = os.path.join(out_dir or HARVEST, name)
    stamp = os.path.join(out_dir, "source.stamp")
    mark = "%d:%d" % (os.path.getsize(source), int(os.path.getmtime(source))) if os.path.exists(source) else ""
    if not mark:
        return {"name": name, "shards": 0, "missing": source}
    if os.path.exists(stamp) and open(stamp).read() == mark:
        return {"name": name, "shards": len(os.listdir(out_dir)) - 1, "fresh": True}
    with open(source, encoding="utf-8") as fh:
        rows = (json.load(fh) or {}).get("rows") or []
    shards = {}
    for r in rows:
        if isinstance(r.get("id"), int):
            shards.setdefault(r["id"] // SHARD, {})[r["id"]] = r
    # the harvest misses ids the cache has
    ns = next((k for k, v in HARVESTED.items() if v == name), None)
    if ns and CACHE_IDS:
        from rs3mcp.common import connect
        for (cid,) in connect().execute("select id from config where api = ?", (ns,)):
            shard = shards.setdefault(cid // SHARD, {})
            if cid not in shard:
                shard[cid] = {"id": cid, "cacheOnly": True}
    os.makedirs(out_dir, exist_ok=True)
    for n, byid in shards.items():
        with open(os.path.join(out_dir, "%d.lua" % n), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("return " + _lua(byid) + "\n")
    # config.Inv.INV is the InvType the client named
    names = {r["configName"]: r["id"] for r in rows
             if isinstance(r.get("configName"), str) and isinstance(r.get("id"), int)}
    with open(os.path.join(out_dir, "names.lua"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("return " + _lua(names) + "\n")
    with open(stamp, "w") as fh:
        fh.write(mark)
    return {"name": name, "shards": len(shards), "rows": len(rows)}


def ensure(facts_path=None, build=None):
    """The model and the shards it needs, rebuilt when facts.db or a harvest has changed."""
    facts_path = facts_path or groundtruth.FACTS
    if not os.path.exists(facts_path):
        return {"error": "no client facts: run qd groundtruth, then rs3_client_facts import=true"}
    stale = not os.path.exists(MODEL) or os.path.getmtime(MODEL) < os.path.getmtime(facts_path)
    # mtime alone ties, so the build is checked
    if not stale:
        with open(MODEL, encoding="utf-8") as fh:
            fh.readline()
            stale = fh.readline().strip() != ("-- build: " + (build or "every build"))
    info = write_model(facts_path, build) if stale else {"written": MODEL, "fresh": True}
    for name in sorted(set(HARVESTED.values())):
        write_shards(name)
    write_id_lists()
    write_var_lists()
    return info


def write_id_lists(out_dir=None, source_dir=None):
    """id.<Type>.NAME for every type with a "Vars - <Type>.csv", the name lists taken off the live
    client, beyond the config types that have a harvest. One file a type, loaded when first asked."""
    import csv
    import glob
    import re
    out_dir = out_dir or IDS
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for path in sorted(glob.glob(os.path.join(source_dir or paths.API_INFO, "Vars - *.csv"))):
        m = re.match(r"Vars - (\w+)", os.path.basename(path))
        if not m:
            continue
        names = {}
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    names[row["CONFIG_NAME"]] = int(row["ID"])
                except (KeyError, TypeError, ValueError):
                    continue
        with open(os.path.join(out_dir, m.group(1) + ".lua"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("return " + _lua(names) + "\n")
        written.append(m.group(1))
    # and tables qd iddump read off the client
    for name, names in id_dumps().items():
        if not names:
            continue
        with open(os.path.join(out_dir, name + ".lua"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("return " + _lua(names) + "\n")
        if name not in written:
            written.append(name)
    return written


def write_var_lists(out_dir=None, source_dir=None):
    """What the client's own var lists say about each var: its id, its value type, and for a varbit
    its mask and the var it sits in. Only the value itself is left for a probe to give."""
    import csv
    import glob
    import re
    out_dir = out_dir or VARS
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for path in sorted(glob.glob(os.path.join(source_dir or paths.API_INFO, "Vars - Var*.csv"))):
        m = re.match(r"Vars - (Var\w*)", os.path.basename(path))
        if not m:
            continue
        vars_ = {}
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("CONFIG_NAME") or "").strip()
                try:
                    vid = int(row["ID"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not name:
                    continue
                mask = (row.get("Varbit Mask") or "").strip()
                entry = {"id": vid, "valueType": (row.get("ValueType") or "").strip(),
                         "typeName": m.group(1)}
                if mask and mask != "0x0":
                    try:
                        entry["varBitMask"] = int(mask, 16)
                    except ValueError:
                        pass
                base = (row.get("CONFIG_NAME - BaseVar") or "").strip()
                if base:
                    entry["baseVar"] = base
                vars_[name] = entry
        with open(os.path.join(out_dir, m.group(1) + ".lua"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("return " + _lua(vars_) + "\n")
        written.append(m.group(1))
    return written


def id_dumps(db_path=None):
    """{Type: {NAME: id}} from the qdevIDs.* rows a `qd iddump` wrote."""
    out = {}
    try:
        data = groundtruth.read(db_path)
    except Exception:
        return out
    db = sqlite3.connect("file:%s?mode=ro" % (db_path or groundtruth.PLUGIN_DATA), uri=True)
    try:
        raw = {k: v for k, v in db.execute("select Key, Value from DataStore where Key like 'qdevIDs.%'")}
    finally:
        db.close()

    def value(key):
        v = raw.get(key)
        if v is None:
            return None
        try:
            return groundtruth._unmap(json.loads(v.decode("utf-8", "replace").rstrip("\x00")))
        except ValueError:
            return None

    for name, n in (value("qdevIDs.index") or {}).items():
        names = {}
        for i in range(1, int(n) + 1):
            chunk = value("qdevIDs.%s.%d" % (name, i)) or []
            rows = list(chunk.values()) if isinstance(chunk, dict) else chunk
            for r in rows:
                if isinstance(r, dict) and isinstance(r.get("name"), str):
                    names[r["name"]] = int(r["id"])
        out[name] = names
    return out
