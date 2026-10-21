"""Fetch the client api docs from the public site into a fresh local mirror.

The docs went public on 2026-10-20 at developer.runescape.com/rs/. Every page reachable from
getting-started.html under /rs/ is fetched, and its article written as markdown to
data/api-docs/<path>.md with the page's url and the day it was fetched on top. A page that fails is
named at the end, never skipped quietly.

Politely: one request at a time, a second apart; robots.txt is obeyed; a page that hasn't changed
since the last run is asked for with its ETag and answers 304 with no body, so a refresh of an
unchanged site is ~105 tiny requests; a 429 or 503 waits as long as Retry-After says (or a
minute) and tries twice more, then gives up on that page.

  python rs3api/docsfetch.py           fetch what changed, then regenerate the Lua typings
"""

import glob
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from bs4 import BeautifulSoup, NavigableString

ROOT = "https://developer.runescape.com/rs/"
START = ROOT + "getting-started.html"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rs3mcp.common import ROOT  # noqa: E402  run as a script, rs3cache isn't on the path

OUT = os.path.join(ROOT, "api-docs")
# each page's ETag and the links it had, so an unchanged page needn't be downloaded to be crawled
SEEN = os.path.join(OUT, ".seen.json")
AGENT = "rs3cache-docs-mirror/1.0 (one request a second, unchanged pages skipped)"
GAP = 1.0


def pages():
    """{name: text} for every page in the mirror, named by its path: core/events."""
    out = {}
    for p in sorted(glob.glob(os.path.join(OUT, "**", "*.md"), recursive=True)):
        out[os.path.relpath(p, OUT)[:-3].replace("\\", "/")] = io.open(p, encoding="utf-8").read()
    return out


def get(url, etag=None):
    """The page's html and ETag, or (None, etag) when it hasn't changed."""
    headers = {"User-Agent": AGENT}
    if etag:
        headers["If-None-Match"] = etag
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return r.read().decode("utf-8", "replace"), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag
            if e.code not in (429, 503) or attempt == 2:
                raise
            wait = e.headers.get("Retry-After", "")
            time.sleep(int(wait) if wait.isdigit() else 60)


def text_of(node):
    """A node's text with inline code kept as `code`."""
    out = []
    for c in node.children:
        if isinstance(c, NavigableString):
            out.append(str(c))
        elif c.name == "code":
            out.append("`%s`" % c.get_text())
        elif c.name == "br":
            out.append(" ")
        else:
            out.append(text_of(c))
    return " ".join("".join(out).split())


def markdown(article):
    lines = []
    for el in article.find_all(["h1", "h2", "h3", "h4", "h5", "p", "pre", "li", "table"]):
        # nested pieces print with their parent
        if el.find_parent(["li", "table"]):
            continue
        if el.name == "pre":
            lines += ["```", el.get_text().rstrip(), "```", ""]
        elif el.name == "table":
            for i, row in enumerate(el.find_all("tr")):
                cells = [text_of(c).replace("|", "/") for c in row.find_all(["th", "td"])]
                lines.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    lines.append("|" + " --- |" * len(cells))
            lines.append("")
        elif el.name == "li":
            lines.append("- " + text_of(el))
        elif el.name.startswith("h"):
            lines += ["", "#" * int(el.name[1]) + " " + el.get_text(" ", strip=True).rstrip("¶ "), ""]
        else:
            lines += [text_of(el), ""]
    return "\n".join(lines).strip() + "\n"


def main():
    robots = urllib.robotparser.RobotFileParser(urllib.parse.urljoin(ROOT, "/robots.txt"))
    try:
        robots.read()
    except OSError as e:
        # urlopen raises URLError offline: nothing else would reach the site either
        print("the docs site can't be reached (%s); the mirror and typings stay as they are" % e)
        return 1
    try:
        known = json.load(io.open(SEEN, encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    seen, queue, failed, changed = set(), [START], [], []
    day = time.strftime("%Y-%m-%d")
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not robots.can_fetch(AGENT, url):
            failed.append("%s: robots.txt says no" % url)
            continue
        rel = url[len(ROOT):-len(".html")]
        path = os.path.join(OUT, *rel.split("/")) + ".md"
        before = known.get(url, {})
        try:
            html, etag = get(url, before.get("etag") if os.path.isfile(path) else None)
        except Exception as e:
            failed.append("%s: %s" % (url, e))
            continue
        finally:
            time.sleep(GAP)
        if html is None:
            links = before.get("links", [])
        else:
            page = BeautifulSoup(html, "html.parser")
            links = sorted({urllib.parse.urljoin(url, a["href"]).split("#")[0] for a in page.find_all("a", href=True)})
            article = page.find("article")
            if article is None:
                failed.append("%s: no article" % url)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("source: %s\nfetched: %s\n\n%s" % (url, day, markdown(article)))
            known[url] = {"etag": etag, "links": links}
            changed.append(rel)
        queue += [l for l in links if l.startswith(ROOT) and l.endswith(".html") and l not in seen]
    with io.open(SEEN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(known, f, indent=1, sort_keys=True)
    print("%d pages, %d fetched new or changed, the rest unchanged (304)" % (len(seen) - len(failed), len(changed)))
    for f in failed:
        print("FAILED", f)
    # the Lua typings follow the docs, and say what changed
    from rs3api import docstypes
    docstypes.main()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
