"""
Catches what the harvest plugins post and writes it to disk.

The client is the only place a quest's readable title lives. The cache has 531 Quest configs and an
empty data column for every one of them, and the titles are not in the clientscripts either, so
anything keyed on a config codename -- XMAS18_QUEST, OZAN1_QUEST, QUEST_200 -- cannot be resolved
without asking a running game.

Routes on the path. /quests keeps its own filename because the quests module already reads it;
anything under /config/<Type> lands in config_<type>.json beside it.

  python quest-harvest-server.py           listen until interrupted, on PORT below
  python quest-harvest-server.py --once    exit once a batch set looks complete
"""

import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from rs3mcp import common  # noqa: E402
# config harvests land where the add-on's paths say
try:
    from rs3api.paths import API_INFO  # noqa: E402
except ImportError:
    API_INFO = None

OUT = os.path.join(API_INFO, "quest_configs.json") if API_INFO else None

collected = {}
last_seen = [0.0]


# dev.ps1 starts this, quest-dev posts to it and howto tells you which it is.
# claims.py checks all four agree.
PORT = 8777
HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
# a batch is about 100 rows
MAX_POST = 16 * 1024 * 1024


def rec_kind(path):
    """The kind out of /rec/<kind>, or None when this is not a recording post.

    Restricted to a plain word because it names a file. Anything with a slash, a dot or a space
    would be a path the caller chose rather than a name."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) != 2 or parts[0] != "rec":
        return None
    kind = parts[1]
    if not kind.replace("_", "").isalnum() or len(kind) > 32:
        return None
    return kind.lower()


def target(path):
    """Where a post lands. /quests keeps its own filename for the sake of what already reads it;
    anything under /config/<Type> gets a file named after the type. Nowhere, without the add-on
    that says where harvests land."""
    if OUT is None:
        return None, None
    if path.rstrip("/").endswith("/quests"):
        return "quests", OUT
    if path.rstrip("/").endswith("/npcs"):
        return "npcs", os.path.join(os.path.dirname(OUT), "npc_locations.json")
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if not name or not name.replace("_", "").isalnum():
        return None, None
    return name, os.path.join(os.path.dirname(OUT), "config_%s.json" % name.lower())


# A recording only grows and is read from the end, so it appends rather than joining the collect
# and rewrite path the config harvests use. One file per kind, and the kind is whatever the path
# said, so a new payload needs no change here.
def rec_path(kind):
    return os.path.join(common.ROOT, "rec-%s.jsonl" % kind)


def append_rec(kind, rows):
    """One json object per line, time stamped. Returns how many landed.

    Row contents are not inspected. What a row means is the reader's problem, and a sink that
    checked shapes would need editing every time something new was worth recording."""
    n = 0
    with io.open(rec_path(kind), "a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            if not isinstance(r, dict):
                continue
            if "t" not in r:
                r["t"] = time.time()
            fh.write(json.dumps(r) + "\n")
            n += 1
    return n


def want_path(path):
    """The file behind /want/<kind>: what a probe should read, kept out of the plugin."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) != 2 or parts[0] != "want" or not parts[1].replace("_", "").isalnum():
        return None
    return os.path.join(common.ROOT, "client", "want-%s.json" % parts[1].lower())


class Handler(BaseHTTPRequestHandler):
    def _deny(self, code, why):
        print("  refused %s: %s" % (self.path, why))
        self.send_response(code)
        self.end_headers()

    def _refused(self):
        """Only the game client's plugins post here, and they are not web pages. A page anywhere in
        any browser on this machine could otherwise write the harvest files: any Origin is refused,
        any Host but this port stops DNS rebinding, and a body past MAX_POST is not read."""
        if self.headers.get("Origin") is not None:
            self._deny(403, "a web page (Origin %s)" % self.headers.get("Origin"))
            return True
        named = self.headers.get("Host")
        if named is not None and named.lower() not in HOSTS:
            self._deny(403, "Host %s" % named)
            return True
        if int(self.headers.get("Content-Length") or 0) > MAX_POST:
            self._deny(413, "over %d bytes" % MAX_POST)
            return True
        return False

    def do_GET(self):
        if self._refused():
            return
        path = want_path(self.path)
        if path is None or not os.path.exists(path):
            self.send_response(404)
            self.end_headers()
            return
        body = io.open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self._refused():
            return
        kind = rec_kind(self.path)
        if kind is not None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            try:
                rows = json.loads(body)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                print("  bad json on %s: %s" % (self.path, e))
                return
            n = append_rec(kind, rows if isinstance(rows, list) else [rows])
            print("  %-14s +%-5d -> %s" % ("rec/" + kind, n,
                                           os.path.basename(rec_path(kind))))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        key, path = target(self.path)
        if key is None:
            self.send_response(404)
            self.end_headers()
            # names the missing add-on
            if OUT is None:
                print("  %s wants the add-on that says where harvests land. /rec/<kind> is "
                      "unaffected" % self.path)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            batch = json.loads(body)
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            print("  bad json on %s: %s" % (self.path, e))
            return
        bucket = collected.setdefault(key, {"path": path, "rows": []})
        bucket["rows"].extend(batch)
        last_seen[0] = time.time()
        print("  %-14s +%-5d %d total" % (key, len(batch), len(bucket["rows"])), flush=True)
        save_soon()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_a):
        pass


def merge_npc(old, new):
    """Widen rather than replace. Where an npc has been seen matters more the more times it has
    been seen, and a wanderer only shows as one once the box has had a few sessions to grow."""
    if old is None:
        return dict(new)
    out = dict(old)
    a, b = old.get("count") or 0, new.get("count") or 0
    out["count"] = a + b
    for lo in ("minX", "minZ"):
        out[lo] = min(old.get(lo, new[lo]), new[lo])
    for hi in ("maxX", "maxZ"):
        out[hi] = max(old.get(hi, new[hi]), new[hi])
    # a count weighted centre, so a long session does not get outvoted by a brief one
    for c in ("centreX", "centreZ"):
        if a + b:
            out[c] = round((old.get(c, new[c]) * a + new[c] * b) / (a + b))
    tiles = list(dict.fromkeys((old.get("tiles") or []) + (new.get("tiles") or [])))
    out["tiles"] = tiles[:48]
    out["name"] = new.get("name") or old.get("name")
    return out


def load_existing():
    """Pick up where a previous session left off, so coverage builds instead of resetting."""
    path = os.path.join(os.path.dirname(OUT), "npc_locations.json")
    if not os.path.exists(path):
        return
    try:
        rows = (json.load(io.open(path, encoding="utf-8")) or {}).get("rows") or []
    except Exception:
        return
    if rows:
        collected["npcs"] = {"path": path, "rows": rows, "merge": True}
        print("  carried %d npcs forward from a previous session" % len(rows))


# A whole rewrite per batch is O(n^2): the loc harvest is 1,403 batches over 139,143 rows, and the
# last ones each re-serialised 139k rows and a byConfig index of them. It ran at 1,700 rows a
# minute and slowed as it went. The per-batch write was for crash safety, so the interval keeps it
# without paying on every batch.
SAVE_EVERY = 20.0
_saved_at = [0.0]


def save_soon():
    """Write if it has been a while. Every batch also calls save() on the way out."""
    if time.time() - _saved_at[0] >= SAVE_EVERY:
        save()


def save():
    _saved_at[0] = time.time()
    for key, bucket in collected.items():
        if key == "npcs":
            by_id = {}
            for r in bucket["rows"]:
                by_id[r.get("id")] = merge_npc(by_id.get(r.get("id")), r)
            rows = sorted(by_id.values(), key=lambda r: -(r.get("count") or 0))
            bucket["rows"] = rows
            os.makedirs(os.path.dirname(bucket["path"]), exist_ok=True)
            io.open(bucket["path"], "w", encoding="utf-8", newline="\n").write(
                json.dumps({"rows": rows}, indent=1, ensure_ascii=False))
            continue
        by_id = {}
        for r in bucket["rows"]:
            by_id[r.get("id")] = r
        rows = sorted(by_id.values(), key=lambda r: r.get("id") or 0)
        os.makedirs(os.path.dirname(bucket["path"]), exist_ok=True)
        # rows only, every reader takes them
        io.open(bucket["path"], "w", encoding="utf-8", newline="\n").write(
            json.dumps({"rows": rows}, indent=1, ensure_ascii=False))


def main():
    once = "--once" in sys.argv
    # threading, because a plugin that fires its batches at once otherwise gets
    # responseCode 0 on most of them and loses the data silently
    load_existing()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.timeout = 1
    print("  listening on http://127.0.0.1:%d, writing to" % PORT)
    print("  " + OUT)
    try:
        while True:
            srv.handle_request()
            # a gap after something arrived means the plugin has finished sending
            if once and collected and time.time() - last_seen[0] > 20:
                break
    except KeyboardInterrupt:
        pass
    save()
    for key, bucket in sorted(collected.items()):
        print("  %-14s %d written to %s"
              % (key, len({r.get("id") for r in bucket["rows"]}), os.path.basename(bucket["path"])))
    if not collected:
        print("  nothing received")


if __name__ == "__main__":
    main()

