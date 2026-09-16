"""
The RS3 wiki, cached locally so a repeat question costs nothing.

The cache knows ids, models and vars. It does not know drop rates, quest walkthroughs, mechanics or
anything a person wrote down. The wiki does, and most of it is structured: a drop line is
{{DropsLine|name=Infernal ashes|quantity=1|rarity=Always}}, an infobox is key=value, so both parse
cleanly without scraping rendered html.

Fetching is sequential, one second apart, never parallel, with a User-Agent that says what this is
and who to contact (RS3MCP_CONTACT), skip anything already stored, and back off on 429 rather than
retrying hard. Everything fetched is kept in rs3-wiki.db, so the second question about a page is a
local read and the wiki is never asked twice for the same thing.

Bulk pulls belong in wikiharvest.py, not here. A tool call fetches one page at most.

Tools:
  rs3_wiki  a page, its infobox, its drop table, or a search
"""

import io
import json
import os
import re
import sqlite3
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

from .common import ROOT, connect

STORE = os.path.join(ROOT, "rs3-wiki.db")
API = "https://runescape.wiki/api.php"
# The wiki asks bots to say who runs them. Whoever runs this sets RS3MCP_CONTACT; an add-on can too.
UA = "rs3cache/1.0 (local tooling over the RS3 game cache; contact via %s)" % (
    os.environ.get("RS3MCP_CONTACT") or "the person running rs3cache, who has not set RS3MCP_CONTACT")

# one second between requests, never parallel
GAP = 1.0
_LAST = [0.0]
# why the last request failed, so the answer can say "unreachable" instead of "no such page"
_ERR = [None]
_DB = {}


def _db():
    if "conn" in _DB:
        return _DB["conn"]
    conn = sqlite3.connect(STORE)
    conn.execute("create table if not exists page (title text primary key, wikitext text, "
                 "fetched integer, missing integer default 0)")
    conn.execute("create table if not exists search (q text primary key, results text, "
                 "fetched integer)")
    conn.commit()
    _DB["conn"] = conn
    return conn


def _windows_fetch_root():
    """Have Windows fetch the wiki once, so it downloads the root certificate Python is missing.

    A fresh Windows ships a few roots and pulls the rest the first time its own networking needs
    one. Python only reads what is already there, so on a new machine every request failed with
    CERTIFICATE_VERIFY_FAILED until something else, a browser say, had visited the site.
    """
    if os.name != "nt" or _DB.get("rootFetched"):
        return False
    _DB["rootFetched"] = True
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Invoke-WebRequest -UseBasicParsing -Method Head https://runescape.wiki/ | Out-Null"],
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _get(params):
    """One request, spaced. Returns None rather than raising, so a wiki outage degrades to
    whatever is cached instead of breaking the tool."""
    wait = GAP - (time.time() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    params = dict(params)
    params["format"] = "json"
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    _ERR[0] = None
    for attempt in range(3):
        # urlopen's shared opener loads the certificate store once, so after Windows has fetched a
        # root it takes a context of its own to see it
        ctx = ssl.create_default_context() if _DB.get("rootFetched") else None
        try:
            with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
                _LAST[0] = time.time()
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            _LAST[0] = time.time()
            _ERR[0] = str(getattr(e, "reason", None) or e)[:200]
            if "CERTIFICATE_VERIFY_FAILED" in _ERR[0] and _windows_fetch_root():
                continue
            code = getattr(e, "code", None)
            if code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if attempt == 2:
                return None
            time.sleep(2)
    return None


def wikitext(title, refresh=False):
    """Page source, from the local store unless it is missing or a refresh is asked for."""
    conn = _db()
    row = conn.execute("select wikitext, missing, fetched from page where title=?", (title,)).fetchone()
    # a missing page is asked about again after a day, since pages get written and outages were
    # once stored as missing too
    if row and not refresh and not (row[1] and time.time() - row[2] > 86400):
        return None if row[1] else row[0]

    d = _get({"action": "query", "prop": "revisions", "rvprop": "content",
              "rvslots": "main", "titles": title, "redirects": 1})
    text = None
    if d is None:
        # a failed request says nothing about the page, and storing it as missing kept it missing
        return row[0] if row and not row[1] else None
    if d:
        pages = (d.get("query") or {}).get("pages") or {}
        for _, pg in pages.items():
            revs = pg.get("revisions") or []
            if revs:
                text = revs[0]["slots"]["main"].get("*")
    conn.execute("insert or replace into page values (?,?,?,?)",
                 (title, text, int(time.time()), 0 if text else 1))
    conn.commit()
    return text


# --------------------------------------------------------------------------- parsing
def _split_params(body):
    """Template parameters, respecting nested {{ }} and [[ ]] so a link with a pipe in it does not
    end the parameter early."""
    out, buf, depth = [], [], 0
    i = 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth = max(0, depth - 1)
            buf.append(two)
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(body[i])
        i += 1
    out.append("".join(buf))
    return out


def templates(text, name):
    """Every {{name|...}} on the page, as dicts. Brace matched, because a drop line can carry a
    nested template in its rarity field."""
    out = []
    pat = re.compile(r"\{\{\s*" + re.escape(name) + r"\s*(\||\})", re.I)
    for m in pat.finditer(text or ""):
        depth, i = 0, m.start()
        while i < len(text):
            if text[i:i + 2] == "{{":
                depth += 1
                i += 2
                continue
            if text[i:i + 2] == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    break
                continue
            i += 1
        body = text[m.start() + 2:i - 2]
        parts = _split_params(body)[1:]
        rec = {}
        for n, p in enumerate(parts, 1):
            if "=" in p:
                k, v = p.split("=", 1)
                rec[k.strip()] = v.strip()
            else:
                rec[str(n)] = p.strip()
        out.append(rec)
    return out


def _clean(v):
    v = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", v or "")
    v = re.sub(r"<[^>]+>", "", v)
    v = re.sub(r"\{\{[^}]*\}\}", "", v)
    return v.strip()


def infobox(text):
    for name in ("Infobox Monster", "Infobox Item", "Infobox NPC", "Infobox Quest",
                 "Infobox Scenery", "Infobox Bonuses"):
        got = templates(text, name)
        if got:
            return name, {k: _clean(v) for k, v in got[0].items() if _clean(v)}
    return None, {}


def drops(text):
    out = []
    for d in templates(text, "DropsLine"):
        out.append({"item": _clean(d.get("name") or d.get("1") or ""),
                    "quantity": _clean(d.get("quantity") or ""),
                    "rarity": _clean(d.get("rarity") or ""),
                    "note": _clean(d.get("altimage") or d.get("note") or "") or None})
    return [d for d in out if d["item"]]


def shared_tables(text):
    """DropsLineEffigy, DropsLineMimic and friends: whole shared tables named by one template
    rather than listed item by item. Reporting only DropsLine leaves a drop list looking complete
    when it is missing five of these."""
    out = []
    for m in re.finditer(r"\{\{\s*(DropsLine[A-Za-z]+)\s*[|}]", text or ""):
        name = m.group(1)
        if name == "DropsLine":
            continue
        rec = {"table": name[len("DropsLine"):]}
        got = templates(text, name)
        if got:
            rec["params"] = {k: _clean(v) for k, v in got[0].items() if _clean(v)}
        if rec not in out:
            out.append(rec)
    return out


_IDS = {}


def _item_ids():
    """Cache display name lowered -> ids. The wiki names drops the way the game displays them, so
    this is the join that turns a drop table into item ids."""
    if _IDS:
        return _IDS
    try:
        c = connect()
        for r in c.execute("select id, display from config where type='items' "
                           "and display is not null"):
            _IDS.setdefault(r["display"].strip().lower(), []).append(r["id"])
    except Exception:
        pass
    return _IDS


def _resolve(name):
    """Ids for a wiki drop name, or None. Noted and dose variants share a display name, so several
    ids for one name is normal and not an error."""
    ids = _item_ids().get((name or "").strip().lower())
    if not ids:
        # the wiki writes "Coins" where the cache has several coin configs, and brackets a dose
        base = re.sub(r"\s*\(\d+\)$", "", name or "").strip().lower()
        ids = _item_ids().get(base)
    return sorted(ids)[:6] if ids else None


def tool_wiki(args):
    conn = _db()
    title = str(args.get("page") or args.get("title") or "").strip()
    q = str(args.get("search") or "").strip()
    refresh = bool(args.get("refresh"))

    if not title and not q:
        n = conn.execute("select count(*) from page where missing=0").fetchone()[0]
        miss = conn.execute("select count(*) from page where missing=1").fetchone()[0]
        return {"cachedPages": n, "knownMissing": miss, "store": STORE,
                "note": "pass page= for one page, search= to find one. Everything fetched is kept, "
                        "so asking twice never touches the wiki. Bulk pulls go through "
                        "wikiharvest.py, not through here."}

    if q and not title:
        row = conn.execute("select results from search where q=?", (q,)).fetchone()
        if row and not refresh:
            return {"query": q, "results": json.loads(row[0]), "from": "cache"}
        d = _get({"action": "query", "list": "search", "srsearch": q, "srlimit": 12})
        if not d:
            return {"query": q, "results": [], "note": "wiki unreachable and nothing cached"}
        res = [{"title": r["title"], "words": r.get("wordcount")}
               for r in (d.get("query") or {}).get("search", [])]
        conn.execute("insert or replace into search values (?,?,?)",
                     (q, json.dumps(res), int(time.time())))
        conn.commit()
        return {"query": q, "results": res, "from": "wiki"}

    text = wikitext(title, refresh)
    if text is None and _ERR[0]:
        return {"page": title, "found": False, "wikiUnreachable": _ERR[0],
                "note": "the wiki could not be reached, so nothing is known about this page yet. "
                        "Ask again once it can be."}
    if text is None:
        return {"page": title, "found": False,
                "note": "no such page, or a redirect that went nowhere. Try search=."}

    want = str(args.get("want") or "").strip().lower()
    box_name, box = infobox(text)
    out = {"page": title, "chars": len(text)}
    if box:
        out["infoboxType"] = box_name
        out["infobox"] = box
    dl = drops(text)
    if dl:
        hit = 0
        for d in dl:
            ids = _resolve(d["item"])
            if ids:
                d["itemIds"] = ids
                hit += 1
        out["drops"] = dl[:200]
        out["dropCount"] = len(dl)
        out["resolvedToCacheIds"] = "%d of %d" % (hit, len(dl))
    st = shared_tables(text)
    if st:
        out["sharedDropTables"] = st
    if want == "wikitext" or args.get("raw"):
        out["wikitext"] = text[:20000]
    out["note"] = ("infobox and drops are parsed from the page templates, not from rendered html. "
                   "Pass raw=true for the source. Rarity strings are the wiki's own wording, and "
                   "sharedDropTables are whole tables named by one template rather "
                   "than listed out, so a drop list is only complete with those too.")
    return out


TOOLS = [
    {"name": "rs3_wiki", "fn": tool_wiki,
     "description": "The RS3 wiki, cached locally: a page's infobox and drop table from its "
                    "templates, or a search. Covers what the cache cannot -- drop rates, "
                    "mechanics, requirements, anything a person wrote down. One page per call, "
                    "and never the same page twice.",
     "inputSchema": {"type": "object", "properties": {
         "page": {"type": "string", "description": "exact page title, e.g. Abyssal demon"},
         "search": {"type": "string", "description": "find a page by words"},
         "raw": {"type": "boolean", "description": "include the wikitext source"},
         "refresh": {"type": "boolean", "description": "re-fetch even if stored"}}}},
]
