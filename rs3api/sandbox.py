"""The lua sandbox in motion: test suites run with every check reported as it happens.

luatrace.lua reports each check a suite makes. The suite's own printed total is kept beside the
traced count, so a check the tracer misses shows as a disagreement.

A session running test/<suite>.lua starts the same suite here too.
"""

import glob
import io
import os
import re
import subprocess
import threading
import time

from rs3api.paths import PLUGINS, PROJECT

HOME = os.path.expanduser("~")
# plugin sources with their tests, three places tried
SOURCE = (os.environ.get("RS3MCP_SUITES")
          or (os.path.join(PROJECT, "plugins") if os.path.isdir(os.path.join(PROJECT, "plugins")) else PLUGINS))
TRACER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luatrace.lua")
# every way a suite here reports, with meanings
SUMMARIES = [
    (re.compile(r"(\d+) passed, (\d+) failed"), "both"),
    (re.compile(r"pass (\d+)\s+fail (\d+)"), "both"),
    (re.compile(r"(\d+) (?:rule|shape|check)\(?s?\)? held, (\d+) broke"), "both"),
    (re.compile(r"all (\d+) \w+ render"), "passed"),
    (re.compile(r"(\d+) \w+ broken"), "failed"),
]


def summarise(text):
    """(passed, failed) as the suite itself reported them, or None when nothing said."""
    passed, failed, said = 0, 0, False
    for pattern, means in SUMMARIES:
        for hit in pattern.findall(text):
            said = True
            if means == "both":
                passed += int(hit[0])
                failed += int(hit[1])
            elif means == "passed":
                passed += int(hit)
            else:
                failed += int(hit)
    return (passed, failed) if said else None
TEST_FILE = re.compile(r"^(.+)/test/([\w.-]+)\.lua$")
KEEP_RUNS = 12
SUITE_SECONDS = 180


def _now():
    return int(time.time() * 1000)


def suites(root):
    """A plugin's suites: the list test/all.lua runs when there is one, every test file when not."""
    listed = os.path.join(root, "test", "all.lua")
    if os.path.exists(listed):
        body = re.search(r"local suites\s*=\s*\{(.*?)\n\}", io.open(listed, encoding="utf-8").read(), re.S)
        if body:
            return re.findall(r"\"([\w.-]+)\"", body.group(1))
    return [os.path.basename(f)[:-4] for f in sorted(glob.glob(os.path.join(root, "test", "*.lua")))]


def projects():
    out = []
    for root in sorted(glob.glob(os.path.join(SOURCE, "*"))):
        names = suites(root) if os.path.isdir(os.path.join(root, "test")) else []
        if names:
            out.append({"name": os.path.basename(root), "suites": len(names)})
    return out


class Sandbox:
    def __init__(self):
        self.lock = threading.Lock()
        self.runs = []
        self.seq = 0
        self.active = None
        self.queued = None

    def start(self, root, only=None, reason="run by hand"):
        """Begin a run, or hand back the one already going. Returns the run id."""
        root = os.path.normpath(root)
        names = suites(root)
        if only:
            names = [n for n in names if n == only] or ([only] if os.path.exists(
                os.path.join(root, "test", only + ".lua")) else [])
        if not names:
            raise ValueError("no lua test suites under %s" % root)
        with self.lock:
            if self.active:
                self.queued = (root, only, reason)
                return self.active["id"]
            run = {"id": len(self.runs) and self.runs[-1]["id"] + 1 or 1,
                   "project": os.path.basename(root), "root": root.replace("\\", "/"),
                   "reason": reason, "started": _now(), "ended": None, "events": [],
                   "suites": [{"name": n, "file": "test/%s.lua" % n, "state": "waiting", "checks": 0,
                               "failed": 0, "harness": None, "exit": None, "ms": None} for n in names]}
            self.runs.append(run)
            del self.runs[:-KEEP_RUNS]
            self.active = run
        threading.Thread(target=self._run, args=(run,), name="sandbox-run", daemon=True).start()
        return run["id"]

    def mirror(self, place, who):
        """A call that ran test/<suite>.lua starts the same suite here."""
        m = TEST_FILE.match(place or "")
        if not m:
            return None
        root = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(HOME, m.group(1))
        if not os.path.isdir(os.path.join(root, "test")):
            return None
        only = None if m.group(2) == "all" else m.group(2)
        return self.start(root, only, "ran along with %s" % who)

    def _emit(self, run, ev):
        with self.lock:
            self.seq += 1
            ev["seq"] = self.seq
            run["events"].append(ev)

    def _run(self, run):
        try:
            for i in range(len(run["suites"])):
                self._suite(run, i)
        finally:
            run["ended"] = _now()
            self._emit(run, {"k": "done"})
            with self.lock:
                self.active = None
                queued, self.queued = self.queued, None
            if queued:
                self.start(*queued)

    def _suite(self, run, i):
        suite = run["suites"][i]
        suite["state"] = "running"
        self._emit(run, {"k": "suite", "i": i, "state": "running"})
        began = time.time()
        src = []
        try:
            src = io.open(os.path.join(run["root"], suite["file"]), encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            pass
        try:
            p = subprocess.Popen(["lua", TRACER, suite["file"]], cwd=run["root"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                 encoding="utf-8", errors="replace")
        except OSError as e:
            suite.update(state="crashed", exit=-1, ms=0, error="lua would not start: %s" % e)
            self._emit(run, {"k": "end", "i": i, **suite})
            return

        plain = []
        reader = threading.Thread(target=lambda: plain.extend(p.stdout), daemon=True)
        reader.start()
        killer = threading.Timer(SUITE_SECONDS, p.kill)
        killer.start()

        held, said = None, []

        def let_go():
            nonlocal held
            if held:
                if held["ok"] == 2:
                    held["ok"] = 1
                suite["checks"] += 1
                suite["failed"] += held["ok"] == 0
                self._emit(run, held)
                held = None

        for raw in p.stderr:
            line = raw.rstrip("\r\n")
            if line.startswith("\x01CHECK\t"):
                let_go()
                parts = (line.split("\t", 6) + [""] * 7)[:7]
                ln = int(parts[1]) if parts[1].isdigit() else 0
                outer = int(parts[2]) if parts[2].isdigit() else 0
                held = {"k": "check", "i": i, "line": ln, "ok": int(parts[3] or 0), "label": parts[4],
                        "got": parts[5][:300], "want": parts[6][:300],
                        "src": src[ln - 1].strip()[:200] if 0 < ln <= len(src) else ""}
                if outer and outer != ln:
                    held["outer"] = outer
            elif line.startswith("\x01OUT\t"):
                text = line[5:]
                said.append(text)
                if held and text.startswith("FAIL"):
                    if held["ok"] == 1:
                        held["disagree"] = True
                    held["ok"] = 0
                let_go()
                self._emit(run, {"k": "out", "i": i, "text": text[:400]})
            elif line.strip():
                let_go()
                said.append(line)
                self._emit(run, {"k": "out", "i": i, "text": line[:400], "err": True})
        let_go()
        code = p.wait()
        killer.cancel()
        reader.join(2)
        for line in plain:
            if line.strip():
                said.append(line.rstrip())
                self._emit(run, {"k": "out", "i": i, "text": line.rstrip()[:400]})

        found = summarise("\n".join(said))
        if found:
            suite["harness"] = list(found)
        suite["exit"] = code
        suite["ms"] = int((time.time() - began) * 1000)
        suite["state"] = "passed" if code == 0 and not suite["failed"] else "failed"
        # a suite nothing counted is uncounted, not passed
        if suite["state"] == "passed" and not suite["checks"] and not suite.get("harness"):
            suite["uncounted"] = True
        self._emit(run, {"k": "end", "i": i, **{k: v for k, v in suite.items() if k != "file"}})

    def snapshot(self, run_id=None, after=0):
        with self.lock:
            runs = list(self.runs)
            active = self.active["id"] if self.active else None
        pick = next((r for r in runs if r["id"] == run_id), runs[-1] if runs else None)
        summary = [{"id": r["id"], "project": r["project"], "reason": r["reason"], "started": r["started"],
                    "ended": r["ended"], "checks": sum(s["checks"] for s in r["suites"]),
                    "failed": sum(s["failed"] for s in r["suites"]), "suites": len(r["suites"])}
                   for r in reversed(runs)]
        out = {"projects": projects(), "runs": summary, "active": active}
        if pick:
            out["run"] = {k: v for k, v in pick.items() if k != "events"}
            out["run"]["events"] = [e for e in pick["events"] if e["seq"] > after]
        return out

    def source(self, run_id, suite, line, around=5, start=0):
        """Lines around one check, from the suite file of a run. Never a path from the request.

        With start, the lines from there down to the check instead: what the test set up since its
        previous check, which is the mock state this one is judging.
        """
        run = next((r for r in self.runs if r["id"] == run_id), None)
        if not run or not (0 <= suite < len(run["suites"])):
            return None
        path = os.path.join(run["root"], run["suites"][suite]["file"])
        try:
            lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            return None
        if 0 < start <= line and line - start <= 60:
            lo, hi = start, line
        else:
            lo, hi = max(1, line - around), line + around
        return {"file": run["suites"][suite]["file"], "start": lo, "lines": lines[lo - 1:hi]}
