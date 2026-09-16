"""
Build rs3-cache.db from a stage written by rs3mcp/extract.py and, if there are any, config name lists.

    python build_db.py <stage> <csvdir> <out.db> [--carry <old.db>]

Three sources, and it matters which is which:

  the stage      json per config, decoded by our specs. This is the cache, and it is what a game
                 update moves.
  the csvs       config name lists, `Vars - <type>.csv`. Only names: the varbits themselves come
                 from the cache. Optional; without them configs go in unnamed.
  the old db     tables nothing here can derive: shop stock, which no spec reads yet and was worked
                 out empirically once; sprite names; loc sections. --carry copies them forward
                 rather than pretending to rebuild them, and a first build simply has none.

param_use is the `params` map on every config, flattened. That is 765k rows and the reason a query
can ask what an item weighs without parsing json.
"""

import csv
import io
import json
import os
import sqlite3
import sys
import time

# stage directory -> the type name the database uses, where they differ
STAGE_TYPE = {"items": "items", "objects": "objects", "npcs": "npcs"}

# type -> what an add-on says reads it, for config.api. Empty without one
API = {}

# "Vars - X.csv" -> the type it names
CSV_TYPE = {
    "Obj": "items", "Loc": "objects", "NPC": "npcs", "Seq": "sequences", "Struct": "structs",
    "Interface": "interfaces", "Enum": "enums", "Param": "params", "DBRow": "dbrows",
    "DBTable": "dbtables", "Achievement": "achievements", "BaseAnimSet": "animgroupconfigs",
    "VarPlayer": "VarPlayer", "VarClient": "VarClient", "VarPlayerGroup": "VarPlayerGroup",
    "Quest": "Quest", "Inv": "Inv", "Font": "Font", "Cursor": "Cursor", "Music": "Music",
    "StyleSheet": "StyleSheet",
}

# types that exist only in the csvs, so a missing stage directory for them is not a problem
CSV_ONLY = {"VarPlayer", "VarClient", "VarPlayerGroup", "Quest", "Inv", "Font", "Cursor",
            "Music", "StyleSheet"}

# A param's vartype says what kind of thing its value names, which is what lets a number be
# resolved to a config rather than left as a number. Anything not listed holds a plain value.
VARTYPE_TARGET = {
    3: "Quest", 5: "Cursor", 6: "sequences", 9: "interfaces", 25: "Font", 26: "enums",
    30: "objects", 32: "npcs", 33: "items", 37: "spotanims", 39: "Inv",
    44: "animgroupconfigs", 73: "structs", 74: "dbrows", 131: "achievements",
}

# not derivable from the cache: some sprite names were collected by hand, and cracking the hashes
# does not reach all of them
CARRIED = ["sprite_name"]

SCHEMA = """
CREATE TABLE config (
    type    TEXT NOT NULL,
    id      INTEGER NOT NULL,
    name    TEXT,
    api     TEXT,
    data    TEXT,
    display TEXT,
    PRIMARY KEY (type, id)
);
CREATE TABLE varbit (
    id        INTEGER NOT NULL,
    name      TEXT,
    domain    TEXT,
    base_name TEXT,
    base_id   INTEGER,
    mask      INTEGER,
    shift     INTEGER,
    PRIMARY KEY (domain, id)
);
CREATE TABLE param_type (
    param_id  INTEGER PRIMARY KEY,
    name      TEXT,
    vartype   INTEGER,
    valuetype TEXT,
    target    TEXT
);
CREATE TABLE param_use (
    owner_type  TEXT NOT NULL,
    owner_id    INTEGER NOT NULL,
    param_id    INTEGER NOT NULL,
    int_value   INTEGER,
    str_value   TEXT,
    target_type TEXT,
    target_name TEXT
);
CREATE TABLE enum_entry (
    enum_id    INTEGER NOT NULL,
    key        TEXT,
    value      TEXT,
    key_type   INTEGER,
    value_type INTEGER
);
CREATE TABLE inv (inv_id INTEGER PRIMARY KEY, name TEXT, size INTEGER, entries INTEGER);
CREATE TABLE inv_stock (
    inv_id   INTEGER NOT NULL,
    slot     INTEGER NOT NULL,
    item_id  INTEGER NOT NULL,
    quantity INTEGER
);
CREATE TABLE loc_section (loc_id INTEGER PRIMARY KEY, section TEXT, placed INTEGER);
CREATE TABLE model_use (
    model_id  INTEGER NOT NULL,
    iface_id  INTEGER NOT NULL,
    comp_id   INTEGER NOT NULL,
    comp_name TEXT
);
CREATE TABLE sprite_name (sprite_id INTEGER PRIMARY KEY, name TEXT, namehash INTEGER);
CREATE TABLE sprite_hash (sprite_id INTEGER PRIMARY KEY, namehash INTEGER);
CREATE TABLE sprite_use (
    sprite_id  INTEGER NOT NULL,
    source     TEXT NOT NULL,
    owner_type TEXT,
    owner_id   INTEGER,
    owner_name TEXT
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

INDICES = """
CREATE INDEX config_name ON config(name);
CREATE INDEX ix_config_display on config(type, display);
CREATE INDEX ix_config_name on config(type, name);
CREATE INDEX ix_enum_entry on enum_entry(enum_id);
CREATE INDEX ix_enum_key on enum_entry(key_type, key);
CREATE INDEX ix_inv_name on inv(name);
CREATE INDEX ix_inv_stock on inv_stock(inv_id);
CREATE INDEX ix_inv_stock_item on inv_stock(item_id);
CREATE INDEX ix_loc_section on loc_section(section);
CREATE INDEX ix_model_use on model_use(model_id);
CREATE INDEX ix_param_owner on param_use(owner_type, owner_id);
CREATE INDEX ix_param_pid on param_use(param_id);
CREATE INDEX ix_sprite_name_n on sprite_name(name);
CREATE INDEX ix_sprite_owner on sprite_use(owner_name);
CREATE INDEX ix_sprite_use on sprite_use(sprite_id);
CREATE INDEX ix_varbit_base on varbit(base_id);
CREATE INDEX ix_varbit_name on varbit(name);
CREATE INDEX pu_str ON param_use(str_value);
CREATE INDEX pu_target ON param_use(target_type, target_name);
"""


def say(msg):
    print("  " + msg, flush=True)


def read_csv(path):
    with io.open(path, encoding="utf-8", errors="replace", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def load_names(csvdir):
    """type -> {id: CONFIG_NAME}, and the varbit rows, both out of the csvs.

    Varbits only exist here. They come down the same VarPlayer sheet as the base vars, as negative
    ids carrying the base they pack into and their mask, and the cache has no notion of them.
    """
    names, varbits, valuetypes = {}, [], {}
    if not os.path.isdir(csvdir):
        say("no name csvs at %s: configs go in unnamed, and varbits come from the cache alone" % csvdir)
        return names, varbits, valuetypes
    for fn in sorted(os.listdir(csvdir)):
        if not fn.startswith("Vars - ") or not fn.endswith(".csv"):
            continue
        key = fn[len("Vars - "):-4].split(" (")[0]
        typ = CSV_TYPE.get(key)
        if typ is None:
            continue
        head, rows = read_csv(os.path.join(csvdir, fn))
        col = {h: i for i, h in enumerate(head)}
        out = {}
        for r in rows:
            if not r or not r[0].lstrip("-").isdigit():
                continue
            cid, cname = int(r[0]), (r[1] if len(r) > 1 else None)
            if cid < 0:
                # a varbit: name, the base var it packs into, and which bits
                mask = r[col["Varbit Mask"]] if "Varbit Mask" in col and len(r) > col["Varbit Mask"] else ""
                base = r[col["BaseVar ID"]] if "BaseVar ID" in col and len(r) > col["BaseVar ID"] else ""
                m = int(mask, 16) if mask.startswith("0x") else (int(mask) if mask.isdigit() else None)
                # a couple of varbits carry a zero mask, which is a listener rather than a slice;
                # its shift is 0, not unknown
                shift = 0 if not m else (m & -m).bit_length() - 1
                varbits.append((cid, cname, typ,
                                r[col["CONFIG_NAME - BaseVar"]] if "CONFIG_NAME - BaseVar" in col else None,
                                int(base) if base.isdigit() else None,
                                m, shift if m is not None else None))
                continue
            out[cid] = cname
            if typ == "params" and "INPUT_VALUETYPE" in col and len(r) > col["INPUT_VALUETYPE"]:
                valuetypes[cid] = r[col["INPUT_VALUETYPE"]]
        names[typ] = out
        say("csv %-18s %d names" % (key, len(out)))
    return names, varbits, valuetypes


# the domain a varbit's var sits in, packed into the top byte of its var
VAR_DOMAIN = {0: "VarPlayer", 1: "VarNpc", 2: "VarClient", 3: "VarWorld", 4: "VarRegion",
              5: "VarObject", 6: "VarClan", 7: "VarClanSettings", 8: "VarCampaign",
              9: "VarPlayerGroup"}


def cache_varbits(config, names, from_csv):
    """Varbit rows out of the decoded varbit configs, named from the csv where it knows them.
    Ids go negative, -(id + 1), the way the name lists number them.

    Player varbits only. Everything reading this table joins on base_id alone, and a varclient
    varbit on base 7799 is not a slice of varplayer 7799. The others stay in config as varbits."""
    csv_names = {vid: vname for vid, vname, *_ in from_csv}
    out = []
    for typ, cid, _n, _a, data, _d in config:
        if typ != "varbits" or not data:
            continue
        d = json.loads(data)
        var, bits = d.get("var"), d.get("bits") or {}
        if var is None or "low" not in bits or var >> 16:
            continue
        domain = VAR_DOMAIN.get(var >> 16, "Var%d" % (var >> 16))
        base, lo, hi = var & 0xFFFF, bits["low"], bits["high"]
        vid = -(cid + 1)
        out.append((vid, csv_names.get(vid), domain, (names.get(domain) or {}).get(base), base,
                    ((1 << (hi - lo + 1)) - 1) << lo, lo))
    return out


def ptype_target(config):
    """param id -> the config type its value names, for the params that name one."""
    out = {}
    for typ, cid, _n, _a, data, _d in config:
        if typ != "params" or not data:
            continue
        vartype = (json.loads(data).get("type") or {}).get("varType") or 0
        target = VARTYPE_TARGET.get(vartype)
        if target:
            out[cid] = target
    return out


def resolve_targets(params, targets, config):
    """Fill in what each param value points at.

    A param use is a number until you know the param types it. `AMMO_MOULD` is more use than 4,
    and it is the difference between the database answering "what does this item smith into" and
    handing back an id to look up separately.
    """
    named = {}
    for typ, cid, name, _a, _data, _d in config:
        if name is not None:
            named[(typ, cid)] = name
    out = []
    for owner_type, owner_id, pid, ival, sval, _tt, _tn in params:
        # The type is recorded even when the id has no name. Knowing a value is a spotanim is
        # worth having on its own, and dropping the type because the name lists never named that
        # spotanim would lose it for 3,691 uses.
        target = targets.get(pid)
        name = named.get((target, ival)) if target is not None and ival is not None else None
        out.append((owner_type, owner_id, pid, ival, sval, target, name))
    return out


def enum_pairs(cid, data):
    """Every key/value pair in an enum, from all four containers it can use.

    The dense forms, intArray and stringArray, hold {size, values} with an index per value. The
    sparse forms, ints and strings, hold a key per value. Reading only one is how this table came
    out short once before, so all four are read.
    """
    # keyChar and valueChar, the old one byte types, sit on two enums out of 16,694. The database
    # never recorded them, so only keyType and valueType are kept.
    ktype = data.get("keyType")
    vtype = data.get("valueType")
    out = []
    for key in ("intArray", "stringArray"):
        for e in (data.get(key) or {}).get("values") or []:
            out.append((cid, str(e["index"]), str(e["value"]), ktype, vtype))
    for key in ("ints", "strings"):
        for e in data.get(key) or []:
            out.append((cid, str(e["key"]), str(e["value"]), ktype, vtype))
    return out


def stage_rows(stagedir):
    """(type, [(id, data json, display)], param uses, enum pairs) for every type in the extract,
    one batch file per type, read in separate processes since they have nothing to do with each
    other.

    An interface component's id is already packed as interface << 16 | component, the number
    `rs3_iface` splits.
    """
    import multiprocessing
    paths = [(typ, os.path.join(stagedir, typ, typ + ".batch.json")) for typ in sorted(os.listdir(stagedir))]
    paths = [p for p in paths if os.path.exists(p[1])]
    with multiprocessing.get_context("spawn").Pool(min(len(paths), os.cpu_count() or 1) or 1) as pool:
        for typ, rows, uses, pairs, shows in pool.imap(_stage_type, paths):
            if rows:
                say("stage %-18s %d configs" % (typ, len(rows)))
            yield typ, rows, uses, pairs, shows


def _stage_type(job):
    typ, path = job
    rows, uses, pairs, shows = [], [], [], []
    for blob in json.load(io.open(path, encoding="utf-8"))["files"]:
        cid = blob.pop("$fileid")
        rows.append((cid, json.dumps(blob, separators=(",", ":")), blob.get("name")))
        for key, v in (blob.get("params") or {}).items():
            uses.append((typ, cid, int(key), v if isinstance(v, int) else None,
                         v if isinstance(v, str) else None, None, None))
        if typ == "enums":
            pairs += enum_pairs(cid, blob)
        if typ == "interfaces":
            # what a component draws: (kind, id, interface, component)
            for kind in ("model", "sprite"):
                block = blob.get(kind)
                if isinstance(block, dict) and isinstance(block.get(kind), int) and block[kind] >= 0:
                    shows.append((kind, block[kind], cid >> 16, cid & 0xFFFF))
    return typ, rows, uses, pairs, shows


def build(stagedir, csvdir, out, carry):
    from rs3mcp import addons, cachefile
    API.update(addons.first("api_names") or {})
    if os.path.exists(out):
        raise SystemExit("refusing to overwrite an existing %s" % out)
    db = sqlite3.connect(out)
    # a fresh file nothing reads until it is finished: a crash means building it again
    db.execute("pragma journal_mode=off")
    db.execute("pragma synchronous=off")
    db.executescript(SCHEMA)

    say("reading the csvs")
    names, varbits, valuetypes = load_names(csvdir)

    say("reading the stage")
    config, params, enums, shows, inv_rows = [], [], [], [], []
    for typ, rows, uses, pairs, drawn in stage_rows(stagedir):
        # config already has the Inv type, named from the csv; the decoded side is inv and inv_stock
        if typ == "invs":
            inv_rows = rows
            continue
        # every component of an interface answers to its interface's name
        lookup = names.get(typ) or {}
        for cid, data, display in rows:
            name = lookup.get(cid >> 16) if typ == "interfaces" else lookup.get(cid)
            config.append((typ, cid, name, API.get(typ), data, display))
        params += uses
        enums += pairs
        shows += drawn

    # the csv-only types have no cache side at all
    for typ in CSV_ONLY:
        for cid, cname in (names.get(typ) or {}).items():
            config.append((typ, cid, cname, API.get(typ), None, None))

    # Base vars are where the tools look vars up, and the name lists only have the ones they name.
    # The cache has them all, so the rest go in unnamed, and a copy without the lists still has them.
    have = {(t, c) for t, c, _n, _a, _d, _s in config if t in ("VarPlayer", "VarClient")}
    extra = [(domain, c, None, API.get(domain), None, None)
             for t, c, _n, _a, _d, _s in config
             for page, domain in (("varplayer", "VarPlayer"), ("varclient", "VarClient"))
             if t == page and (domain, c) not in have]
    config += extra

    # Varbits come from the cache, which is the game as it is now; the csv only lends them names.
    # Its own bits went stale wherever Jagex repacked a varbit after the lists were made, 1,310 of
    # them by 2026-09-13, most of them league tasks moved to new base vars.
    varbits = cache_varbits(config, names, varbits) or varbits

    # A varbit is addressable as a VarPlayer in its own right, as a negative id, so it belongs in
    # config beside the base vars as well as in the varbit table.
    for vid, vname, domain, _base_name, _base_id, _mask, _shift in varbits:
        if domain == "VarPlayer":
            config.append((domain, vid, vname, API.get(domain), None, None))

    say("writing %d config rows" % len(config))
    db.executemany("insert or replace into config values (?,?,?,?,?,?)", config)
    say("resolving param targets")
    params = resolve_targets(params, ptype_target(config), config)
    say("writing %d param uses" % len(params))
    db.executemany("insert into param_use values (?,?,?,?,?,?,?)", params)
    say("writing %d enum entries" % len(enums))
    db.executemany("insert into enum_entry values (?,?,?,?,?)", enums)
    say("writing %d varbits" % len(varbits))
    db.executemany("insert or replace into varbit values (?,?,?,?,?,?,?)", varbits)

    # A param row exists for every param the cache actually types. Most of the 9,420 param configs
    # are empty shells; the 6,002 carrying a `type` block are the real ones, and the listed
    # names are a subset of those. vartype says what the value points at, which is what turns a
    # number into "that is an item id".
    named_params = names.get("params") or {}
    ptypes = []
    for typ, cid, _n, _a, data, _d in config:
        if typ != "params" or not data:
            continue
        block = (json.loads(data).get("type") or {})
        if not block:
            continue
        vartype = block.get("varType") or 0
        ptypes.append((cid, named_params.get(cid), vartype, valuetypes.get(cid),
                       VARTYPE_TARGET.get(vartype)))
    say("writing %d param types" % len(ptypes))
    db.executemany("insert or replace into param_type values (?,?,?,?,?)", ptypes)

    # What uses which model and sprite, from this extract rather than carried from an old database:
    # interface components that draw one, and params typed graphic on everything else
    iface_names = names.get("interfaces") or {}
    model_use = [(i, iface, comp, iface_names.get(iface)) for kind, i, iface, comp in shows if kind == "model"]
    sprite_use = [(i, "iface", "interfaces", iface, iface_names.get(iface))
                  for kind, i, iface, comp in shows if kind == "sprite"]
    graphic = {cid: (nm or "param_%d" % cid) for cid, nm, vartype, _v, _t in ptypes if vartype == 23}
    owners = {(typ, cid): nm for typ, cid, nm, _a, _d, _s in config if nm}
    for owner_type, owner_id, pid, ival, _s, _t, _n in params:
        if pid in graphic and ival is not None and ival >= 0:
            sprite_use.append((ival, graphic[pid], owner_type, owner_id, owners.get((owner_type, owner_id))))
    say("writing %d model uses, %d sprite uses" % (len(model_use), len(sprite_use)))
    db.executemany("insert into model_use values (?,?,?,?)", model_use)
    db.executemany("insert into sprite_use values (?,?,?,?,?)", sprite_use)
    hashes = [(g, v["nameHash"]) for g, v in sorted((cachefile.descriptor(8) or {}).items())
              if v.get("nameHash") is not None]
    db.executemany("insert or replace into sprite_hash values (?,?)", hashes)

    invs = {iid: [nm, None, None] for iid, nm in (names.get("Inv") or {}).items()}
    stock = []
    for iid, data, _display in inv_rows:
        d = json.loads(data)
        lines = d.get("stock") or []
        invs.setdefault(iid, [None, None, None])[1:] = [d.get("size"), len(lines)]
        stock += [(iid, slot, s["item"], s["quantity"]) for slot, s in enumerate(lines)]

    if carry:
        old = sqlite3.connect(carry)
        # the cache has no names for inventories, and the csv lacks some an older build had
        try:
            for iid, nm in old.execute("select inv_id, name from inv where name is not null"):
                if iid in invs and not invs[iid][0]:
                    invs[iid][0] = nm
        except sqlite3.Error:
            pass
        for t in CARRIED:
            try:
                rows = old.execute("select * from %s" % t).fetchall()
            except sqlite3.Error:
                say("carry %-14s not in the old db, skipped" % t)
                continue
            if not rows:
                continue
            db.executemany("insert into %s values (%s)"
                           % (t, ",".join("?" * len(rows[0]))), rows)
            say("carry %-14s %d rows" % (t, len(rows)))
        old.close()

    # names cracked from the hashes, over and beyond the carried ones, so a first build has some
    from rs3mcp import spritenames
    words = {n for _t, _c, n, _a, _d, _s in config if n}
    words.update(p[1] for p in ptypes if p[1])
    words.update(e[2] for e in enums if e[4] == 36)
    for t, _c, _n, _a, d, _s in config:
        if d and t in ("dbrows", "structs", "mapzones", "maplabels", "achievements"):
            words.update(spritenames.STRING.findall(d))
    carried = [n for (n,) in db.execute("select name from sprite_name")]
    cracked = spritenames.crack(hashes, words, carried)
    before = db.execute("select count(*) from sprite_name").fetchone()[0]
    db.executemany("insert or ignore into sprite_name values (?,?,?)",
                   [(sid, nm, spritenames.name_hash(nm)) for sid, nm in sorted(cracked.items())])
    gained = db.execute("select count(*) from sprite_name").fetchone()[0] - before
    if carried:
        say("cracked %d sprite names, %d beyond the %d carried" % (len(cracked), gained, len(carried)))
    else:
        say("cracked %d sprite names" % len(cracked))

    from rs3mcp import areas
    sections = areas.sections([d for t, _c, _n, _a, d, _s in config if t == "mapzones" and d],
                              [(c, n) for t, c, n, _a, _d, _s in config if t == "objects"])
    say("writing %d loc sections" % len(sections))
    db.executemany("insert or replace into loc_section values (?,?,?)", sections)

    say("writing %d inventories, %d stock lines" % (len(invs), len(stock)))
    db.executemany("insert into inv values (?,?,?,?)", [(iid,) + tuple(v) for iid, v in sorted(invs.items())])
    db.executemany("insert into inv_stock values (?,?,?,?)", stock)

    db.executemany("insert or replace into meta values (?,?)", [
        ("cache", cachefile.GAME),
        ("stage", stagedir), ("csvs", csvdir),
        ("configs", str(len(config))), ("param_uses", str(len(params))),
        ("varbits", str(len(varbits))), ("enum_entries", str(len(enums))),
        ("built", time.strftime("%Y-%m-%dT%H:%M:%S")),
    ])

    say("indexing")
    db.executescript(INDICES)
    db.commit()
    db.execute("analyze")
    db.commit()
    db.close()
    say("done: %s" % out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    carry = None
    if "--carry" in sys.argv:
        carry = sys.argv[sys.argv.index("--carry") + 1]
        args = [a for a in args if a != carry]
    if len(args) != 3:
        raise SystemExit(__doc__)
    build(args[0], args[1], args[2], carry)


if __name__ == "__main__":
    main()
