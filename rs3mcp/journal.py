"""The scrying pool: a live map of what every AI session here is doing.

Claude Code already writes all of it down. Every session keeps a jsonl under ~/.claude/projects,
with each tool call, its result, the prompt that started it and the context size of each turn.
Subagents keep their own beside it. Other apps keep nothing readable, so the server writes their
rs3cache calls under data/pool in the same shape (poollog.py). This tails both and serves them to
monitor.html.

Every call is reduced to the place it touched: a file, an rs3cache tool, a shell program, a host.
Those are real names, so the map drawn from them is the machine's own layout, not a made up one.

Served on 127.0.0.1:7331 by whichever rs3cache server binds it first. The rest keep trying, so
closing that session hands the map to the next one. Nothing is read until the page asks, and
nothing is ever written to stdout, which is the MCP wire.

RS3_MONITOR=off turns it off. RS3_MONITOR_PORT moves it.
"""

import glob
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import addons

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "monitor.html")
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
PORT = int(os.environ.get("RS3_MONITOR_PORT") or 7331)


def _person():
    """Who is giving the orders, for the page to call them by name: RS3MCP_USER, else git's."""
    name = os.environ.get("RS3MCP_USER")
    if not name:
        try:
            import subprocess
            name = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    return name or "you"


USER = _person()

ACTIVE_HOURS = 24
BACKFILL = 24 * 1024 * 1024
KEEP = 40000

# a tool run from a shell, either through the server script or the rs3 command
SHELL_TOOL = re.compile(r"(?:rs3-mcp\.py[\"']?\s+|\brs3(?:\.py|\.cmd)?[\"']?\s+)(?:rs3_)?(\w+)")
NOT_A_PROMPT = ("<", "This session is being continued", "[Request interrupted", "Caveat:")

ACTS = {
    "Read": "read", "NotebookRead": "read", "LSP": "read",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit", "Write": "write",
    "Grep": "search", "Glob": "search",
    "Bash": "run", "PowerShell": "run", "Monitor": "run",
    "WebFetch": "fetch", "WebSearch": "fetch",
    "Agent": "summon", "Task": "summon", "Workflow": "summon", "SendMessage": "summon",
    "AskUserQuestion": "askuser", "ExitPlanMode": "askuser",
    "Artifact": "publish", "SendUserFile": "publish", "Skill": "skill",
}

# Shell commands that run a file are a visit to that file. Quoted paths keep their spaces.
WORD = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
CD = re.compile(r"^\s*(?:cd|Set-Location)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)[^;&|\n]*(?:&&|;|\n)\s*", re.I)
URL = re.compile(r"https?://([^/\s\"'<>]+)")
EXTS = r"\.(?:py|lua|mjs|cjs|js|ts|tsx|json|md|txt|html|css|ps1|sh|toml|ya?ml|csv|db|jsonl|d\.ts)"
FILEISH = re.compile(EXTS + r"$", re.I)
QUOTED_FILE = re.compile(r"[\"']([^\"'\n]{2,200}" + EXTS + r")[\"']", re.I)
REGEXY = re.compile(r"[\\*?\[\](){}^$|+]")
INLINE = {"python","python3", "py", "node", "lua", "bash", "sh", "pwsh", "powershell"}
READERS = {"cat", "head", "tail", "sed", "less", "wc", "type", "get-content", "gc", "awk", "diff"}
SEARCHERS = {"grep", "rg", "find", "ls", "dir", "get-childitem", "gci", "select-string", "tree"}
BASH_DRIVE = re.compile(r"^/([a-zA-Z])/")


def _ms(stamp):
    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return 0


def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
    return "\n".join(out)


def _size(content):
    """Characters an answer cost, an image counted as what it roughly costs in tokens times four."""
    if isinstance(content, str):
        return len(content)
    n = 0
    for b in content or []:
        if isinstance(b, dict):
            n += 6000 if b.get("type") == "image" else len(b.get("text") or "")
    return n


def _peek(content):
    for line in _text_of(content).splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


TESTS = re.compile(r"(?:([\w.-]+):\s+)?(\d+) passed, (\d+) failed|pass (\d+)\s+fail (\d+)"
                   r"|(\d+) checks passed|(\d+) of (\d+) checks failed|(\d+) calls, (\d+) failures")


def _tests(text):
    """[name, passed, failed] for every harness summary in an answer, whatever printed it."""
    if "pass" not in text and "fail" not in text:
        return []
    out = []
    for name, p, f, p2, f2, allp, nf, tot, calls, cf in TESTS.findall(text):
        if p:
            out.append([name, int(p), int(f)])
        elif p2:
            out.append([name, int(p2), int(f2)])
        elif allp:
            out.append(["checks", int(allp), 0])
        elif nf:
            out.append(["checks", int(tot) - int(nf), int(nf)])
        elif calls:
            out.append(["soak", int(calls) - int(cf), int(cf)])
    return out[:40]


def _lines(s):
    return (s.count("\n") + 1) if s else 0


def path_place(p, cwd=None):
    """A path as the map names it: relative to home when it is under it, forward slashes."""
    p = str(p or "").strip().strip("\"'")
    if not p:
        return None
    p = _native(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd or HOME, p)
    p = os.path.normpath(p)
    try:
        rel = os.path.relpath(p, HOME)
    except ValueError:
        rel = p
    if rel.startswith(".."):
        rel = p
    return rel.replace("\\", "/")


def _native(p):
    if p == "~" or p.startswith(("~/", "~\\")):
        p = HOME + p[1:]
    return BASH_DRIVE.sub(lambda m: m.group(1).upper() + ":/", p)


def shell_place(cmd, cwd=None):
    """(place, act, what ran) for a shell command.

    A command is a visit to the file it runs, reads or searches, when it names one. Inline scripts
    are a visit to the first file they open, since `python - <<PY` says nothing by itself.
    """
    cmd = cmd or ""
    m = CD.match(cmd)
    while m:
        cwd = os.path.join(cwd or HOME, _native(m.group(1).strip("\"'")))
        cmd = cmd[m.end():]
        m = CD.match(cmd)
    first = re.split(r"&&|\|\||[|;\n]", cmd, maxsplit=1)[0].strip()
    words = [w.strip("\"'") for w in WORD.findall(first)]
    if not words:
        return "shell/(empty)", "run", ""
    prog = os.path.basename(words[0]).lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    act = "read" if prog in READERS else "search" if prog in SEARCHERS else "run"
    shown = first[:120]

    if prog in ("curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"):
        u = URL.search(first)
        if u:
            return "web/" + u.group(1), "fetch", shown
    # A regex is not a path. Filenames count when they look like one; folders only when they exist,
    # since `grep -rn "a/b"` otherwise invents a folder called a.
    args = [w for w in words[1:] if w and not w.startswith("-") and not w.startswith(("$", "<", ">", "2>"))]
    for w in args:
        if FILEISH.search(w) and not REGEXY.search(w):
            return path_place(_native(w), cwd), act, shown
    if act == "search":
        for w in args:
            full = os.path.join(cwd or HOME, _native(w))
            if not REGEXY.search(w) and os.path.isdir(full):
                return path_place(full) + "/(searched)", act, shown
        return path_place(cwd or HOME) + "/(searched)", act, shown
    if prog in INLINE and ("-c" in words or "-" in words or "<<" in first):
        opened = next((m.group(1) for m in QUOTED_FILE.finditer(cmd) if not REGEXY.search(m.group(1))), None)
        if opened:
            return path_place(_native(opened), cwd), "run", shown
        return "shell/%s (inline)" % prog, "run", shown
    return "shell/" + prog, act, shown


class Journal:
    def __init__(self, sets=None, root=PROJECTS, advise=None):
        self.sets = sets or {}
        self.root = root
        self.advise = advise
        self.lock = threading.Lock()
        self.files = {}
        self.sessions = {}
        self.events = []
        self.where = {}
        self.seq = 0
        self.globbed = 0
        self.on_test = None

    def place(self, tool, args, cwd):
        """(place, act, detail, rs3 tool or None) for one call."""
        args = args if isinstance(args, dict) else {}
        act = ACTS.get(tool, "tool")

        if tool.startswith("mcp__rs3cache__"):
            name = tool[len("mcp__rs3cache__"):]
            detail = " ".join("%s=%s" % (k, v) for k, v in args.items())[:160]
            return "rs3cache/%s/%s" % (self.sets.get(name, "meta"), name), "ask", detail, name

        if tool in ("Bash", "PowerShell"):
            cmd = str(args.get("command") or "")
            m = SHELL_TOOL.search(cmd)
            name = m and next((n for n in (m.group(1), "rs3_" + m.group(1)) if n in self.sets), None)
            if name:
                return "rs3cache/%s/%s" % (self.sets[name], name), "ask", cmd[:160], name
            where, act, ran = shell_place(cmd, cwd)
            return where, act, ran, None

        if tool in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead"):
            where = path_place(args.get("file_path") or args.get("notebook_path"), cwd)
            if tool == "Edit":
                detail = "+%d -%d" % (_lines(args.get("new_string")), _lines(args.get("old_string")))
            elif tool == "MultiEdit":
                eds = args.get("edits") or []
                detail = "+%d -%d" % (sum(_lines(e.get("new_string")) for e in eds),
                                      sum(_lines(e.get("old_string")) for e in eds))
            elif tool == "Write":
                detail = "%d lines" % _lines(args.get("content"))
            elif args.get("offset") or args.get("limit"):
                start = int(args.get("offset") or 1)
                detail = "lines %d-%d" % (start, start + int(args.get("limit") or 2000) - 1)
            else:
                detail = ""
            return where or "claude/" + tool, act, detail, None

        if tool in ("Grep", "Glob"):
            base = args.get("path") or cwd or HOME
            full = path_place(base, cwd)
            is_file = os.path.isfile(BASH_DRIVE.sub(lambda m: m.group(1) + ":/", str(base)))
            where = full if is_file else full + "/(searched)"
            return where, "search", str(args.get("pattern") or "")[:120], None

        if tool.startswith("mcp__"):
            parts = tool.split("__")
            server, name = parts[1], parts[-1]
            url = str(args.get("url") or "")
            u = URL.search(url)
            if u:
                return "web/" + u.group(1), "fetch", url[:160], None
            if "browser" in server.lower() or "chrome" in server.lower():
                return "web/browser", "fetch", name, None
            return "mcp/%s/%s" % (server.replace("plugin_", ""), name), "call", \
                " ".join("%s=%s" % (k, v) for k, v in args.items())[:160], None

        if tool == "WebFetch":
            u = URL.search(str(args.get("url") or ""))
            return "web/" + (u.group(1) if u else "?"), act, str(args.get("url"))[:160], None
        if tool == "WebSearch":
            return "web/search", act, str(args.get("query"))[:160], None
        if tool in ("Agent", "Task"):
            return "claude/subagents/" + str(args.get("subagent_type") or "general-purpose"), act, \
                str(args.get("description") or "")[:160], None
        if tool == "Skill":
            return "claude/skills/" + str(args.get("skill") or "?"), act, str(args.get("args") or "")[:160], None
        if tool == "AskUserQuestion":
            qs = args.get("questions") or [{}]
            return "claude/asked " + USER, act, str(qs[0].get("question") or "")[:160], None
        if tool in ("Artifact", "SendUserFile"):
            f = args.get("file_path") or (args.get("files") or [None])[0]
            return (path_place(f, cwd) if f else "claude/" + tool), act, str(args.get("action") or "publish"), None
        first = next((str(v) for v in args.values() if isinstance(v, str) and v), "")
        return "claude/" + tool, act, first[:160], None

    def _emit(self, ev):
        self.seq += 1
        ev["seq"] = self.seq
        self.events.append(ev)

    def _session(self, path):
        name = os.path.basename(path)[:-len(".jsonl")]
        info = {"id": name, "title": "", "project": "", "cwd": "", "parent": None, "kind": "",
                "ctx": 0, "out": 0, "model": "", "last": 0, "_reqs": {}}
        if os.path.basename(os.path.dirname(path)) == "subagents":
            info["parent"] = os.path.basename(os.path.dirname(os.path.dirname(path)))
            try:
                meta = json.load(io.open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8"))
                info["title"] = meta.get("description") or ""
                info["kind"] = meta.get("agentType") or ""
                info["spawnedBy"] = meta.get("toolUseId")
            except (OSError, ValueError):
                pass
        return info

    def _find(self):
        now = time.time()
        if now - self.globbed < 5:
            return
        self.globbed = now
        found = glob.glob(os.path.join(self.root, "*", "*.jsonl"))
        found += glob.glob(os.path.join(self.root, "*", "*", "subagents", "*.jsonl"))
        # every other app, from what the rs3cache servers wrote down themselves
        from .poollog import POOL
        found += glob.glob(os.path.join(POOL, "*", "*.jsonl"))
        for path in found:
            if path in self.files:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime > ACTIVE_HOURS * 3600:
                continue
            info = self._session(path)
            self.sessions[info["id"]] = info
            self.files[path] = {"offset": max(0, st.st_size - BACKFILL), "sid": info["id"],
                                "partial": st.st_size > BACKFILL, "path": path}

    def _read(self, path, f):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size <= f["offset"]:
            return
        with open(path, "rb") as h:
            h.seek(f["offset"])
            chunk = h.read(min(size - f["offset"], BACKFILL))
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        at = f["offset"]
        f["offset"] += end + 1
        skip = f.pop("partial", False)
        for raw in chunk[:end].split(b"\n"):
            here = at
            at += len(raw) + 1
            if skip:
                skip = False
                continue
            try:
                self._line(json.loads(raw), f, here)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        f["live"] = True

    def _line(self, d, f, at=0):
        s = self.sessions[f["sid"]]
        kind = d.get("type")
        t = _ms(d.get("timestamp"))
        if t:
            s["last"] = max(s["last"], t)
        if d.get("cwd"):
            s["cwd"] = d["cwd"]
            if not s["project"]:
                s["project"] = os.path.basename(d["cwd"].rstrip("\\/"))
        if kind == "custom-title":
            s["title"] = d.get("customTitle") or s["title"]
            return
        msg = d.get("message") or {}
        content = msg.get("content")

        if kind == "assistant":
            usage = msg.get("usage") or {}
            if usage:
                s["ctx"] = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0))
                s["_reqs"][d.get("requestId") or d.get("uuid")] = usage.get("output_tokens", 0)
                s["out"] = sum(s["_reqs"].values())
                s["model"] = msg.get("model") or s["model"]
            for b in content if isinstance(content, list) else []:
                if b.get("type") != "tool_use":
                    continue
                name = b.get("name") or ""
                args = b.get("input") or {}
                where, act, detail, rs3 = self.place(name, args, d.get("cwd") or s["cwd"])
                ev = {"kind": "call", "sid": s["id"], "t": t, "id": b.get("id"), "tool": name,
                      "place": where, "act": act, "detail": detail}
                if rs3:
                    ev["rs3"] = rs3
                if name in ("Bash", "PowerShell") and not rs3 and self.advise \
                        and self.advise(str(args.get("command") or "")):
                    ev["bypass"] = True
                self.where[b.get("id")] = [f["path"], at, None]
                self._emit(ev)
                # only what happens while the page is open: the backfill is history, not a run
                if f.get("live") and act == "run" and self.on_test:
                    try:
                        self.on_test(where, s["title"] or s["project"] or s["id"][:8])
                    except (OSError, ValueError):
                        pass
            return

        if kind != "user" or d.get("isMeta"):
            return
        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            for b in content:
                if b.get("type") == "tool_result":
                    got = b.get("content")
                    ev = {"kind": "result", "sid": s["id"], "t": t, "of": b.get("tool_use_id"),
                          "err": bool(b.get("is_error")), "chars": _size(got), "peek": _peek(got)}
                    tests = _tests(_text_of(got))
                    if tests:
                        ev["tests"] = tests
                    self._emit(ev)
                    if b.get("tool_use_id") in self.where:
                        self.where[b["tool_use_id"]][2] = at
            return
        text = _text_of(content).strip()
        if text and not text.startswith(NOT_A_PROMPT) and not s["parent"]:
            self._emit({"kind": "prompt", "sid": s["id"], "t": t, "text": text[:600]})

    def poll(self, after=0):
        with self.lock:
            self._find()
            for path, f in list(self.files.items()):
                self._read(path, f)
            if len(self.events) > KEEP:
                del self.events[:len(self.events) - KEEP]
            new = [e for e in self.events if e["seq"] > after] if after else list(self.events)
            people = {k: {x: y for x, y in v.items() if not x.startswith("_")}
                      for k, v in self.sessions.items()}
            return {"events": new, "next": self.seq, "sessions": people,
                    "now": int(time.time() * 1000)}

    def call(self, call_id):
        """Everything about one call, read back from the transcript on demand.

        Kept out of the event stream: full inputs and answers for a day of calls is megabytes the
        page would hold to show one at a time.
        """
        with self.lock:
            spot = self.where.get(call_id)
        if not spot:
            return None
        path, at_call, at_result = spot
        out = {"id": call_id}

        def line(at):
            with open(path, "rb") as h:
                h.seek(at)
                return json.loads(h.readline())

        def clip(v, n):
            if isinstance(v, str):
                return v if len(v) <= n else v[:n] + "\n... %d more characters" % (len(v) - n)
            if isinstance(v, dict):
                return {k: clip(x, n) for k, x in v.items()}
            if isinstance(v, list):
                return [clip(x, n) for x in v[:50]]
            return v

        try:
            for b in line(at_call)["message"]["content"]:
                if b.get("type") == "tool_use" and b.get("id") == call_id:
                    out["tool"] = b.get("name")
                    out["input"] = clip(b.get("input") or {}, 12000)
            if at_result is not None:
                for b in line(at_result)["message"]["content"]:
                    if b.get("type") == "tool_result" and b.get("tool_use_id") == call_id:
                        out["result"] = clip(_text_of(b.get("content")), 8000)
                        out["err"] = bool(b.get("is_error"))
        except (OSError, ValueError, KeyError, TypeError):
            out["unreadable"] = True
        return out


def _handler(journal, tools, box=None):
    allowed = {"127.0.0.1:%d", "localhost:%d"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):
            # A page on some other site could rebind a hostname to 127.0.0.1 and read prompts.
            port = self.server.server_address[1]
            if self.headers.get("Host") not in {a % port for a in allowed}:
                return self._send(403, b"local only", "text/plain")
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                try:
                    body = io.open(PAGE, "rb").read()
                except OSError:
                    return self._send(500, b"monitor.html is missing", "text/plain")
                return self._send(200, body, "text/html; charset=utf-8")
            if url.path == "/meta":
                return self._json({"tools": tools, "home": HOME.replace("\\", "/"), "user": USER,
                                   "sandbox": box is not None})
            if url.path == "/events":
                return self._json(journal.poll(int((q.get("after") or ["0"])[0] or 0)))
            if url.path == "/call":
                got = journal.call((q.get("id") or [""])[0])
                if got is None:
                    return self._send(404, b"no such call", "text/plain")
                return self._json(got)
            num = lambda k: int((q.get(k) or ["0"])[0] or 0)
            if url.path == "/sandbox" and box:
                return self._json(box.snapshot(num("run") or None, num("after")))
            if url.path == "/sandbox/source" and box:
                got = box.source(num("run"), num("suite"), num("line"), start=num("from"))
                return self._json(got) if got else self._send(404, b"", "text/plain")
            self._send(404, b"", "text/plain")

        def do_POST(self):
            # Running code wants more than a local Host: a custom header, which a form on another
            # page cannot send and a cross-site fetch cannot send without a preflight nobody answers.
            port = self.server.server_address[1]
            if self.headers.get("Host") not in {a % port for a in allowed} or self.headers.get("X-Pool") != "1":
                return self._send(403, b"local only", "text/plain")
            if urlparse(self.path).path != "/sandbox/run" or not box:
                return self._send(404, b"", "text/plain")
            try:
                ask = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
                if ask.get("place"):
                    run = box.mirror(ask["place"], "a click on Claude's run")
                else:
                    runner = addons.first("pool_sandbox")
                    if ask.get("project") not in {p["name"] for p in runner.projects()}:
                        raise ValueError("no such suite")
                    run = box.start(os.path.join(runner.SOURCE, ask["project"]), ask.get("suite"))
                if run is None:
                    raise ValueError("that is not a test file")
                return self._json({"run": run})
            except (ValueError, TypeError) as e:
                return self._send(400, str(e).encode(), "text/plain")

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port and steal half the requests.
    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def serve(tools, advise=None, port=PORT, root=PROJECTS, retry=20):
    """Start the monitor on a daemon thread. Returns the thread, which keeps trying the port."""
    if (os.environ.get("RS3_MONITOR") or "").lower() in ("off", "0", "no"):
        return None
    sets = {t["name"]: t["set"] for t in tools}
    journal = Journal(sets, root, advise)
    # an add-on can run test suites the page watches; without one those routes answer 404
    runner = addons.first("pool_sandbox")
    box = runner.Sandbox() if runner else None
    if box:
        journal.on_test = box.mirror

    def run():
        while True:
            try:
                httpd = _Server(("127.0.0.1", port), _handler(journal, tools, box))
            except OSError:
                if not retry:
                    return
                time.sleep(retry)
                continue
            run.httpd = httpd
            httpd.serve_forever()

    run.httpd = None
    th = threading.Thread(target=run, name="scrying-pool", daemon=True)
    th.journal = journal
    th.sandbox = box
    th.run_ref = run
    th.start()
    return th
