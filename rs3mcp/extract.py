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
