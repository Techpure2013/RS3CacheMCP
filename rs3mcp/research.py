"""Research paid for once and believed only when it quotes its source.

Every fetch goes through the store: a page already fetched is read from disk, and fetching it again
needs a stated reason. A finding is a claim with the url it came from and a line quoted from that page;
it is kept only when the quote is on the stored page, or the local NLI finds the claim in it.
"""

import hashlib
import html
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse

from rs3mcp import common

MAX_BYTES = 5 * 1024 * 1024
FETCH_S = 60
MAX_HOPS = 5
RETRY_WAIT_S = (1, 3)
RETRY_AFTER_MAX_S = 30
# per host, between requests
COURTESY_S = 1.0
AGENT = "rs3cache-research/1 (fetches each page once)"
# curl exit codes worth retrying
TRANSIENT = {6, 7, 28, 35, 52, 55, 56}
# NLI support at or above this counts
SUPPORT_AT = 0.9
PASSAGE = 480
PASSAGES_JUDGED = 8
STAMP = "%Y-%m-%dT%H:%M:%S"
QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "‑": "-",
                        "–": "-", "—": "-", " ": " "})


def folder():
    return os.environ.get("RS3_RESEARCH") or os.path.join(common.DEFAULT_ROOT, "research")


def _index():
    return os.path.join(folder(), "fetched.jsonl")


def _findings():
    return os.path.join(folder(), "findings.jsonl")


def _body_path(sha):
    return os.path.join(folder(), "fetched", sha + ".body")


def _rows(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def _append(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def normal(url):
    """One spelling per page: scheme and host lower case, no fragment, no trailing slash."""
    p = urllib.parse.urlsplit(url.strip())
    path = p.path.rstrip("/") if p.path not in ("", "/") else ""
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def stored(url):
    """The latest fetch of this page, or None."""
    want = normal(url)
    rows = [r for r in _rows(_index()) if r.get("url") == want and "sha1" in r]
    return rows[-1] if rows else None


class _Transient(Exception):
    """A failure worth another try."""


def vet(url, resolve=socket.getaddrinfo):
    """The address to connect to for url: https only, and every address its name has must be public.

    Plain http cannot be checked end to end. A private, loopback or link-local answer means the page
    would be fetched from this machine's own network (the router, the cloud metadata address, the
    host on 8794), which a page on the internet must never be able to steer a fetch towards."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("%s: only https urls are fetched" % url)
    try:
        infos = resolve(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ValueError("%s: %s does not resolve (%s)" % (url, p.hostname, e))
    addrs = sorted({i[4][0].split("%")[0] for i in infos})
    for a in addrs:
        if not ipaddress.ip_address(a).is_global:
            raise ValueError("%s: %s resolves to %s, which is not on the public internet" % (url, p.hostname, a))
    if not addrs:
        raise ValueError("%s: %s has no address" % (url, p.hostname))
    return addrs[0]


def _curl_once(url, ip, headers):
    """(status, lower-case headers, body) for one request, pinned to the vetted address, no redirects."""
    p = urllib.parse.urlsplit(url)
    pin = "%s:%d:%s" % (p.hostname, p.port or 443, "[%s]" % ip if ":" in ip else ip)
    cmd = ["curl", "-sS", "--proto", "=https", "--tlsv1.2", "--compressed", "--max-redirs", "0",
           "--max-filesize", str(MAX_BYTES), "-m", str(FETCH_S), "-A", AGENT, "--resolve", pin, "-D", "-"]
    for k, v in headers.items():
        cmd += ["-H", "%s: %s" % (k, v)]
    done = subprocess.run(cmd + [url], capture_output=True, timeout=FETCH_S + 10)
    # certificate failures are final
    if done.returncode in TRANSIENT and not re.search(rb"SEC_E_CERT|certificate", done.stderr or b"", re.I):
        raise _Transient("curl %d on %s" % (done.returncode, url))
    if done.returncode != 0:
        # includes 60, a bad certificate
        raise ValueError("%s: curl %d, %s" % (url, done.returncode,
                                              done.stderr.decode("utf-8", "replace").strip()[:200]))
    head, _, body = done.stdout.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, body


_last_hit = {}


def _courtesy(url, sleep):
    host = urllib.parse.urlsplit(url).hostname
    wait = _last_hit.get(host, 0) + COURTESY_S - time.time()
    if wait > 0:
        sleep(wait)
    _last_hit[host] = time.time()


def download(url, headers=None, transport=None, resolve=socket.getaddrinfo, sleep=time.sleep):
    """{status, body, final, type, etag, modified}, following redirects ourselves so each hop is vetted."""
    transport = transport or _curl_once
    first = url
    for _ in range(MAX_HOPS + 1):
        ip = vet(url, resolve)
        _courtesy(url, sleep)
        for attempt in range(len(RETRY_WAIT_S) + 1):
            try:
                status, hdrs, body = transport(url, ip, headers or {})
            except _Transient as e:
                if attempt == len(RETRY_WAIT_S):
                    raise ValueError("%s: gave up after %d tries (%s)" % (url, attempt + 1, e))
                sleep(RETRY_WAIT_S[attempt])
                continue
            if status == 429 or status >= 500:
                if attempt == len(RETRY_WAIT_S):
                    raise ValueError("%s: answered %d, %d times" % (url, status, attempt + 1))
                after = hdrs.get("retry-after", "")
                sleep(min(int(after), RETRY_AFTER_MAX_S) if after.isdigit() else RETRY_WAIT_S[attempt])
                continue
            break
        if status in (301, 302, 303, 307, 308):
            if not hdrs.get("location"):
                raise ValueError("%s: a %d with nowhere to go" % (url, status))
            url = urllib.parse.urljoin(url, hdrs["location"])
            continue
        if status == 304:
            return {"status": 304, "final": url}
        if status != 200:
            raise ValueError("%s: answered %d" % (url, status))
        if len(body) > MAX_BYTES:
            raise ValueError("%s: over %d bytes" % (url, MAX_BYTES))
        return {"status": 200, "body": body, "final": url, "type": hdrs.get("content-type", ""),
                "etag": hdrs.get("etag"), "modified": hdrs.get("last-modified")}
    raise ValueError("%s: more than %d redirects" % (first, MAX_HOPS))


def fetch(url, by, refresh=None, get=None, transport=None, resolve=socket.getaddrinfo, sleep=time.sleep):
    """The page, from the store when it is there; refresh fetches again and needs a reason.

    A refresh asks the server whether the page changed (If-None-Match, If-Modified-Since); a 304 keeps
    the stored copy and moves no body. get is the tests' stand-in for the network."""
    if refresh is not None and not str(refresh).strip():
        raise ValueError("a refresh needs a reason")
    row = stored(url)
    if row and refresh is None:
        _append(_index(), {"reuse": row["url"], "at": time.strftime(STAMP), "by": by, "tokens": row["tokens"]})
        return dict(row, reused=True, path=_body_path(row["sha1"]))
    if get is not None:
        got = {"status": 200, "body": get(url), "final": normal(url)}
    else:
        ask = {}
        if row and row.get("etag"):
            ask["If-None-Match"] = row["etag"]
        if row and row.get("modified"):
            ask["If-Modified-Since"] = row["modified"]
        got = download(url, ask, transport, resolve, sleep)
    if got["status"] == 304:
        again = dict(row, at=time.strftime(STAMP), by=by, refresh=str(refresh).strip(), unchanged=True)
        _append(_index(), again)
        return dict(again, reused=False, path=_body_path(row["sha1"]))
    body = got["body"]
    if len(body) > MAX_BYTES:
        raise ValueError("%s is over %d bytes" % (url, MAX_BYTES))
    sha = hashlib.sha1(body).hexdigest()
    os.makedirs(os.path.join(folder(), "fetched"), exist_ok=True)
    with io.open(_body_path(sha), "wb") as fh:
        fh.write(body)
    row = {"url": normal(url), "at": time.strftime(STAMP), "by": by, "sha1": sha, "bytes": len(body),
           "tokens": len(body) // 4}
    for k in ("type", "etag", "modified"):
        if got.get(k):
            row[k] = got[k]
    if normal(got["final"]) != row["url"]:
        row["final"] = normal(got["final"])
    if refresh is not None:
        row["refresh"] = str(refresh).strip()
    _append(_index(), row)
    return dict(row, reused=False, path=_body_path(sha))


def _body(row):
    """A stored page's bytes, refused when they no longer match the hash they were stored under."""
    with io.open(_body_path(row["sha1"]), "rb") as fh:
        body = fh.read()
    if hashlib.sha1(body).hexdigest() != row["sha1"]:
        raise ValueError("the stored copy of %s has changed on disk since it was fetched; fetch it again "
                         "with a refresh" % row["url"])
    return body


INLINE = r"a|abbr|b|bdi|bdo|cite|code|data|dfn|em|i|kbd|mark|q|s|samp|small|span|strong|sub|sup|time|u|var|wbr"


def page_text(body, kind=None):
    """What a reader sees: html without scripts, styles and tags, entities read, spaces folded.

    An inline tag joins what is either side of it, as the page shows it: code highlighting wraps each
    token in a span, and a space for every tag read `None=0` as `None = 0`.

    kind is the stored Content-Type. Only an html page is stripped: a source file that builds html
    strings, sniffed as a page, lost every generic and comparison to the tag stripper."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    if kind and "json" in kind.lower():
        try:
            return _fold("\n".join(_strings(json.loads(text))))
        except ValueError:
            # a cut-off answer, read raw
            return _fold(text)
    html_page = "html" in kind.lower() if kind else bool(re.match(r"\s*(<!doctype html|<html)", text, re.I))
    if html_page:
        text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
        text = re.sub(r"(?is)</?(?:%s)\b[^>]*>" % INLINE, "", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
    return _fold(text)


def _strings(value):
    """Every string in a JSON value, in document order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _fold(text):
    return " ".join(text.translate(QUOTES).split())


def _squash(text):
    """Lower case with no whitespace at all, so a quote matches whatever spacing it was copied with."""
    return re.sub(r"\s+", "", text.translate(QUOTES)).lower()


def _passages(text, claim):
    """The windows of the page most like the claim by shared words, best first."""
    words = set(re.findall(r"\w{4,}", claim.lower()))
    step = PASSAGE // 2
    wins = [text[i:i + PASSAGE] for i in range(0, max(1, len(text) - step), step)] or [text]
    return sorted(wins, key=lambda w: -len(words & set(re.findall(r"\w{4,}", w.lower()))))[:PASSAGES_JUDGED]


def check_finding(f):
    """{verdict: quoted | supported | unsupported, score, passage} for a finding on a fetched page."""
    row = stored(f["url"])
    if row is None:
        raise ValueError("%s was never fetched through the store; fetch it first" % f["url"])
    text = page_text(_body(row), row.get("type"))
    quote = _fold(f.get("quote") or "")
    # older quotes kept the JSON escapes
    raw = _body(row).decode("utf-8", "replace") if "json" in (row.get("type") or "").lower() else ""
    if quote and (_squash(quote) in _squash(text) or (raw and _squash(quote) in _squash(raw))):
        return {"verdict": "quoted", "score": 1.0, "passage": quote, "sha1": row["sha1"]}
    from rs3mcp import judge
    model = judge.nli()
    if model is None:
        return {"verdict": "unsupported", "score": 0.0, "passage": "",
                "why": "the quote is not on the page and no NLI model is installed", "sha1": row["sha1"]}
    best, where = 0.0, ""
    for p in _passages(text, f["claim"]):
        score = model.entail(p, [f["claim"]])[0]
        if score > best:
            best, where = score, p
    verdict = "supported" if best >= SUPPORT_AT else "unsupported"
    return {"verdict": verdict, "score": round(best, 3), "passage": where, "sha1": row["sha1"]}


# search-results pages
SEARCH_PAGE = re.compile(r"api\.github\.com/search/|github\.com/search\b|/search\?(.*&)?q=", re.I)


def add(f, by):
    """Keep a finding that holds; refuse one that does not, saying why."""
    for k in ("claim", "url", "quote"):
        if not str(f.get(k) or "").strip():
            raise ValueError("a finding needs %s" % k)
    if SEARCH_PAGE.search(f["url"]):
        raise ValueError("%s is a search-results page, whose snippets rarely carry a whole claim. Fetch the issue "
                         "or page itself and quote that" % f["url"])
    ident =hashlib.sha1((normal(f["url"]) + "|" + f["claim"]).encode("utf-8")).hexdigest()[:10]
    # one quote per claim id
    had = {r["id"]: r for r in _rows(_findings())}.get(ident)
    if had is not None:
        if _squash(had["quote"]) == _squash(f["quote"]):
            return had
        raise ValueError("finding %s already keeps this claim on %s with another quote. To offer better words, "
                         "add them under a reworded claim and review %s with --better" % (ident, f["url"], ident))
    got = check_finding(f)
    if got["verdict"] != "quoted":
        raise ValueError("the quote is not word for word on %s (the page's closest passage scores NLI %.2f). "
                         "Quote the page's own words: a paraphrase the model accepts was wrong 37%% of the "
                         "time" % (f["url"], got["score"]))
    row = {"id": ident, "topic": f.get("topic") or "", "claim": f["claim"], "url": normal(f["url"]),
           "quote": f["quote"], "fetchedSha": got["sha1"], "by": by, "at": time.strftime(STAMP),
           "verdict": got["verdict"], "score": got["score"]}
    # chance the quote carries the claim
    from rs3mcp import judge
    try:
        row["holds"] = judge.holds(f["quote"], f["claim"])["holds"]
    except ValueError:
        pass
    gaps = judge.unseen(f["quote"], f["claim"])
    if gaps["numbers"] or gaps["files"]:
        row["unseen"] = gaps
    _append(_findings(), row)
    return row


def costs():
    """What the store has paid and saved: downloads, reads from disk, and refreshes a 304 answered."""
    rows = _rows(_index())
    fetched = [r for r in rows if "sha1" in r and not r.get("unchanged")]
    unchanged = [r for r in rows if r.get("unchanged")]
    reused = [r for r in rows if "reuse" in r]
    return {"fetched": len(fetched), "fetchedTokens": sum(r["tokens"] for r in fetched),
            "fetchedBytes": sum(r["bytes"] for r in fetched), "unchanged": len(unchanged),
            "reused": len(reused), "savedTokens": sum(r.get("tokens", 0) for r in reused + unchanged)}


def find(q, limit=6):
    """What is known already: findings nearest the question, and fetched pages whose url shares its words."""
    rows = _rows(_findings())
    known = []
    if rows and q.strip():
        from rs3mcp import judge
        known = [dict(rows[i], similarity=round(float(sim), 3))
                 for i, sim in judge.nearest(q, [r["claim"] for r in rows], k=limit)]
    words = set(re.findall(r"\w{3,}", q.lower()))
    pages = [r for r in _rows(_index()) if "sha1" in r and words & set(re.findall(r"\w{3,}", r["url"].lower()))]
    return {"known": known, "fetched": [{"url": r["url"], "at": r["at"], "path": _body_path(r["sha1"])}
                                        for r in pages[-limit:]], "costs": costs()}


REVIEWS = ("holds", "overclaims", "wrong", "unsupported")
CONTEXT = 400


def _reviews_path():
    return os.path.join(folder(), "reviews.jsonl")


def reviews():
    """The latest review of each finding, by id."""
    return {r["id"]: r for r in _rows(_reviews_path())}


def show(ident, pad=CONTEXT):
    """A finding with its latest review and the stored page's text round the quote, for a checker to read."""
    kept = {r["id"]: r for r in _rows(_findings())}
    f = kept.get(ident)
    if f is None:
        raise ValueError("no finding %s" % ident)
    row = stored(f["url"])
    text = page_text(_body(row), row.get("type"))
    quote = _fold(f["quote"])
    at = text.lower().find(quote.lower())
    if at < 0:
        # spaced differently: place it by its first word
        at = max(0, text.lower().find(quote.split()[0].lower()) if quote else 0)
    return dict(f, review=reviews().get(ident), context=text[max(0, at - pad):at + len(quote) + pad])


def review(ident, verdict, why, by, better=None):
    """A checker's ruling on a finding. Anything but holds sends the finding back at the gate."""
    kept = {r["id"] for r in _rows(_findings())}
    if ident not in kept:
        raise ValueError("no finding %s" % ident)
    if verdict not in REVIEWS:
        raise ValueError("a review is one of %s" % ", ".join(REVIEWS))
    if not str(why or "").strip():
        raise ValueError("a review says why")
    if better and better not in kept:
        raise ValueError("the better finding %s is not kept" % better)
    row = {"id": ident, "verdict": verdict, "why": str(why).strip(), "by": by, "at": time.strftime(STAMP)}
    if better:
        row["better"] = better
    _append(_reviews_path(), row)
    return row


def gate(hb):
    """What is wrong with a researcher's handback: every finding id kept and holding, no bare claims."""
    kept = {r["id"]: r for r in _rows(_findings())}
    ruled = reviews()
    out = []
    for ident in hb.get("findings") or []:
        r = kept.get(ident.strip())
        said = ruled.get(ident.strip())
        if r is None:
            out.append("finding %s is not in the findings store: add it with rs3_research {add: ...} and "
                       "list the id it gives" % ident)
        elif r["verdict"] not in ("quoted", "supported"):
            out.append("finding %s is %s" % (ident, r["verdict"]))
        elif said and said["verdict"] != "holds":
            out.append("finding %s was reviewed as %s: %s%s" % (
                ident, said["verdict"], said["why"],
                "; list %s instead" % said["better"] if said.get("better") else ""))
    for line in hb.get("unkeyed") or []:
        out.append("'%s' has no key, so nothing checks it: put finding:, reviewed: or fetched: on every line"
                   % line[:60])
    for line in hb.get("reviewed") or []:
        ident, _, said = line.strip().partition(" ")
        got = ruled.get(ident)
        if got is None:
            out.append("reviewed %s has no review in the store: record it with review %s --verdict ... --why ..."
                       % (ident, ident))
        elif said.strip() and said.split()[0].lower() != got["verdict"]:
            out.append("reviewed %s says %s, but the store's latest review is %s" % (ident, said.split()[0], got["verdict"]))
    from rs3mcp.handback import NOT_A_COMMAND
    for url in hb.get("fetched") or []:
        if url.strip(" -.") == "" or NOT_A_COMMAND.match(url):
            continue
        if stored(url) is None:
            out.append("fetched %s is not in the research store: fetch through rs3_research {fetch: url} or "
                       "python -m rs3mcp.research fetch, never around it" % url)
    for claim in hb.get("claims") or []:
        out.append("'%s' is a claim outside a finding: put it through rs3_research {add: ...} with a quote "
                   "and list its id under finding:" % claim[:80])
    return out


def tool_research(args):
    """{q} what is known; {fetch: url, refresh?} through the store; {add: finding} checked and kept;
    {show: id} a finding and its page; {review: {id, verdict, why, better?}} a checker's ruling."""
    by = args.get("by") or "rs3_research"
    if args.get("fetch"):
        got = fetch(args["fetch"], by, refresh=args.get("refresh"))
        return {k: got[k] for k in ("url", "at", "path", "reused", "tokens")}
    if args.get("add"):
        return add(args["add"], by)
    if args.get("show"):
        return show(args["show"])
    if args.get("review"):
        r = args["review"]
        return review(r.get("id"), r.get("verdict"), r.get("why"), by, better=r.get("better"))
    return find(args.get("q") or "")


TOOLS = [
    {"name": "rs3_research", "fn": tool_research,
     "examples": [{"q": "how LSP reports a file edit"}],
     "description": "Research paid once: q finds what is known; fetch stores a page (refresh needs a reason); add keeps a quoted finding; show and review are for checking one.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string"}, "fetch": {"type": "string"}, "refresh": {"type": "string"},
         "add": {"type": "object", "description": "claim, url, quote, topic"}, "show": {"type": "string"},
         "review": {"type": "object", "description": "id, verdict (holds|overclaims|wrong|unsupported), why, better"}}}},
]


USAGE = """python -m rs3mcp.research fetch <url> [--refresh "<reason>"]
python -m rs3mcp.research find "<question>"
python -m rs3mcp.research add --claim "<one sentence>" --url <url> --quote "<the page's own words>" [--topic <t>]
python -m rs3mcp.research show <finding id>
python -m rs3mcp.research review <finding id> --verdict holds|overclaims|wrong|unsupported --why "..." [--better <id>]"""


def main(argv):
    """The store from a shell, for an agent whose MCP server predates rs3_research."""
    # page text is any language; a cp1252 console would stop at the first arrow
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import argparse
    if not argv or argv[0] not in ("fetch", "find", "add", "show", "review"):
        print(USAGE)
        return 2
    p = argparse.ArgumentParser(prog="python -m rs3mcp.research " + argv[0])
    if argv[0] == "fetch":
        p.add_argument("url")
        p.add_argument("--refresh")
    elif argv[0] == "find":
        p.add_argument("q")
    elif argv[0] == "show":
        p.add_argument("id")
    elif argv[0] == "review":
        p.add_argument("id")
        p.add_argument("--verdict", required=True, choices=REVIEWS)
        p.add_argument("--why", required=True)
        p.add_argument("--better")
    else:
        for k in ("claim", "url", "quote"):
            p.add_argument("--" + k, required=True)
        p.add_argument("--topic", default="")
    a = p.parse_args(argv[1:])
    by = os.environ.get("RS3_WHO") or "shell"
    try:
        if argv[0] == "fetch":
            got = fetch(a.url, by, refresh=a.refresh)
            print(("stored %s" if not got["reused"] else "already fetched %s") % got["at"], got["path"])
        elif argv[0] == "find":
            print(json.dumps(find(a.q), indent=1, ensure_ascii=False))
        elif argv[0] == "show":
            print(json.dumps(show(a.id), indent=1, ensure_ascii=False))
        elif argv[0] == "review":
            print(json.dumps(review(a.id, a.verdict, a.why, by, better=a.better)))
        else:
            got = add({"claim": a.claim, "url": a.url, "quote": a.quote, "topic": a.topic}, by)
            print(json.dumps({k: got[k] for k in ("id", "verdict", "score", "url")}))
    except ValueError as e:
        print("refused: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
