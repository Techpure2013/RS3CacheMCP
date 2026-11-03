"""Which groups moved since the database was built.

    python -m rs3mcp.changed          the groups that moved, per archive
    python -m rs3mcp.changed mark [_failures.json]
                                      record the cache as it is now as what was built, with the
                                      decode failures of the extract it was built from

A rebuild writes the crc of every group it was built from to data/rs3-cache-built.json. The next
one compares the cache against it and decodes only the records in the groups that moved, so an
update that touched a few hundred groups does not decode the other half a million again.
"""

import io
import json
import os
import sys
import time

from . import cachefile, common

MANIFEST = os.path.join(common.ROOT, "rs3-cache-built.json")
# besides the spec types: models, clientscripts, sprites
EXTRA = (47, 12, 8)


def majors():
    """Every archive the database and its assets are built from."""
    from . import extract, ops
    return sorted({ops.load(n)["index"] for n in extract.types()} | set(EXTRA))


def crcs(root=None, archives=None):
    """{archive: {group: crc}} as the cache reads now, the overlay included."""
    out = {}
    for major in archives or majors():
        desc = cachefile.descriptor(major, root or cachefile.GAME)
        if desc:
            out[major] = {g: v["crc"] for g, v in desc.items()}
    return out


def names_stamp():
    """A fingerprint of the name lists a build reads. Names come from them, not the cache, so a
    list that changed is a rebuild even when no group moved."""
    import hashlib
    folder = common.name_lists()
    h = hashlib.sha1()
    if folder:
        for f in sorted(os.listdir(folder)):
            if f.startswith("Vars - ") and f.endswith(".csv"):
                st = os.stat(os.path.join(folder, f))
                h.update(("%s|%d|%d;" % (f, st.st_size, int(st.st_mtime))).encode("utf-8"))
    return h.hexdigest()


def load(path=None):
    """The manifest of the last build, or None before there is one."""
    try:
        got = json.load(io.open(path or MANIFEST, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    got["archives"] = {int(a): {int(g): c for g, c in groups.items()}
                       for a, groups in got.get("archives", {}).items()}
    got["failures"] = {t: set(ids) for t, ids in got.get("failures", {}).items()}
    return got


def save(archives, failures, chunks, path=None):
    """archives as crcs() gives them, failures per type, and the chunk size each chunked type's
    ids were built with."""
    path = path or MANIFEST
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"built": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "archives": {str(a): {str(g): c for g, c in sorted(groups.items())}
                                for a, groups in sorted(archives.items())},
                   "failures": {t: sorted(ids) for t, ids in sorted(failures.items())},
                   "chunks": chunks, "names": names_stamp()}, fh)
    os.replace(tmp, path)


def moved(old, new):
    """{archive: groups whose crc differs, or that one side lacks}. An archive missing from old
    counts as every group moved."""
    out = {}
    for major, groups in new.items():
        was = old.get(major)
        if was is None:
            out[major] = set(groups)
            continue
        got = {g for g, c in groups.items() if was.get(g) != c} | (set(was) - set(groups))
        if got:
            out[major] = got
    return out


def main(argv):
    now = crcs()
    if argv[:1] == ["mark"]:
        from . import extract
        before = load()
        failures = json.load(io.open(argv[1], encoding="utf-8")) if len(argv) > 1 else \
            before["failures"] if before else {}
        save(now, failures, extract.chunks())
        print("recorded %d groups across %d archives as built" % (sum(map(len, now.values())), len(now)))
        return 0
    before = load()
    if before is None:
        print("no manifest yet: the next rebuild decodes everything, then writes one")
        return 1
    got = moved(before["archives"], now)
    for major, groups in sorted(got.items()):
        print("archive %-3d %6d group(s) moved, e.g. %s" % (major, len(groups), sorted(groups)[:8]))
    print("%d group(s) moved since %s" % (sum(map(len, got.values())), before.get("built")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
