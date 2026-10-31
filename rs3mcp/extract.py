"""Every config type out of the game cache through our specs, and a summary of every model, into a
stage build_db reads.

    python -m rs3mcp.extract <stage> [type ...]

One `<type>/<type>.batch.json` per type, each record carrying `$fileid`. A record that does not
decode to its end is left out and named in `_failures.json`, so a rebuild can tell a lost row from
removed content.
"""

import io
import json
import os
import sys
import time

from . import cachefile, models, ops


def types():
    return sorted(f[:-4] for f in os.listdir(ops.SPECS) if f.endswith(".ops"))


def _plain(v):
    # params keep int keys, json wants strings
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def extract(stage, names=None, root=None, say=print):
    """{type: [ids that did not decode]} after writing the stage.

    Every type, and the groups of the big ones, decode at once in separate processes. Each part
    comes back as json text, which is cheap to hand over, and is written in group order."""
    import multiprocessing
    root = root or cachefile.GAME
    names = names or types() + sorted(CUSTOM)
    jobs = []
    for name in names:
        if name in CUSTOM:
            desc = cachefile.descriptor(CUSTOM[name].MAJOR, root) or {}
            minors = sorted(desc)
        else:
            spec = ops.load(name)
            desc = cachefile.descriptor(spec["index"], root) or {}
            minors = sorted(desc) if spec["group"] is None else [None]
        files = sum(len(desc[m]["files"]) for m in minors if m is not None) or 1
        # about 4,000 records a part, spread over cores
        n = max(1, min(WORKERS, files // 4000, len(minors)))
        step = -(-len(minors) // n)
        for k in range(0, len(minors), step):
            jobs.append((name, k, None if minors[k] is None else minors[k:k + step], root))
    # the slowest first, before the cores fill
    jobs.sort(key=lambda j: -len(j[2] or ()))
    parts = {}
    with multiprocessing.get_context("spawn").Pool(WORKERS) as pool:
        for name, k, text, count, failed, secs in pool.imap_unordered(_part, jobs):
            parts.setdefault(name, {})[k] = (text, count, failed, secs)
    failures = {}
    for name in names:
        got = [parts[name][k] for k in sorted(parts[name])]
        out = os.path.join(stage, name)
        os.makedirs(out, exist_ok=True)
        with io.open(os.path.join(out, name + ".batch.json"), "w", encoding="utf-8") as fh:
            fh.write('{"$schema":"ops","files":[')
            fh.write(",".join(text for text, _, _, _ in got if text))
            fh.write("]}")
        failures[name] = [cid for _, _, failed, _ in got for cid in failed]
        say("%-18s %7d records%s  %.1fs" % (name, sum(c for _, c, _, _ in got),
            ", %d did not decode" % len(failures[name]) if failures[name] else "",
            sum(s for _, _, _, s in got)))
    with io.open(os.path.join(stage, "_failures.json"), "w", encoding="utf-8") as fh:
        json.dump(failures, fh)
    return failures


WORKERS = min(32, os.cpu_count() or 1)
# types a spec cannot describe, one module each
CUSTOM = {"models": models}


def where(name, root=None):
    """(archive, group or None, layout, chunk size or None) a type is read from."""
    root = root or cachefile.GAME
    if name in CUSTOM:
        return CUSTOM[name].MAJOR, None, "groups", None
    spec = ops.load(name)
    layout = spec.get("layout", "chunked")
    desc = cachefile.descriptor(spec["index"], root) or {}
    size = cachefile.chunk(desc) if layout == "chunked" and spec["group"] is None and desc else None
    return spec["index"], spec["group"], layout, size


def group_of(cid, layout, size):
    """The group a record id came from."""
    if layout == "files":
        return cid >> 16
    if layout == "groups" or size is None:
        return cid
    return cid // size


def groups_of(name, ids, root=None):
    """(archive, the groups these records of a type are in)."""
    major, group, layout, size = where(name, root)
    if group is not None:
        return major, {group}
    return major, {group_of(cid, layout, size) for cid in ids}


def keep_previous(stage, db, failures, say=print):
    """Every record that did not decode but db still holds, staged as it was. {type: ids kept}.

    An update that breaks a decode then never costs a row the database could read yesterday: it
    keeps that row, and is named so somebody looks. A record new in this update has no row to
    keep, so it stays out, named in the failures."""
    import re
    import sqlite3
    if not os.path.exists(db):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    kept = {}
    for name, ids in sorted(failures.items()):
        path = os.path.join(stage, name, name + ".batch.json")
        if not ids or not os.path.exists(path):
            continue
        want = set(ids)
        held = {cid: data for cid, data in con.execute("select id, data from config where type = ?", (name,))
                if cid in want and data is not None}
        if not held:
            continue
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        # rows already staged
        staged = {int(c) for c in re.findall(r'"\$fileid":(\d+)', text)}
        add = sorted(c for c in held if c not in staged)
        if add:
            rows = ",".join('{"$fileid":%d}' % c if held[c] == "{}" else '{"$fileid":%d,%s' % (c, held[c][1:])
                            for c in add)
            body = text[:-2]
            text = body + ("," if not body.endswith("[") else "") + rows + "]}"
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        kept[name] = sorted(held)
        say("%-18s %d record(s) did not decode and keep the row they had" % (name, len(held)))
    con.close()
    return kept


def chunks(root=None):
    """{type: chunk size} for the chunked types, which a record id is built from."""
    return {n: where(n, root)[3] for n in types() if where(n, root)[3]}


def refresh(stage, db, moved, before, root=None, say=print, names=None):
    """extract, decoding only the records in moved groups. {type: ids that did not decode}.

    The rest come from db, which holds every type's records exactly as the last extract staged
    them, with their failures from before (the manifest rs3mcp/changed.py keeps). invs is not held
    there as records, so it is always decoded; it is one group. A type whose chunk size moved has
    every id moved with it, so it is decoded whole."""
    import multiprocessing
    import sqlite3
    root = root or cachefile.GAME
    jobs, plan = [], {}
    for name in names or types() + sorted(CUSTOM):
        major, group, layout, size = where(name, root)
        hit = set(moved.get(major, ()))
        desc = cachefile.descriptor(major, root) or {}
        if group is not None:
            # one group: decoded whole or kept whole
            if group in hit or name == "invs":
                plan[name] = (None, set(), layout, size)
                jobs.append((name, 0, None, root))
            else:
                plan[name] = (set(), set(), layout, size)
            continue
        if size is not None and before.get("chunks", {}).get(name) not in (None, size):
            hit = set(desc) | set(hit)
        redo = sorted(hit & set(desc))
        plan[name] = (redo, hit, layout, size)
        step = max(1, -(-len(redo) // WORKERS))
        for k in range(0, len(redo), step):
            jobs.append((name, k, redo[k:k + step], root))
    parts = {}
    if len(jobs) > 1:
        with multiprocessing.get_context("spawn").Pool(min(WORKERS, len(jobs))) as pool:
            for name, k, text, count, failed, secs in pool.imap_unordered(_part, jobs):
                parts.setdefault(name, {})[k] = (text, count, failed)
    else:
        for job in jobs:
            name, k, text, count, failed, secs = _part(job)
            parts.setdefault(name, {})[k] = (text, count, failed)

    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    failures = {}
    for name, (redo, hit, layout, size) in sorted(plan.items()):
        got = [parts.get(name, {})[k] for k in sorted(parts.get(name, {}))]
        texts = [text for text, _, _ in got if text]
        decoded = sum(c for _, c, _ in got)
        failed = [cid for _, _, ids in got for cid in ids]
        kept = 0
        if redo is not None:
            for cid, data in con.execute("select id, data from config where type = ?", (name,)):
                if data is None or group_of(cid, layout, size) in hit:
                    continue
                texts.append('{"$fileid":%d}' % cid if data == "{}" else '{"$fileid":%d,%s' % (cid, data[1:]))
                kept += 1
            failed += [c for c in before.get("failures", {}).get(name, ())
                       if group_of(c, layout, size) not in hit]
        out = os.path.join(stage, name)
        os.makedirs(out, exist_ok=True)
        with io.open(os.path.join(out, name + ".batch.json"), "w", encoding="utf-8") as fh:
            fh.write('{"$schema":"ops","files":[')
            fh.write(",".join(texts))
            fh.write("]}")
        failures[name] = sorted(failed)
        if decoded or (redo is None):
            say("%-18s %7d decoded, %d kept%s" % (name, decoded, kept,
                ", %d did not decode" % len(failed) if failed else ""))
    con.close()
    with io.open(os.path.join(stage, "_failures.json"), "w", encoding="utf-8") as fh:
        json.dump(failures, fh)
    return failures


def _part(job):
    """One type, or some of its groups: (type, part, json rows joined by commas, count, failed ids,
    seconds)."""
    name, k, minors, root = job
    started = time.time()
    if name in CUSTOM:
        found, decode = CUSTOM[name].records(root, minors), CUSTOM[name].decode
    else:
        spec = ops.load(name)
        found, decode = ops.records(spec, root, minors), lambda data: ops.decode(spec, data)
    rows, failed = [], []
    for cid, data in found:
        fields, problem = decode(data)
        if problem is not None:
            failed.append(cid)
            continue
        fields = _plain(fields)
        fields["$fileid"] = cid
        rows.append(json.dumps(fields, separators=(",", ":")))
    return name, k, ",".join(rows), len(rows), failed, time.time() - started


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    extract(sys.argv[1], sys.argv[2:] or None)
