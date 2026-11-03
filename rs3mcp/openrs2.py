"""The groups the game client's cache is behind on, from the OpenRS2 archive.

    python -m rs3mcp.openrs2            fetch what changed since the client's cache
    python -m rs3mcp.openrs2 --check    say what would be fetched, fetch nothing

archive.openrs2.org keeps every RS3 cache Jagex serves, each pulled straight from Jagex. The
client only downloads a group when it needs it, so after an update its cache lags. This reads
OpenRS2's index for each archive the database is built from, and fetches only the groups whose crc
differs from the client's: one update is a few thousand small requests, not the 23.6 GiB cache.

Fetched groups go into data/openrs2/, laid out like the client's js5-N.jcache files, and
cachefile reads them over the client's. The client's own files are never written.

Politeness: an identifying User-Agent, four requests at a time, the cache list only re-read when
it changed (If-None-Match), and nothing fetched twice. Once a cache has been synced whole, the next
run stops at the cache list until OpenRS2 has a newer one. A group is only kept if its bytes hash
to the crc OpenRS2's index gives it.
"""

import concurrent.futures
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zlib

from . import cachefile

BASE = "https://archive.openrs2.org"
AGENT = "rs3cache-mcp (+https://github.com/Techpure2013/RS3CacheMCP)"
WORKERS = 4
# the language the client is set to
LANGUAGE = "en"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (KEY INTEGER PRIMARY KEY, DATA BLOB, VERSION INTEGER, CRC INTEGER);
CREATE TABLE IF NOT EXISTS cache_index (KEY INTEGER PRIMARY KEY, DATA BLOB, VERSION INTEGER, CRC INTEGER);
"""


def _get(url, etag=None):
    """(status, bytes, etag). 304 comes back with no bytes."""
    headers = {"User-Agent": AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, b"", etag
            if e.code == 429 or e.code >= 500:
                time.sleep(float(e.headers.get("Retry-After") or 5 * (attempt + 1)))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    raise OSError("no answer from %s" % url)


def latest(folder=None):
    """The newest live RS3 cache OpenRS2 holds in full: {id, timestamp, builds}."""
    folder = folder or cachefile.OVERLAY
    os.makedirs(folder, exist_ok=True)
    path, tag = os.path.join(folder, "caches.json"), os.path.join(folder, "caches.etag")
    etag = io.open(tag, encoding="utf-8").read().strip() if os.path.exists(tag) and os.path.exists(path) else None
    status, body, etag = _get(BASE + "/caches.json", etag)
    if status != 304:
        io.open(path, "wb").write(body)
        if etag:
            io.open(tag, "w", encoding="utf-8").write(etag)
    caches = json.load(io.open(path, encoding="utf-8"))
    whole = [c for c in caches if c.get("game") == "runescape" and c.get("environment") == "live"
             and c.get("language") == LANGUAGE and c.get("timestamp")
             and c.get("groups") and c.get("valid_groups") == c.get("groups")]
    if not whole:
        raise OSError("OpenRS2 lists no whole live RS3 cache")
    return max(whole, key=lambda c: c["timestamp"])


def _stamp(iso):
    """Seconds since the epoch, from OpenRS2's ISO 8601 timestamp."""
    import datetime
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _index(cache_id, major):
    """(bytes, descriptor) of OpenRS2's index for one archive."""
    _status, blob, _ = _get("%s/caches/runescape/%d/archives/255/groups/%d.dat" % (BASE, cache_id, major))
    return blob, cachefile._descriptor(cachefile.unwrap(blob))


def _crc(blob):
    return zlib.crc32(blob) & 0xFFFFFFFF


def behind(cache_id, major, root=None):
    """(OpenRS2's index bytes, its descriptor, [groups to fetch]) for one archive.

    A group is fetched when OpenRS2's index gives it a crc that neither the client's copy nor the
    overlay's has: changed or new. One the client never downloaded and nothing changed stays
    absent, as it always has."""
    root = root or cachefile.GAME
    blob, theirs = _index(cache_id, major)
    if theirs is None:
        return blob, None, []
    ours = cachefile._descriptor(cachefile.unwrap(_client_index(root, major))) or {}
    have = {}
    for con in (cachefile._open(root, major), cachefile._open(cachefile.OVERLAY, major)):
        if con is not None:
            for key, crc in con.execute("select KEY, CRC from cache"):
                have.setdefault(key, set()).add(crc)
    want = [g for g, v in sorted(theirs.items())
            if (g not in ours or ours[g]["crc"] != v["crc"])
            and cachefile.stored_crc(v["crc"]) not in have.get(g, ())]
    return blob, theirs, want


def _client_index(root, major):
    con = cachefile._open(root, major)
    if con is None:
        return None
    row = con.execute("select DATA from cache_index where KEY = 1").fetchone()
    return row[0] if row else None


def _fetch(cache_id, major, minor, crc):
    _status, blob, _ = _get("%s/caches/runescape/%d/archives/%d/groups/%d.dat" % (BASE, cache_id, major, minor))
    return minor, blob, _crc(blob) == crc


def sync(majors, check=False, say=print, root=None):
    """Bring the overlay up to OpenRS2's newest cache for these archives.

    {archive: groups fetched}, or with check, the groups that would be. An archive the client
    wrote after that cache was made is left to the client."""
    root = root or cachefile.GAME
    if cachefile.BETA and root == cachefile.GAME:
        say("openrs2: the cache is the beta's, and OpenRS2 keeps only live ones, so nothing is fetched")
        return {}
    cache = latest()
    done =os.path.join(cachefile.OVERLAY, "synced.json")
    try:
        last = json.load(io.open(done, encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    if last.get("cache") == cache["id"] and sorted(last.get("archives", ())) == sorted(set(majors)):
        say("openrs2: already up to cache %d, nothing to fetch" % cache["id"])
        return {}
    made = _stamp(cache["timestamp"])
    say("openrs2: cache %d, build %s, %s" % (cache["id"], ".".join(
        str(b.get("major")) + ("." + str(b["minor"]) if b.get("minor") is not None else "")
        for b in cache.get("builds") or []) or "?", cache["timestamp"][:19]))
    got, whole = {}, True
    for major in sorted(set(majors)):
        if cachefile._mtime(root, major) > made:
            continue
        blob, theirs, want = behind(cache["id"], major, root)
        if theirs is None:
            say("openrs2: archive %d has no index there, skipped" % major)
            continue
        if not want and _client_index(root, major) is not None:
            continue
        got[major] = want
        if want:
            say("openrs2: archive %-3d %5d group(s) behind, e.g. %s" % (major, len(want), want[:6]))
        if check or not want:
            continue
        if not _store(cache, major, blob, theirs, want, say):
            whole = False
    if not check and whole:
        with io.open(done, "w", encoding="utf-8") as fh:
            json.dump({"cache": cache["id"], "archives": sorted(set(majors))}, fh)
    total = sum(len(v) for v in got.values())
    say("openrs2: %d group(s) %s across %d archive(s)" % (total, "behind" if check else "fetched", len(got)))
    return got


def _store(cache, major, blob, theirs, want, say):
    os.makedirs(cachefile.OVERLAY, exist_ok=True)
    path = os.path.join(cachefile.OVERLAY, "js5-%d.jcache" % major)
    # drop the read only handle
    cachefile._OPEN.pop((cachefile.OVERLAY, major), None)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    started, bad = time.time(), []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        jobs = [pool.submit(_fetch, cache["id"], major, g, theirs[g]["crc"]) for g in want]
        for job in concurrent.futures.as_completed(jobs):
            minor, data, ok = job.result()
            if not ok:
                bad.append(minor)
                continue
            v = theirs[minor]
            con.execute("insert or replace into cache values (?,?,?,?)",
                        (minor, data, v["version"], cachefile.stored_crc(v["crc"])))
    raw = cachefile.unwrap(blob)
    revision = int.from_bytes(raw[1:5], "big") if raw and raw[0] >= 6 else 0
    if not bad:
        con.execute("insert or replace into cache_index values (1,?,?,?)",
                    (blob, revision, cachefile.stored_crc(_crc(blob))))
    con.commit()
    con.close()
    cachefile._OPEN.pop((cachefile.OVERLAY, major), None)
    cachefile._STORED.clear()
    say("openrs2: archive %-3d %5d group(s) in %.0fs%s" % (
        major, len(want) - len(bad), time.time() - started,
        ", %d failed their crc, so its index was not taken" % len(bad) if bad else ""))
    return not bad


if __name__ == "__main__":
    from rs3mcp import changed
    sys.exit(0 if sync(changed.majors(), check="--check" in sys.argv) is not None else 1)
