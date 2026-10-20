"""
Bulk wiki pull, into the same store rs3_wiki reads.

    python rs3mcp/wikiharvest.py quests
    python rs3mcp/wikiharvest.py monsters items scenery
    python rs3mcp/wikiharvest.py --list

Courtesy: one request per second, never parallel, skip anything already
stored, back off on 429. The rule is about not hammering the wiki, so this asks for fifty pages per
request rather than one. That is fifty times fewer requests for the same data, which is more polite
than the naive version and finishes in minutes instead of an hour.

Resumable. Every page is written as it arrives, so stopping halfway loses nothing and re-running
picks up where it left off.
"""
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from rs3mcp.wiki import _db, API, STORE, UA, GAP  # noqa

BATCH = 50
_LAST = [0.0]

SETS = {
    "quests": ["Category:Quests"],
    "monsters": ["Category:Monsters"],
    "items": ["Category:Items"],
    "scenery": ["Category:Scenery"],
    "npcs": ["Category:Non-player characters"],
    "skills": ["Category:Skills"],
    "minigames": ["Category:Minigames"],
    "music": ["Category:Music tracks"],
}


def _get(params, tries=3):
    wait = GAP - (time.time() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    params = dict(params)
    params["format"] = "json"
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                _LAST[0] = time.time()
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            _LAST[0] = time.time()
            if getattr(e, "code", None) == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if attempt == tries - 1:
                return None
            time.sleep(3)
    return None


def members(category):
    """Every page in a category, following continuation."""
    out, cont = [], None
    while True:
        p = {"action": "query", "list": "categorymembers", "cmtitle": category,
             "cmlimit": 500, "cmnamespace": 0}
        if cont:
            p["cmcontinue"] = cont
        d = _get(p)
        if not d:
            break
        out += [m["title"] for m in (d.get("query") or {}).get("categorymembers", [])]
        cont = (d.get("continue") or {}).get("cmcontinue")
        if not cont:
            break
        print("      %d titles..." % len(out), flush=True)
    return out


def fetch(titles):
    """Fifty pages per request. Fewer requests for the same data is the polite direction."""
    conn = _db()
    have = {r[0] for r in conn.execute("select title from page")}
    todo = [t for t in titles if t not in have]
    print("    %d titles, %d already stored, %d to fetch"
          % (len(titles), len(titles) - len(todo), len(todo)), flush=True)
    got = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        d = _get({"action": "query", "prop": "revisions", "rvprop": "content",
                  "rvslots": "main", "titles": "|".join(chunk), "redirects": 1})
        if not d:
            print("      request failed, skipping %d" % len(chunk), flush=True)
            continue
        pages = (d.get("query") or {}).get("pages") or {}
        seen = set()
        for _, pg in pages.items():
            title = pg.get("title")
            revs = pg.get("revisions") or []
            text = revs[0]["slots"]["main"].get("*") if revs else None
            conn.execute("insert or replace into page values (?,?,?,?)",
                         (title, text, int(time.time()), 0 if text else 1))
            seen.add(title)
            if text:
                got += 1
        # a redirect answers under its target, mark both
        for t in chunk:
            if t not in seen:
                conn.execute("insert or replace into page values (?,?,?,?)",
                             (t, None, int(time.time()), 1))
        conn.commit()
        if (i // BATCH) % 5 == 0:
            print("      %d/%d" % (min(i + BATCH, len(todo)), len(todo)), flush=True)
    return got


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--list" in sys.argv or not args:
        print("sets: " + ", ".join(sorted(SETS)))
        conn = _db()
        n = conn.execute("select count(*) from page where missing=0").fetchone()[0]
        print("stored: %d pages" % n)
        return
    t0 = time.time()
    for name in args:
        cats = SETS.get(name)
        if not cats:
            print("  unknown set: %s" % name)
            continue
        print("  %s" % name, flush=True)
        titles = []
        for c in cats:
            titles += members(c)
        titles = sorted(set(titles))
        got = fetch(titles)
        print("    %s: %d new pages" % (name, got), flush=True)
    conn = _db()
    n = conn.execute("select count(*) from page where missing=0").fetchone()[0]
    print("done in %.0fs, %d pages stored, %.1f MB"
          % (time.time() - t0, n, os.path.getsize(
              STORE) / 1e6))


if __name__ == "__main__":
    main()
