"""Whether GitHub has moved on from this copy. One request, one reply, twice an hour.

The reply is the branch's commit sha and nothing else, 40 bytes, and a second look costs a 304
with no body at all. Against an hourly allowance of 60 that is two.

It compares shas, so it says the two differ rather than which way round: a copy with its own
commits on top reads the same as one that is behind. `git pull` is the answer either way.

RS3_UPDATES=off turns it off and nothing is ever requested.
"""

import io
import json
import os
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

from rs3mcp import common

REPO = "Techpure2013/RS3CacheMCP"
API = "https://api.github.com/repos/%s/commits/main" % REPO
UA = "rs3cache update check (+https://github.com/%s)" % REPO
EVERY = 30 * 60
STORE = os.path.join(common.ROOT, "update.json")

_ROOT_FETCHED = [False]


def off():
    return (os.environ.get("RS3_UPDATES") or "").strip().lower() in ("off", "0", "no", "false")


def local_head():
    """This copy's commit, read out of .git without needing git. None if it is not a clone."""
    git = os.path.join(common.BASE, ".git")
    if os.path.isfile(git):
        try:
            git = os.path.join(common.BASE, io.open(git, encoding="utf-8").read().split(":", 1)[1].strip())
        except (OSError, IndexError):
            return None
    if not os.path.isdir(git):
        return None
    try:
        head = io.open(os.path.join(git, "HEAD"), encoding="utf-8").read().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(" ", 1)[1].strip()
    try:
        return io.open(os.path.join(git, ref), encoding="utf-8").read().strip()
    except OSError:
        pass
    try:
        for line in io.open(os.path.join(git, "packed-refs"), encoding="utf-8"):
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip()
    except OSError:
        pass
    return None


def _windows_fetch_root():
    """Have Windows fetch github once so it downloads the root certificate Python is missing.

    Same trap rs3_wiki hit on a fresh machine: Python reads only the roots already on disk.
    """
    if os.name != "nt" or _ROOT_FETCHED[0]:
        return False
    _ROOT_FETCHED[0] = True
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Invoke-WebRequest -UseBasicParsing -Method Head https://api.github.com/ | Out-Null"],
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _read():
    try:
        return json.load(io.open(STORE, encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _write(state):
    try:
        os.makedirs(common.ROOT, exist_ok=True)
        with io.open(STORE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def _ask(etag):
    """(sha, etag, error). sha is None when nothing changed or the look failed."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github.sha",
                                               "User-Agent": UA})
    if etag:
        req.add_header("If-None-Match", etag)
    for attempt in (0, 1):
        ctx = ssl.create_default_context() if _ROOT_FETCHED[0] else None
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                return r.read(64).decode("ascii", "replace").strip(), r.headers.get("ETag"), None
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag, None
            return None, etag, "github said %d" % e.code
        except Exception as e:
            why = str(getattr(e, "reason", None) or e)[:120]
            if attempt == 0 and "CERTIFICATE_VERIFY_FAILED" in why and _windows_fetch_root():
                continue
            return None, etag, why
    return None, etag, "unreachable"


def check(force=False):
    """Look if it is time to, and return what we know. Never raises."""
    state = _read()
    if off():
        return dict(state, checked=state.get("checked"), skipped="RS3_UPDATES=off")
    if not force and time.time() - (state.get("checked") or 0) < EVERY:
        return state

    sha, etag, error = _ask(state.get("etag"))
    state["checked"] = time.time()
    state["etag"] = etag
    state["error"] = error
    if sha:
        state["remote"] = sha
    state["local"] = local_head()
    _write(state)
    return state


def status():
    """A line for a person, or None when there is nothing to say."""
    if off():
        return "not looking, RS3_UPDATES=off"
    state = check()
    local, remote = state.get("local"), state.get("remote")
    if state.get("error"):
        return "could not reach github (%s)" % state["error"]
    if not local:
        return "this copy is not a git clone, so there is nothing to compare"
    if not remote:
        return None
    if local == remote:
        return "up to date with github (%s)" % remote[:7]
    return "github is at %s, this copy is at %s -- run: git pull" % (remote[:7], local[:7])


# moved group count and its time
_GAME = {"at": 0, "moved": None}
_SHOWN = [None]


def moved_since_build():
    """Groups a game update moved since the database was built. None before the first build."""
    from rs3mcp import changed
    before = changed.load()
    if before is None or not os.path.exists(common.DB):
        return None
    got = changed.moved(before["archives"], changed.crcs(archives=sorted(before["archives"])))
    return sum(map(len, got.values()))


def _count_moved():
    _GAME["moved"] = moved_since_build()


def notice():
    """A line for the AI to pass on, once each time it changes. None when there is nothing new.

    Counting the moved groups reads every archive's index, about a second, so it runs beside the
    call and the next one says what it found. The client downloading groups as you play does not
    move them, only a game update does."""
    if time.time() - _GAME["at"] > EVERY:
        _GAME["at"] = time.time()
        threading.Thread(target=_count_moved, name="moved-groups", daemon=True).start()
    said = []
    remote, local = _read().get("remote"), local_head()
    if not off() and remote and local and remote != local:
        said.append("a newer rs3cache is on GitHub, run git pull in %s" % common.BASE)
    if _GAME["moved"]:
        said.append("the game updated since the database was built (%d groups moved), run rs3 rebuild, "
                    "which redoes only what moved" % _GAME["moved"])
    text = "; ".join(said) or None
    if text == _SHOWN[0]:
        return None
    _SHOWN[0] = text
    return text


def start():
    """Look every half hour in the background, for as long as the server runs."""
    if off():
        return None

    def loop():
        while True:
            try:
                check()
            except Exception:
                pass
            time.sleep(EVERY)

    th = threading.Thread(target=loop, name="update-check", daemon=True)
    th.start()
    return th
