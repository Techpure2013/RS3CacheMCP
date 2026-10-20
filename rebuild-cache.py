"""
Rebuild rs3-cache.db from the game cache, but only install it if it is actually better.

    python rebuild-cache.py            rebuild if the game cache has moved ahead, else do nothing
    python rebuild-cache.py --check    say whether a rebuild is due and stop
    python rebuild-cache.py --force    rebuild even if the game cache looks unchanged
    python rebuild-cache.py --no-swap  build and verify, leave the current database in place

The game cache is only newer when the client has actually updated, so the usual answer is that
there is nothing to do and this costs a stat of forty files.

Installing is the part worth being careful about. A rebuild can go wrong in a way that still
produces a plausible database -- an opcode the update changed stops decoding and a few thousand
rows quietly vanish -- so nothing is swapped until it passes:

  every mode extracted, and the ones that fail to decode are counted per mode
  no row the current database holds is lost to a decode failure
  no config type shrinks by more than a fiftieth, which content removal does not do
  the tables carried forward arrive intact

Content the update genuinely removed is expected and does not block anything: Jagex deleted 242
structs and 213 interfaces in the 2026-08-31 update. What blocks is losing a row we could read
yesterday and cannot read today.

The previous database is kept as rs3-cache.db.bak, and the sidecar index is rebuilt after a swap
because every reader falls back to a slow path without it.
"""

import json
import os
import shutil
import subprocess
import sqlite3
import sys
import time

from rs3mcp import cachefile, common  # noqa: E402

# the scripts sit here, and what they build sits in data/ unless RS3MCP_DATA says otherwise
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = common.ROOT
DB = os.path.join(DATA, "rs3-cache.db")
NEW = os.path.join(DATA, "rs3-cache-new.db")
BAK = os.path.join(DATA, "rs3-cache.db.bak")
STAGE = os.path.join(DATA, "rs3-cache-stage-new")
# "-" when there are no name lists, which build_db reads as none
CSVS = common.name_lists() or "-"
GAME = cachefile.GAME
INDEXER = os.path.join(ROOT, "rs3mcp", "buildindex.py")

# Every config type is decoded by rs3mcp/extract.py from rs3mcp/specs, the clientscripts are
# decompiled by rs3mcp/csdecomp.py and the sprites drawn by rs3mcp/sprites.py. rs3_script and
# rs3_sprite read those straight off disk, so they go stale the same way and are refreshed on the
# same trigger. The canonical location is rs3-cache-stage, where the readers look, so they are
# only moved into place once the database built beside them has been accepted.
ASSET_MODES = ["clientscript", "sprites"]
LIVE_STAGE = os.path.join(DATA, "rs3-cache-stage")

# Types with a standing decode gap: a type listed here may fail up to its number, a type not listed
# failing is what blocks an install. Every config, model, clientscript and sprite decodes today, so
# the list is empty and any failure is new. Add one only after looking at what fails and why.
KNOWN_UNDECODED = {}
# a build's own standing gaps, with a _why
_GAPS = os.path.join(DATA, "known-undecoded.json")
if os.path.exists(_GAPS):
    KNOWN_UNDECODED.update({k: v for k, v in json.load(open(_GAPS, encoding="utf-8")).items()
                            if not k.startswith("_")})
SHRINK_LIMIT = 0.02


def say(msg):
    print("  " + msg, flush=True)


def newest_game_file():
    try:
        return max(os.path.getmtime(os.path.join(GAME, f))
                   for f in os.listdir(GAME) if f.startswith("js5-"))
    except (OSError, ValueError):
        return None


def due():
    """(is a rebuild due, why). The game cache carries its own timestamps, so no state is kept."""
    game = newest_game_file()
    if game is None:
        return False, "the game cache is not readable at " + GAME
    if not os.path.exists(DB):
        return True, "there is no database yet"
    built = os.path.getmtime(DB)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(game))
    if game <= built:
        return False, "the game cache has not moved since the database was built (%s)" % when
    return True, "the game cache was updated %s, after the database was built" % when


def extract():
    """Every config type into a fresh stage, then the clientscripts. Returns failures per type."""
    from rs3mcp.extract import extract as extract_configs

    shutil.rmtree(STAGE, ignore_errors=True)
    os.makedirs(STAGE)
    failures = {t: set(ids) for t, ids in extract_configs(STAGE, say=say).items()}

    failures["clientscript"] = clientscripts(os.path.join(STAGE, "clientscript"))
    from rs3mcp import sprites
    failures["sprites"] = set(timed("sprites", sprites.write, os.path.join(STAGE, "sprites"), None, say))
    return failures


def clientscripts(folder):
    """The clientscripts, decompiled by rs3mcp/csdecomp.py. Returns the ids not decompiled.

    Our opcode map fits one update: Jagex renumbers every opcode each time. On an update the
    reference does not hold, the map is calibrated against the last one it read
    (rs3mcp/cscalibrate.py). If that cannot be trusted and the map no longer reads every script, the
    scripts are skipped and the last good ones stay."""
    from rs3mcp import clientscript, cscalibrate, csdecomp

    if clientscript.OPMAP != clientscript.SHIPPED_OPMAP and not os.path.exists(clientscript.OPMAP):
        # seed a new folder from the shipped map
        shipped = os.path.dirname(clientscript.SHIPPED_OPMAP)
        os.makedirs(DATA, exist_ok=True)
        shutil.copyfile(clientscript.SHIPPED_OPMAP, clientscript.OPMAP)
        shutil.copyfile(os.path.join(shipped, "clientscript-reference.bin"), cscalibrate.REFERENCE)
        say("clientscript: seeded this folder's map and reference from the shipped ones")
    fit = timed("clientscript opcode check", clientscript.verify)
    calibrated = None
    if fit["read"] != fit["scripts"]:
        say("clientscript: the opcode map reads %d of %d scripts, so the game has updated past it"
            % (fit["read"], fit["scripts"]))
    # Reading every script is not proof the map fits: nearly every op has a one byte immediate, so
    # a map for another update reads most small scripts regardless. An update the reference does
    # not hold is calibrated whatever the check says, and the same numbers come back if none moved.
    if fit["read"] != fit["scripts"] or cscalibrate.stale_reference():
        calibrated = timed("clientscript calibration", cscalibrate.calibrate_cache, say)
        if calibrated and calibrated["saved"]:
            fit = clientscript.verify()
    if fit["read"] == fit["scripts"]:
        ctx = timed("clientscript stack effects and types", csdecomp.run_deep, csdecomp.context)
        _, _, listed = timed("clientscript decompile", csdecomp.run_deep, csdecomp.write, folder, ctx, say)
        from rs3mcp import scriptnames
        timed("clientscript names", scriptnames.run, folder, None, None, say)
        saved = bool(calibrated and calibrated["saved"])
        if saved and listed:
            # a wrongly paired op shows as scripts whose stack no longer adds up
            say("clientscript: %d scripts fail with the calibrated map; the previous map is %s"
                % (len(listed), os.path.join(DATA, "clientscript-opmap.previous.json")))
        elif not listed and (saved or not os.path.exists(cscalibrate.REFERENCE)):
            # this update is what the next one gets lined up against. Only after a map that was
            # calibrated to it, or with no reference at all: a map that merely reads is not enough
            timed("clientscript reference", cscalibrate.snapshot)
        return set(listed)
    # the database is still worth installing without them
    say("clientscript: the opcode map could not be fitted to this update, so the scripts are not "
        "rebuilt this time")
    return set()


def build():
    # build_db refuses to overwrite, which is right when a person runs it and wrong here: this is
    # our own scratch file and a leftover from a previous attempt would stop every future rebuild.
    if os.path.exists(NEW):
        os.remove(NEW)
    # a first build carries from the default
    first = os.path.join(common.DEFAULT_ROOT, "rs3-cache.db")
    carry = ["--carry", DB] if os.path.exists(DB) else ["--carry", first] if os.path.exists(first) else []
    p = subprocess.run([sys.executable, os.path.join(ROOT, "build_db.py"), STAGE, CSVS, NEW] + carry,
                       capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0 or not os.path.exists(NEW):
        raise SystemExit("build_db failed:\n" + (p.stdout + p.stderr)[-1500:])
    for line in p.stdout.splitlines():
        if "writing" in line or "carry" in line or "cracked" in line:
            say(line.strip())


def verify(failures):
    """What the new database gains and loses. Returns (ok, findings)."""
    problems, notes = [], []
    if not os.path.exists(DB):
        # a first build: nothing to compare with, but the decode gaps still have to hold
        notes.append("first build, so there is no previous database to compare with")
        gaps(failures, problems, notes)
        return not problems, problems, notes
    old = sqlite3.connect(DB)
    new = sqlite3.connect(NEW)

    failed_pairs = {(m, i) for m, ids in failures.items() for i in ids}
    gained = lost = lost_to_decode = 0
    oldtypes = {r[0] for r in old.execute("select distinct type from config")}
    for (ctype, n) in new.execute("select type, count(*) from config group by type"):
        if ctype not in oldtypes:
            gained += n
            notes.append("%s is new: %d rows" % (ctype, n))
    for (ctype,) in old.execute("select distinct type from config"):
        A = {r[0] for r in old.execute("select id from config where type=?", (ctype,))}
        B = {r[0] for r in new.execute("select id from config where type=?", (ctype,))}
        gained += len(B - A)
        gone = A - B
        lost += len(gone)
        bad = {i for i in gone if (ctype, i) in failed_pairs}
        lost_to_decode += len(bad)
        if bad and ctype not in KNOWN_UNDECODED:
            # this mode read cleanly before, so a row it can no longer read is a regression
            problems.append("%s loses %d row(s) that no longer decode" % (ctype, len(bad)))
        elif bad:
            notes.append("%s loses %d row(s) inside its known gap: %s"
                         % (ctype, len(bad), sorted(bad)[:6]))
        if A and len(B) < len(A) * (1 - SHRINK_LIMIT):
            problems.append("%s shrank from %d to %d, more than content removal explains"
                            % (ctype, len(A), len(B)))
    gaps(failures, problems, notes)
    notes.append("config rows %d -> %d, gained %d, lost %d (%d to a decode failure)"
                 % (old.execute("select count(*) from config").fetchone()[0],
                    new.execute("select count(*) from config").fetchone()[0],
                    gained, lost, lost_to_decode))

    for table in ("param_use", "enum_entry", "varbit", "param_type", "inv",
                  "inv_stock", "loc_section", "sprite_name", "model_use"):
        try:
            a = old.execute("select count(*) from %s" % table).fetchone()[0]
            b = new.execute("select count(*) from %s" % table).fetchone()[0]
        except sqlite3.Error:
            problems.append("%s is missing from the new database" % table)
            continue
        if b == 0 and a > 0:
            problems.append("%s came out empty" % table)
        elif b < a * (1 - SHRINK_LIMIT):
            problems.append("%s shrank from %d to %d" % (table, a, b))
        else:
            notes.append("%-12s %d -> %d" % (table, a, b))

    old.close()
    new.close()
    return not problems, problems, notes


def gaps(failures, problems, notes):
    """The decode gaps have to still be the size they were, and no other mode may have one."""
    for mode, allowed in sorted(KNOWN_UNDECODED.items()):
        got = len(failures.get(mode) or ())
        if got > allowed:
            problems.append("%s failed on %d records where its known gap is %d, so %d of them "
                            "are new" % (mode, got, allowed, got - allowed))
        elif got < allowed:
            notes.append("%s failed on %d where %d was expected, so the gap has shrunk; lower "
                         "the number in KNOWN_UNDECODED" % (mode, got, allowed))
    for mode, ids in sorted(failures.items()):
        if ids and mode not in KNOWN_UNDECODED:
            problems.append("%s had %d record(s) fail to decode, which is new"
                            % (mode, len(ids)))


def swap_assets():
    """Move the freshly extracted assets to where the readers look, keeping the old copy."""
    for mode in ASSET_MODES:
        src = os.path.join(STAGE, mode)
        dst = os.path.join(LIVE_STAGE, mode)
        if not os.path.isdir(src) or not os.listdir(src):
            say("%s came out empty, leaving the existing copy alone" % mode)
            continue
        os.makedirs(LIVE_STAGE, exist_ok=True)
        bak = dst + ".bak"
        shutil.rmtree(bak, ignore_errors=True)
        kept = os.path.isdir(dst)
        if kept:
            os.rename(dst, bak)
        shutil.move(src, dst)
        say("%-14s %d files installed%s" % (mode, len(os.listdir(dst)),
                                            ", previous kept as " + os.path.basename(bak) if kept else ""))


def swap():
    """Put the new database in place, keeping the old one as the backup. False if that failed."""
    if not os.path.exists(DB):
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        os.replace(NEW, DB)
        say("installed the first database")
        return True
    # replace, not remove then move: removing first lost the backup whenever the move then failed
    try:
        os.replace(DB, BAK)
        os.replace(NEW, DB)
    except PermissionError:
        # Windows will not rename a file a running rs3-mcp.py has open. Copying the pages into the
        # open file works under a reader, which picks the new data up on its next query
        if not os.path.exists(NEW):
            return False
        if not os.path.exists(DB):
            os.replace(BAK, DB)
        try:
            copy_pages(DB, BAK)
            copy_pages(NEW, DB)
        except sqlite3.Error as e:
            say("could not copy into the open database: %s" % e)
            return False
        os.remove(NEW)
        say("installed by copying into the open database; the previous one is %s" % os.path.basename(BAK))
        return True
    say("installed; the previous database is %s" % os.path.basename(BAK))
    return True


def copy_pages(src, dst):
    a, b = sqlite3.connect(src), sqlite3.connect(dst, timeout=60)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def index():
    # after the assets move, since the index reads the scripts where the readers find them
    p = subprocess.run([sys.executable, INDEXER], capture_output=True, text=True, cwd=ROOT)
    say(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "index rebuilt")


def after():
    """Check the database that was just installed, and say what a rebuild moved.

    Doing this by hand is how three written claims and a stale token index survived the last
    rebuild until somebody happened to look. Numbers moving is the expected outcome of a rebuild
    rather than a fault, so this reports them and does not fail; a known-answer check breaking is
    a different thing and is called out as one."""
    mcp = os.path.join(ROOT, "rs3-mcp.py")

    # the sentences that count the data move with it, so they are rewritten before anything checks them
    p = subprocess.run([sys.executable, "-c", "from rs3mcp.test import claims\n"
                        "for path, was, now in claims.rewrite(): print(path, was, now)"],
                       capture_output=True, text=True, cwd=ROOT)
    for line in p.stdout.strip().splitlines():
        say("  rewrote %s" % line)
    if p.returncode != 0:
        say("  the written counts could not be rewritten: %s" % (p.stderr.strip().splitlines() or ["?"])[-1])

    p = subprocess.run([sys.executable, mcp, "--selftest"], capture_output=True, text=True,
                       cwd=ROOT)
    tail = [l for l in p.stdout.strip().splitlines() if "checks" in l]
    say("selftest: %s" % (tail[-1].strip() if tail else "did not report"))
    if "skipped, for want of" in p.stdout:
        say("  some checks were skipped for data this machine does not have; --test lists them")
    if p.returncode != 0:
        say("  a known-answer check is failing, which a rebuild should not cause on its own")

    p = subprocess.run([sys.executable, mcp, "rs3_drift"], capture_output=True, text=True,
                       cwd=ROOT)
    try:
        d = json.loads(p.stdout)
        say("drift: %s" % d.get("verdict"))
        for f in d.get("findings", []):
            say("  %s: %s is now %s" % (f["check"].split("--")[-1].strip()[:44],
                                        f["value"], f["expected"]))
        if d.get("findings"):
            say("  these are sentences describing the old data; update them and re-run rs3_drift")
    except ValueError:
        say("drift: could not be read")

    p = subprocess.run([sys.executable, mcp, "--diff"], capture_output=True, text=True, cwd=ROOT)
    moved = [l.strip() for l in p.stdout.splitlines() if l.strip().startswith("MOVED")]
    say("goldens: %s" % ("nothing moved" if not moved
                         else "%d answer(s) moved -- %s"
                              % (len(moved), ", ".join(m.split()[1] for m in moved[:6]))))


def main():
    check_only = "--check" in sys.argv
    force = "--force" in sys.argv
    no_swap = "--no-swap" in sys.argv

    needed, why = due()
    print("cache rebuild: %s" % why)
    if check_only:
        return 0 if not needed else 1
    if not needed and not force:
        return 0

    start = time.time()
    say("extracting from the game cache")
    failures = timed("extract", extract)
    say("building")
    timed("build", build)
    say("checking it against the database it would replace")
    ok, problems, notes = timed("verify", verify, failures)
    for n in notes:
        say(n)
    if not ok:
        print()
        print("  NOT installing. %s" % ("; ".join(problems))[:400])
        print("  the built database is at %s if you want to look" % os.path.basename(NEW))
        return 1
    if no_swap:
        say("verified, and --no-swap was given, so nothing was installed")
        return 0
    if not timed("install", swap):
        print()
        print("  NOT installing: something holds %s open, most likely a running rs3-mcp.py. Close "
              "the sessions using it and run this again; the built database is %s"
              % (os.path.basename(DB), os.path.basename(NEW)))
        return 1
    timed("assets", swap_assets)
    # what is left of the stage is decoded json the database now holds, a few hundred MB nothing
    # reads again. A build that is not installed keeps it to look at.
    shutil.rmtree(STAGE, ignore_errors=True)
    if common.OWN_ROOT:
        # names again, now its database exists
        from rs3mcp import scriptnames
        timed("clientscript names", scriptnames.run, os.path.join(LIVE_STAGE, "clientscript"), None, None, say)
    timed("index", index)
    if common.OWN_ROOT:
        # docs describe the default build
        say("rebuilt and installed in %.1fs; docs and checks left to the default build" % (time.time() - start))
        return 0
    from rs3mcp import docs
    timed("docs", docs.write, None, None, lambda msg: None)
    say("rebuilt and installed in %.1fs" % (time.time() - start))
    say("checking what the rebuild moved")
    timed("checks", after)
    return 0


def timed(label, fn, *args):
    t = time.time()
    got = fn(*args)
    say("%s took %.1fs" % (label, time.time() - t))
    return got


if __name__ == "__main__":
    sys.exit(main())
