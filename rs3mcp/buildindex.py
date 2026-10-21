"""
Build the sidecar index. Run it when rs3-cache.db is regenerated; nothing else needs it.

    python rs3mcp/buildindex.py

rs3-cache.db is a build artifact of the cache extract and is left untouched, so the index lives beside
it in rs3-index.db. Every reader falls back to the slow path when the sidecar is missing, which
means a stale or absent index degrades speed and never correctness.

What it fixes: _item_family answered "who links to this item" with `data LIKE '%"noteLink":6,%'`
once per field per suffix, sixteen full scans of 63,936 rows carrying JSON. That was 1.1 seconds on
every rs3_trace call. Precomputed it is a keyed lookup.
"""
import io
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time

from rs3mcp.common import ROOT  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "rs3-cache.db")
INDEX = os.path.join(ROOT, "rs3-index.db")

ITEM_LINK_FIELDS = ["noteLink", "noteTemplate", "lentLink", "lentTemplate",
                    "boughtLink", "boughtTemplate", "shardLink", "shardTemplate", "recipeTemplate"]


def build():
    if not os.path.exists(CACHE):
        raise SystemExit("no cache at " + CACHE)
    t0 = time.time()
    src = sqlite3.connect("file:" + CACHE.replace("\\", "/") + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    # built beside the live one, then moved over
    staging = INDEX + ".new"
    if os.path.exists(staging):
        os.remove(staging)
    dst = sqlite3.connect(staging)
    dst.execute("pragma journal_mode=off")
    dst.execute("pragma synchronous=off")
    dst.execute("create table item_link (field text, target_id integer, owner_id integer, "
                "owner_name text, owner_display text)")

    rows = 0
    batch = []
    for r in src.execute("select id,name,display,data from config where type='items' "
                         "and data is not null"):
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        for f in ITEM_LINK_FIELDS:
            v = d.get(f)
            if isinstance(v, int) and v >= 0:
                batch.append((f, v, r["id"], r["name"], r["display"]))
        if len(batch) >= 20000:
            dst.executemany("insert into item_link values (?,?,?,?,?)", batch)
            rows += len(batch)
            batch = []
    if batch:
        dst.executemany("insert into item_link values (?,?,?,?,?)", batch)
        rows += len(batch)

    # morph edges: which parent wears which child
    dst.execute("create table morph_edge (type text, parent_id integer, child_id integer, "
                "slot text)")
    from rs3mcp.cache import MORPHS, morph_children, NONE_CHILD
    medges = 0
    for t in ("npcs", "objects"):
        batch = []
        for r in src.execute("select id, data from config where type=? and data is not null "
                             "and data like '%\"morphs%'", (t,)):
            try:
                d = json.loads(r["data"])
            except Exception:
                continue
            for key in MORPHS:
                m = d.get(key)
                if not isinstance(m, dict):
                    continue
                lst, fallback = morph_children(m)
                seen = set()
                for child in list(lst) + ([fallback] if isinstance(fallback, int) else []):
                    if not isinstance(child, int) or child in seen:
                        continue
                    if child == NONE_CHILD.get(t) or child == r["id"]:
                        continue
                    seen.add(child)
                    batch.append((t, r["id"], child, key))
            if len(batch) >= 20000:
                dst.executemany("insert into morph_edge values (?,?,?,?)", batch)
                medges += len(batch)
                batch = []
        if batch:
            dst.executemany("insert into morph_edge values (?,?,?,?)", batch)
            medges += len(batch)
    dst.execute("create index me_child on morph_edge (type, child_id)")
    dst.execute("create index me_parent on morph_edge (type, parent_id)")

    dst.execute("create index il_target on item_link (target_id, field)")
    dst.execute("create index il_owner on item_link (owner_id)")
    # clientscript token postings: token to its files
    dst.execute("create table script_token (token text, file_id integer)")
    dst.execute("create table script_file (file_id integer primary key, name text)")
    ntok = 0
    scripts = os.path.join(ROOT, "rs3-cache-stage", "clientscript")
    if os.path.isdir(scripts):
        TOK = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
        texts = script_texts()
        names = sorted(texts)
        dst.executemany("insert into script_file values (?,?)", list(enumerate(names)))
        batch = []
        for i, n in enumerate(names):
            raw = texts[n]
            if raw is None:
                continue
            for t in set(x.lower() for x in TOK.findall(raw)):
                batch.append((t.decode("utf-8", "replace"), i))
            if len(batch) >= 50000:
                dst.executemany("insert into script_token values (?,?)", batch)
                ntok += len(batch)
                batch = []
        if batch:
            dst.executemany("insert into script_token values (?,?)", batch)
            ntok += len(batch)
        dst.execute("create index st_token on script_token (token)")

    dst.execute("create table meta (k text primary key, v text)")
    dst.execute("insert into meta values ('built', ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    dst.execute("insert into meta values ('cache_mtime', ?)",
                (str(int(os.path.getmtime(CACHE))),))
    dst.execute("insert into meta values ('item_link_rows', ?)", (str(rows),))
    dst.execute("insert into meta values ('morph_edge_rows', ?)", (str(medges),))
    dst.execute("insert into meta values ('script_token_rows', ?)", (str(ntok),))
    # the references go in before the swap
    build_script_refs(dst)
    dst.commit()
    dst.close()
    swapped = _swap_in(staging)
    return rows, time.time() - t0, swapped




def _swap_in(staging, tries=5):
    """Move the freshly built index over the live one.

    Windows will not replace a file anything holds open, and a running server holds the index all
    session. Then the pages are copied into the open file instead, which a reader picks up on its
    next query. Only if that fails too does the new one stay beside the old.
    """
    import time as _t
    for n in range(tries):
        try:
            os.replace(staging, INDEX)
            return True
        except OSError:
            _t.sleep(0.3 * (n + 1))
    try:
        a, b = sqlite3.connect(staging), sqlite3.connect(INDEX, timeout=60)
        try:
            a.backup(b)
        finally:
            a.close()
            b.close()
    except sqlite3.Error:
        return False
    os.remove(staging)
    return True


# --------------------------------------------------------------------------- script references
#
# the decompiler types its constants, so extract exactly
CLIENTSCRIPT = os.path.join(ROOT, "rs3-cache-stage", "clientscript")

REF_VAR = re.compile(r"\bvar(bitplayer|player|client)_(\d+)\b")
REF_TYPED = re.compile(r"\b(\d+)\s+as\s+([a-z_]+)\b")
REF_COMP = re.compile(r"\bcomp\(\s*(\d+)\s*,\s*(\d+)\s*\)")
REF_CALL = re.compile(r"\bscript(\d+)\s*\(")

VAR_KIND = {"bitplayer": "varbit", "player": "varp", "client": "varclient"}
# the typed names worth indexing, not the counts
TYPED_KEEP = {"obj", "npc", "loc", "seq", "struct", "inv", "quest", "model", "dbrow",
              "cursor", "achievement", "graphic", "stat", "cs"}


_TEXTS = {}


def script_texts():
    """Every decompiled script, {file name: bytes}, read once for both passes.

    In parallel: the first open of a file just written waits on the virus scanner, about 5ms a
    file one at a time. The rebuild writes all 21,100 right before indexing, and read in order
    that was two minutes; 32 threads make it seconds."""
    if not _TEXTS:
        from concurrent.futures import ThreadPoolExecutor
        names = [n for n in os.listdir(CLIENTSCRIPT) if n.endswith(".ts")] if os.path.isdir(CLIENTSCRIPT) else []

        def read(n):
            try:
                with io.open(os.path.join(CLIENTSCRIPT, n), "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        with ThreadPoolExecutor(32) as pool:
            _TEXTS.update(zip(names, pool.map(read, names)))
    return _TEXTS


def build_script_refs(dst):
    """Every typed reference in every decompiled clientscript."""
    dst.execute("drop table if exists script_ref")
    dst.execute("create table script_ref (kind text, id integer, file text)")
    rows = []
    n = 0
    texts = script_texts()
    for name in sorted(texts):
        if texts[name] is None:
            continue
        text = texts[name].decode("utf-8", "replace")
        n += 1
        stem = name[:-3]
        seen = set()
        for m in REF_VAR.finditer(text):
            seen.add((VAR_KIND[m.group(1)], int(m.group(2))))
        for m in REF_TYPED.finditer(text):
            kind = m.group(2)
            if kind in TYPED_KEEP:
                seen.add(("script" if kind == "cs" else kind, int(m.group(1))))
        for m in REF_COMP.finditer(text):
            seen.add(("iface", int(m.group(1))))
        for m in REF_CALL.finditer(text):
            seen.add(("script", int(m.group(1))))
        rows += [(k, i, stem) for k, i in seen]
        if len(rows) > 200000:
            dst.executemany("insert into script_ref values (?,?,?)", rows)
            rows = []
    dst.executemany("insert into script_ref values (?,?,?)", rows)
    dst.execute("create index ix_script_ref on script_ref (kind, id)")
    dst.commit()
    total = dst.execute("select count(*) from script_ref").fetchone()[0]
    print("script_ref: %d references across %d scripts" % (total, n))
    return total


if __name__ == "__main__":
    n, secs, swapped = build()
    print("item_link: %d rows in %.1fs -> %s" % (n, secs, INDEX))
    if not swapped:
        print("  NOT installed: something still holds %s open, so the previous index is still in "
              "place and the new one is beside it as %s.new. Close the session using it and move "
              "it over by hand, or run this again." % (INDEX, INDEX))
    import sqlite3 as _s
    c = _s.connect("file:" + INDEX.replace("\\", "/") + "?mode=ro", uri=True)
    for k, v in c.execute("select k, v from meta where k like '%rows'"):
        print("  %-18s %s" % (k, v))
    c.close()
    print("size %.1f MB" % (os.path.getsize(INDEX) / 1e6))
