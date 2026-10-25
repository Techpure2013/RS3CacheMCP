"""Is everything in place? Each check says what it found and, when it is not fine, what to do.

    rs3 doctor

Checks come back as (state, what, found, fix). ok means nothing to do. warn means something works
less well or not at all, but the rest does not care. fail means the tools cannot work until it is
fixed.
"""

import os
import shutil
import socket
import sqlite3
import sys
import time

from . import addons, cachefile, clients, common


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def checks():
    out = []

    def add(state, what, found, fix=None):
        out.append((state, what, found, fix))

    v = sys.version_info
    add("ok" if v >= (3, 10) else "fail", "Python", "%d.%d.%d at %s" % (v[0], v[1], v[2], sys.executable),
        None if v >= (3, 10) else "install Python 3.12 from python.org, then run this again with it")

    game = cachefile.GAME
    caches = [f for f in os.listdir(game) if f.startswith("js5-")] if os.path.isdir(game) else []
    newest = max((os.path.getmtime(os.path.join(game, f)) for f in caches), default=0)
    if caches:
        add("ok", "RuneScape game cache", "%d files in %s, last updated %s" % (len(caches), game, _when(newest)))
    else:
        add("fail", "RuneScape game cache", "nothing at %s" % game,
            "install RuneScape and log in once so it downloads the cache. If it lives somewhere else, "
            "set RS3MCP_GAME_CACHE to that folder")

    if caches:
        try:
            from . import clientscript, cscalibrate
            fit = clientscript.verify()
            if cscalibrate.stale_reference():
                add("warn", "Clientscript opcode map", "made for another game version (reads %d of %d "
                    "scripts)" % (fit["read"], fit["scripts"]),
                    "run: rs3 rebuild, which works out this version's opcodes before decompiling")
            elif fit["read"] == fit["scripts"]:
                add("ok", "Clientscript opcode map", "reads all %d scripts in this game version" % fit["scripts"])
            else:
                add("warn", "Clientscript opcode map", "reads %d of %d scripts of the version it was made for"
                    % (fit["read"], fit["scripts"]),
                    "the map was edited by hand; data/clientscript-opmap.previous.json is the one before")
        except Exception as e:
            add("warn", "Clientscript opcode map", "could not check: %s" % e)

    db = common.DB
    if not os.path.exists(db):
        add("fail", "Database", "not built yet (%s)" % db, "run: rs3 rebuild (a few minutes)")
    else:
        built = os.path.getmtime(db)
        try:
            rows = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True) \
                .execute("select count(*) from config").fetchone()[0]
            found = "%d configs, built %s" % (rows, _when(built))
        except sqlite3.Error as e:
            found = "unreadable: %s" % e
        stale = newest and newest > built
        add("warn" if stale else "ok", "Database", found,
            "the game has updated since, so run: rs3 rebuild" if stale else None)

    index = common.sidecar("item_link")
    add("ok" if index is not None else "warn", "Search index",
        "in step with the database" if index is not None else "missing or older than the database",
        None if index is not None else "run: rs3 rebuild (or python rs3mcp/buildindex.py on its own)")

    scripts = common.CLIENTSCRIPT
    n = len([f for f in os.listdir(scripts) if f.startswith("clientscript-")]) if os.path.isdir(scripts) else 0
    add("ok" if n else "warn", "Decompiled clientscripts", "%d files" % n if n else "none yet",
        None if n else "run: rs3 rebuild")

    csvs = common.name_lists()
    has = bool(csvs) and any(f.startswith("Vars - ") for f in os.listdir(csvs))
    shipped = csvs and os.path.normcase(os.path.abspath(csvs)) == os.path.normcase(common.NAMES)
    add("ok" if has else "warn", "Config names",
        (csvs + ("  (the shipped set)" if shipped else "")) if has else "no name lists",
        None if has else "without them interfaces, vars and quests build with no names at all. "
        "The set that ships is rs3mcp/names; RS3MCP_NAMES points at a newer one")

    from rs3mcp import updates
    said = updates.status()
    if said:
        add("warn" if "git pull" in said else "ok", "Updates", said,
            "RS3_UPDATES=off stops it looking" if "git pull" in said else None)

    try:
        probe = os.path.join(common.ROOT, ".doctor-write-test")
        os.makedirs(common.ROOT, exist_ok=True)
        open(probe, "w").close()
        os.remove(probe)
        add("ok", "Data folder", common.ROOT)
    except OSError as e:
        add("fail", "Data folder", "cannot write to %s: %s" % (common.ROOT, e),
            "move rs3cache somewhere your account can write, or set RS3MCP_DATA to such a folder")

    # programs an add-on's tools run, named by it
    wanted = [pair for offered in addons.every("programs") for pair in offered]
    for tool, needed_by in wanted:
        where = shutil.which(tool)
        add("ok" if where else "warn", "Optional: %s" % tool, where or "not installed",
            None if where else "only needed for %s" % needed_by)
    for mod in ("PIL", "numpy"):
        try:
            __import__(mod)
            add("ok", "Optional: %s" % mod, "installed")
        except ImportError:
            add("warn", "Optional: %s" % mod, "not installed",
                "only for rendering icons and model pictures: pip install pillow numpy")
    try:
        __import__("bs4")
        add("ok", "Optional: beautifulsoup4", "installed")
    except ImportError:
        add("warn", "Optional: beautifulsoup4", "not installed",
            "only for fetching the client api docs and their Lua typings: pip install beautifulsoup4")

    port = int(os.environ.get("RS3_MONITOR_PORT") or 7331)
    with socket.socket() as s:
        s.settimeout(0.5)
        live = s.connect_ex(("127.0.0.1", port)) == 0
    add("ok", "Scrying pool", "running at http://127.0.0.1:%d" % port if live else
        "starts with the first AI app that uses rs3cache, or run: rs3 pool")

    for key, label, *_ in clients.APPS:
        state = clients.status(key)
        if state == "set":
            add("ok", "App: %s" % label, "rs3cache is set up")
        elif state == "here":
            add("warn", "App: %s" % label, "installed, rs3cache not added", "run: rs3 setup %s" % key)
    return out
