"""Check an agent's handback against its own transcript before it is allowed to stop.

Every agent ends with a block like this, and the transcript has to back it up:

    ## Handback
    status: done                    done, partial or blocked
    file: rs3mcp/wiki.py            each file written or edited
    ran: python rs3-mcp.py --test   each check it ran, which must have run and passed
    claim: the cert retry works, rs3mcp/wiki.py:95
    question: should missing pages be retried sooner?

The agent is sent back when a check it lists never ran or failed, a file is missing or does not
parse, an edit is left off the list, a claim points nowhere, or it says tests pass with nothing
under ran. So "done" means checked, and nobody has to check it again.

    python -m rs3mcp.handback --hook              as a SubagentStop hook, or PreToolUse on SubagentHandback
    python -m rs3mcp.handback <transcript.jsonl>  check one finished agent by hand
"""

import io
import json
import os
import re
import sys

# found is the debugger's status, rules still apply
STATUSES = ("done", "partial", "blocked", "found")
TEMPLATE = ("## Handback\nstatus: done | partial | blocked\nfile: <each file you wrote or edited>\n"
            "ran: <each check you ran, exactly as run>\nclaim: <what you found>, <file:line or command>\n"
            "question: <anything you could not settle>\n"
            "learned: <optional: what the next agent should know>, <file:line or `a command you ran`>")
SAYS_CHECKED = re.compile(r"\b(verified|build (?:is )?(?:clean|green|passes|passed)|compiles( cleanly)?|"
                          r"tests? pass(?:ed|es)?|all green|exit 0)\b", re.I)
SHELLS = ("Bash", "PowerShell")
MCP_MARK = "mcp: "
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# bounces allowed before an agent is let go
MAX_BLOCKS = 3
HERE = os.path.dirname(os.path.abspath(__file__))
# where Claude Code keeps every session
PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
# run as a file, so the package resolves
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))


def parse(text):
    """The handback block as {status, files, ran, claims, questions}, or None when there is none."""
    m = re.search(r"^#{1,4}\s*handback\s*$(.*)", text or "", re.I | re.M | re.S)
    if not m:
        return None
    out = {"status": None, "files": [], "ran": [], "claims": [], "questions": [], "learned": [], "findings": [],
           "fetched": [], "reviewed": [], "unkeyed": []}
    keys = {"status": "status", "file": "files", "ran": "ran", "claim": "claims", "question": "questions",
            "learned": "learned", "finding": "findings", "fetched": "fetched", "reviewed": "reviewed"}
    for line in m.group(1).splitlines():
        k = re.match(r"\s*[-*]?\s*(status|file|ran|claim|question|learned|finding|fetched|reviewed)s?\s*:\s*(.*)$",
                     line, re.I)
        if not k:
            if line.strip() and not line.strip().startswith("```"):
                out["unkeyed"].append(line.strip())
            continue
        key, value = keys[k.group(1).lower()], k.group(2).strip()
        # learned keeps backticks round its command
        if key != "learned":
            value = value.strip("`").strip()
        elif value.startswith("<") or NOT_A_COMMAND.match(value):
            continue
        if not value:
            continue
        if key == "status":
            out["status"] = value.lower().split()[0]
        else:
            out[key].append(value)
    return out


def activity(transcript):
    """What the agent actually did: shell commands with whether they failed, and files it edited."""
    runs, edited, pending, writes = [], [], {}, {}
    try:
        fh = io.open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return runs, edited
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for b in (row.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") in SHELLS:
                        pending[b.get("id")] = str(inp.get("command") or "")
                    # a check run through MCP is still run
                    elif str(b.get("name") or "").startswith("mcp__"):
                        pending[b.get("id")] = MCP_MARK + b["name"].split("__")[-1] + " " + " ".join(
                            "%s=%s" % (k, v) for k, v in inp.items())
                    elif b.get("name") in EDITS and inp.get("file_path"):
                        writes[b.get("id")] = str(inp["file_path"])
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    content = b.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    runs.append((pending.pop(b["tool_use_id"]), bool(b.get("is_error")), text[:300]))
                # an edit counts once it went through
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in writes:
                    path = writes.pop(b["tool_use_id"])
                    if not b.get("is_error"):
                        edited.append(path)
    # a write with no result yet still counts
    edited.extend(writes.values())
    return runs, edited


def _squash(s):
    return re.sub(r"\s+", " ", s.replace("\\", "/")).strip().lower()


# the result beside the command, not another one
ANNOTATION = re.compile(r"\s\([^()]*\)\s*$")
# a command carries a flag, path or redirect
COMMANDISH = re.compile(r"(?:^|\s)(?:-{1,2}\w|[\w.]+[/\\]|[\w-]+\.\w{1,4}\b|[|<>])|^\s*`?(?:mcp__|rs3_)\w+")
NOT_A_COMMAND = re.compile(r"^\s*(none|n/?a|nothing|not\b|no\b)", re.I)


def _ran_match(want, runs):
    """Whether this listed check is one of the commands the transcript shows.

    Matched on the command alone, with any trailing note dropped, and an elision (`a ... b`) treated
    as its parts in order: an agent that shortens a long one-liner is not claiming a different one.
    """
    want = ANNOTATION.sub("", want.strip())
    w = _squash(want)
    if not w:
        return []
    parts = [p for p in (_squash(x) for x in want.split("...")) if p]
    # the tool's name says which tool ran
    tool = re.sub(r"^mcp__\w+?__", "", w.split(" ")[0].strip("`'\""))
    out = []
    for r in runs:
        if r[0].startswith(MCP_MARK):
            if r[0][len(MCP_MARK):].split(" ")[0] == tool:
                out.append(r)
            continue
        c = _squash(r[0])
        if w in c or c in w:
            out.append(r)
            continue
        if len(parts) > 1:
            at, ok = 0, True
            for p in parts:
                found = c.find(p, at)
                if found < 0:
                    ok = False
                    break
                at = found + len(p)
            if ok:
                out.append(r)
    return out


def _roots(transcript, cwd):
    """Where a relative path in a handback can honestly be from: the session's cwd, and the folders
    the agent actually worked in. An agent that reads Quest_Helper while the shell sits in rs3cache
    writes Quest_Helper paths, and resolving those against the cwd alone called 54 real files
    missing."""
    out = []
    for c in (cwd,):
        if c:
            out.append(c)
    try:
        edited = activity(transcript)[1] if transcript else []
    except Exception:
        edited = []
    for f in edited:
        here = os.path.dirname(f)
        while here and len(here) > 3 and here not in out:
            out.append(here)
            here = os.path.dirname(here)
    # the brief's scope folders, not only the cwd
    try:
        out += [b for b in bases(_brief_text(transcript)) if b not in out] if transcript else []
    except Exception:
        pass
    return out


SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv", "out", "target"}


def _unique_named(name, roots, budget=20000):
    """The one file called `name` under a project the agent worked in, or None.

    A claim naming controller.lua and a line sent a correct agent back, since a bare name was never
    searched for. It is
    only unclear when two files share the name, so a single match is taken and two are not.
    """
    # nearest project first, widening only when empty
    done = set()
    for base in sorted(roots, key=lambda r: -len(os.path.normpath(r))):
        key = os.path.normcase(os.path.normpath(base))
        if key in done or not (os.path.exists(os.path.join(base, "plugin.json"))
                               or os.path.isdir(os.path.join(base, ".git"))):
            continue
        done.add(key)
        found, seen = set(), 0
        for here, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".mutate-")]
            seen += len(names)
            if name in names:
                found.add(os.path.normcase(os.path.join(here, name)))
            if seen > budget or len(found) > 1:
                break
        if len(found) == 1:
            return found.pop()
        if found:
            return None
    return None


def _under(base, tail, budget=20000):
    """A file whose path ends with `tail`, somewhere under base. `src/render/native_hints.lua` is a
    real file inside a plugin folder, and resolving it only against the roots called it missing."""
    tail = tail.replace("\\", "/").lower()
    want = os.path.basename(tail)
    seen = 0
    for here, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        seen += len(names)
        if want in names or want in [n.lower() for n in names]:
            for n in names:
                full = os.path.join(here, n)
                if full.replace("\\", "/").lower().endswith(tail):
                    return full
        if seen > budget:
            return None
    return None


def _norm_path(p, cwd, roots=()):
    # lua prints ./src/copy.lua, so the dot stays
    p = re.sub(r"^\.[/\\]", "", p.strip().strip("`'\""))
    p = os.path.expanduser(p.strip("(),.;"))
    p = re.sub(r"^/([a-zA-Z])/", r"\1:/", p)
    if os.path.isabs(p):
        return os.path.normcase(os.path.normpath(p))
    bases = ([cwd] if cwd else []) + list(roots)
    for base in bases:
        here = os.path.normcase(os.path.normpath(os.path.join(base, p)))
        if os.path.exists(here):
            return here
    # a path with a folder can sit deeper
    if "/" in p.replace("\\", "/"):
        for base in bases:
            if os.path.isdir(base):
                found = _under(base, p)
                if found:
                    return os.path.normcase(os.path.normpath(found))
    return os.path.normcase(os.path.normpath(os.path.join(cwd or "", p)))


def _file_checks():
    """{extension: fn(path) -> problem or None}. Python and json here; an add-on offers more."""
    def py(path):
        # compile() rather than py_compile, leaving no pyc
        try:
            compile(io.open(path, encoding="utf-8-sig").read(), path, "exec")
        except SyntaxError as e:
            return "line %s: %s" % (e.lineno, e.msg)

    def js(path):
        try:
            json.load(io.open(path, encoding="utf-8-sig"))
        except ValueError as e:
            return "not valid json: %s" % e

    checks = {".py": py, ".json": js}
    try:
        from rs3mcp import addons
        for offered in addons.every("file_checks"):
            checks.update(offered)
    except Exception:
        pass
    return checks


def _pointers(text, cwd, roots, what):
    """(problems, how many file:line pointers held) for every file:line in one line of a handback."""
    out, held = [], 0
    # a folder can have a space in it
    for path, line in re.findall(r"([\w.][\w./\\:+-]*(?:[ ][\w.][\w./\\:+-]*)*\.\w+):(\d+)", text):
        # the shortest dropped prefix that exists wins
        def resolve(candidate):
            q = _norm_path(candidate, cwd, roots)
            if not os.path.exists(q) and "/" not in candidate.replace("\\", "/"):
                q = _unique_named(candidate, roots) or q
            return q
        words = path.split(" ")
        for k in range(len(words)):
            tail = " ".join(words[k:])
            if os.path.exists(resolve(tail)):
                path = tail
                break
        p = resolve(path)
        if not os.path.exists(p):
            bare = "/" not in path.replace("\\", "/")
            out.append("%s points at %s, which %s" % (
                what, path, "is more than one file or none here: give its path" if bare else "does not exist"))
            continue
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            n = sum(1 for _ in fh)
        if int(line) > n:
            out.append("%s points at %s:%s, but the file has %d lines" % (what, path, line, n))
        else:
            held += 1
    return out, held


# words that carry no claim
STOP = set("that this with from when then than they them their into have been were will would could should "
           "about after before where which while there these those what your only just also does done more "
           "most some such each other over under same instead store stored values value".split())
# overlap that counts as a repeat
ECHO = 0.5
# fewest shared words for a repeat
ECHO_WORDS = 3


def _words(text):
    return {_stem(w) for w in re.findall(r"[a-z][a-z0-9_]{3,}", text.lower()) if w not in STOP}


def _stem(w):
    """reading, reads and read are one word."""
    if len(w) > 6 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _share(mine, text):
    shared = len(mine & _words(text))
    return shared / float(len(mine)) if shared >= ECHO_WORDS else 0.0


def _repeats(learned, brief):
    """Why a learned: line is not new: it repeats the hand-over, or a lesson on file. None when new."""
    # the evidence after the last comma
    mine = _words(learned.rsplit(",", 1)[0] if "," in learned else learned)
    handed = brief.split(HANDOVER, 1)[1] if HANDOVER in brief else ""
    for line in handed.split("\n"):
        if line.strip().startswith("- ") and _share(mine, line) >= ECHO:
            return "repeats what you were handed (%s)" % line.strip()[2:70]
    from rs3mcp import addons
    known = addons.first("known_lessons")
    for ident, text in (known() if known else []):
        if _share(mine, text) >= ECHO:
            return "is already a lesson, %s" % ident
    return None


def readable(transcript):
    """Whether there is a transcript to judge against at all."""
    return bool(transcript) and os.path.exists(transcript) and os.path.getsize(transcript) > 0


def problems(message, transcript, cwd=None):
    """Everything wrong with this handback, as sentences the agent can act on. Empty when it holds.

    With no transcript there is no evidence, so there is no verdict: not about the checks, and not
    about the block either. Of the first 401 logged handbacks, every one was judged without a
    transcript, and 143 of them were told off for having no block while the checker could not see
    whether they were an agent at all.
    """
    hb = parse(message)
    if hb is None:
        return [] if not readable(transcript) else ["End with a handback block, exactly this shape:\n" + TEMPLATE]
    out = []
    if hb["status"] not in STATUSES:
        out.append("status must be one of done, partial or blocked")
    runs, edited = activity(transcript)
    # a transcript showing nothing done is no evidence
    unreadable = not readable(transcript) or (not runs and not edited)

    for want in hb["ran"]:
        if unreadable:
            break
        hits = _ran_match(want, runs)
        if not hits and (NOT_A_COMMAND.match(want) or not COMMANDISH.search(want)):
            out.append("`%s` is not a command. Put it under claim, or say partial and put it under "
                       "question" % want.strip()[:80])
        elif not hits:
            out.append("you list `%s` under ran, but it was never run. Run it, or take it out" % want)
        elif hits[-1][1] and hb["status"] != "found":
            first = hits[-1][2].strip().splitlines()[0] if hits[-1][2].strip() else "it failed"
            # only found is let through, not partial
            out.append("`%s` failed the last time you ran it (%s). Fix it and run it again, or move it "
                       "from ran: to question: and say partial" % (want, first[:160]))

    checks = _file_checks()
    roots = _roots(transcript, cwd)
    listed = set()
    files = []
    for f in hb["files"]:
        # file: a.lua, b.lua is two files
        parts = [p.strip() for p in f.split(", ") if p.strip()]
        whole = _norm_path(re.split(r"\s+[-(]", f)[0], cwd, roots)
        files += parts if len(parts) > 1 and not os.path.exists(whole) else [f]
    for f in files:
        # file: none means nothing was written
        if NOT_A_COMMAND.match(f.strip().lstrip("([\"'")):
            continue
        path = _norm_path(re.split(r"\s+[-(]", f)[0], cwd, roots)
        listed.add(path)
        if not os.path.exists(path):
            # a bare name can be several files
            bare = "/" not in f.replace("\\", "/")
            out.append("file %s does not exist. Give the path from the project root, not the file "
                       "name on its own" % f if bare else "file %s does not exist" % f)
            continue
        check = checks.get(os.path.splitext(path)[1].lower())
        problem = check(path) if check else None
        if problem:
            out.append("%s: %s" % (f, problem))
    for f in sorted(set(edited)):
        if _norm_path(f, cwd, roots) not in listed and os.path.exists(f):
            out.append("you edited %s but did not list it under file" % f)

    for claim in hb["claims"]:
        out += _pointers(claim, cwd, roots, "claim")[0]

    brief = _brief_text(transcript) if hb["learned"] and not unreadable else ""
    for learned in hb["learned"]:
        if unreadable:
            break
        again = _repeats(learned, brief)
        if again:
            out.append("learned: `%s` %s. Leave it out, or say what you found that it does not"
                       % (learned.strip()[:80], again))
            continue
        bad, points = _pointers(learned, cwd, roots, "learned")
        ran = [c for c in re.findall(r"`([^`]+)`", learned) if _ran_match(c, runs)]
        out += bad
        if not points and not ran:
            out.append("learned: `%s` needs what shows it: a file:line, or a command you ran in backticks"
                       % learned.strip()[:80])

    # found needs a cause and a failing check
    if hb["status"] == "found" and not unreadable:
        if not hb["ran"]:
            out.append("status found needs the check under ran, so it is on record as having run")
        if not hb["files"]:
            out.append("status found needs the check you added under file")
        if not hb["claims"]:
            out.append("status found needs the cause under claim, with a file:line")

    prose = message[:message.lower().rfind("handback")] if "handback" in message.lower() else message
    if hb["status"] == "done" and not hb["ran"] and SAYS_CHECKED.search(prose):
        out.append("you say it was checked (\"%s\") but list nothing under ran. Run the check and list "
                   "it, or drop the claim" % SAYS_CHECKED.search(prose).group(0))
    return out


# second-opinion limits
CLAIMS_MAX = 8
EVIDENCE_MAX = 80
EVIDENCE_CHARS = 1500
NEAREST = 3


# what an agent says about running something
SAYS_RAN = re.compile(r"\b(verified|compiles?|compiled|pass(?:ed|es)?|fail(?:ed|s)?|printed|broke|runs?|ran|"
                      r"exit(?:ed)? \d|ok\b|green|works?|succeed(?:ed|s)?)", re.I)
# tools whose output shows something ran
RUNNERS = SHELLS


def _evidence(transcript):
    """What running things printed, each with the command, newest last. File reads and edits are
    left out: the NLI model reads English, and scored claims about code it had read at 0.03."""
    chunks, pending = [], {}
    try:
        fh = io.open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return chunks
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for b in (row.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name, inp = str(b.get("name") or ""), b.get("input") or {}
                    if name in RUNNERS or name.startswith("mcp__"):
                        what = inp.get("command") or json.dumps(inp)[:200]
                        pending[b.get("id")] = "%s %s" % (name, what)
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    content = b.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    # results print last
                    chunks.append("%s\n%s" % (pending.pop(b["tool_use_id"]), text[-EVIDENCE_CHARS:]))
    return chunks[-EVIDENCE_MAX:]


def claims_of(message):
    """What the handback says about running things: claim lines and sentences saying something ran,
    passed or was verified. Claims about what code does are left to the rules."""
    hb = parse(message) or {}
    prose = message[:message.lower().rfind("handback")] if "handback" in message.lower() else message
    out = [c for c in hb.get("claims") or [] if SAYS_RAN.search(c)]
    out += [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", prose) if SAYS_CHECKED.search(s)]
    return [c for c in dict.fromkeys(out) if c][:CLAIMS_MAX]


def second_opinion(message, transcript):
    """Each claim of a run with how well what actually ran supports it, by the local NLI model.
    None when there is nothing to weigh or the models are not installed. Advice only: it never blocks."""
    import time
    claims, evidence = claims_of(message), _evidence(transcript)
    if not claims:
        return None
    if not evidence:
        # nothing ran at all
        return {"claims": [{"claim": c[:240], "support": 0.0, "from": "nothing was run"} for c in claims], "seconds": 0}
    from rs3mcp import judge
    model = judge.nli()
    if model is None:
        return None
    started = time.time()
    try:
        vecs = judge.embedder().embed(evidence)
    except ValueError:
        return None
    out = []
    for claim in claims:
        near = list((vecs @ judge.embedder().embed([claim])[0]).argsort()[::-1][:NEAREST])
        # outputs of commands the claim names come first
        named = [i for i, e in enumerate(evidence) if any(len(w) > 4 and w in e.split("\n", 1)[0]
                                                          for w in re.findall(r"[\w./-]+", claim))]
        near = list(dict.fromkeys(named[-NEAREST:] + near))[:NEAREST * 2]
        scores = [(model.entail(evidence[i], [claim])[0], i) for i in near]
        best, i = max(scores)
        out.append({"claim": claim[:240], "support": round(best, 3), "from": evidence[i].split("\n", 1)[0][:160]})
    return {"claims": out, "seconds": round(time.time() - started, 2)}


def kind(row):
    """What a logged row actually is, since three different things share the file.

    Worked out 2026-09-21 over all 454 rows. The surviving sessions launched 209 agents and three
    of them are in this log; the 175 agents it does name were launched by no session left on disk.
    So most rows are not a judgement of a briefed agent at all, and a rate over the whole file
    measured the gate's blindness, not the agents.
    """
    if row.get("evidence"):
        return "judged"
    if not row.get("status"):
        # an agent stop that was never a handback
        return "not a handback"
    if "evidence" not in row:
        # a real block, judged with no transcript
        return "judged blind"
    return "no transcript"


# what the brief carried when the agent started
BRIEF_FEATURES = [
    ("tools", "the api is readable, so read it"),
    ("checksLast", "do not hand back done on one that failed"),
    ("ranIsAcheck", "not for reading a file"),
    ("enforced", "already enforced by a tool that runs"),
    ("measured", "what the client was measured doing here"),
    ("resume", "redo only what failed or was never run"),
]


def brief_features(transcript):
    """Which of those the agent's brief had, or None when there is no brief to read."""
    if not transcript:
        return None
    text = _brief_text(transcript) or ""
    if not text:
        return None
    return [name for name, mark in BRIEF_FEATURES if mark in text]


def gate_log():
    from rs3mcp.common import ROOT
    return os.environ.get("RS3_GATE_LOG") or os.path.join(ROOT, "agents", "handbacks.jsonl")


def _log(entry):
    # undated rows could not be placed in time
    import time
    entry = dict(entry, when=time.strftime("%Y-%m-%dT%H:%M:%S"))
    path = gate_log()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def learned_path():
    from rs3mcp.common import ROOT
    return os.environ.get("RS3_LEARNED") or os.path.join(ROOT, "agents", "learned.jsonl")


def _learned(text, agent, event):
    """One learned: line from a handback that held, kept as a candidate lesson."""
    import time
    words = re.findall(r"[a-z0-9]+", text.split(",")[0].lower())[:6]
    row = {"id": "-".join(words) or "learned", "text": text, "status": "candidate", "agent": agent,
           "type": event.get("agent_type"), "session": event.get("session_id"),
           "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    path = learned_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _blocks_so_far(agent_id):
    path = gate_log()
    n = 0
    try:
        for line in io.open(path, encoding="utf-8"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("agent") == agent_id and e.get("verdict") == "sent back":
                n += 1
    except OSError:
        pass
    return n


def _agent_transcript(event):
    """The subagent's own transcript.

    Given outright on SubagentStop in some versions. Otherwise it is `agent-<id>.jsonl` under a
    session's `subagents` folder, which is not always this session's: every one of the first 401
    logged handbacks was checked against a path that did not exist, so the checker saw nothing and
    read that as the agent having done nothing.
    """
    if event.get("agent_transcript_path"):
        return event["agent_transcript_path"]
    main, agent = event.get("transcript_path") or "", event.get("agent_id")
    if not agent:
        return main
    base = main[:-6] if main.endswith(".jsonl") else main
    here = os.path.join(base, "subagents", "agent-%s.jsonl" % agent)
    if os.path.exists(here):
        return here
    import glob as _glob
    # both roots searched at their own depth
    name = "agent-%s.jsonl" % agent
    tries = []
    if main:
        tries.append(os.path.join(os.path.dirname(main), "*", "subagents", "**", name))
    tries.append(os.path.join(PROJECTS, "*", "*", "subagents", "**", name))
    for pattern in tries:
        found = _glob.glob(pattern, recursive=True)
        if found:
            return max(found, key=os.path.getmtime)
    return here


def _handed_back(transcript):
    """The report an agent already delivered through SubagentHandback, if it used it."""
    found = None
    try:
        for line in io.open(transcript, encoding="utf-8", errors="replace"):
            if "SubagentHandback" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for b in (row.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                    found = str((b.get("input") or {}).get("message") or "")
    except OSError:
        pass
    return found


def hook(event):
    """The decision for one event: None to let it through, or a block.

    SubagentStop carries the agent's closing message. In auto mode an agent reports through the
    SubagentHandback tool instead, so a PreToolUse on that tool checks the report as it is handed
    over, and the stop that follows lets it go."""
    transcript = _agent_transcript(event)
    agent = event.get("agent_id") or transcript
    pre = event.get("hook_event_name") == "PreToolUse"
    if pre:
        message = str((event.get("tool_input") or {}).get("message") or "")
    else:
        if _handed_back(transcript) is not None:
            return None
        message = event.get("last_assistant_message") or ""
    if not message.strip():
        # nothing to judge, and blocking would only loop
        return None
    found = problems(message, transcript, event.get("cwd"))
    hb = parse(message) or {}
    if event.get("agent_type") in ("researcher", "claims-checker") and hb:
        from rs3mcp import research
        found = found + research.gate(hb)
    # what the verdict rests on, for the rate
    entry = {"agent": agent, "type": event.get("agent_type"), "status": hb.get("status"),
             "event": event.get("hook_event_name"), "evidence": readable(transcript),
             "block": hb.get("status") is not None, "problems": found,
             "briefHas": brief_features(transcript), "briefHead": head(_brief_text(transcript) if transcript else "")}
    if readable(transcript):
        # logged, never part of the verdict
        entry["secondOpinion"] = second_opinion(message, transcript)
    if not found:
        if not readable(transcript):
            # a blind row keeps what the event carried
            entry["blind"] = {"keys": sorted(event), "transcript": transcript or None,
                              "said": " ".join(message.split())[:160]}
        _log(dict(entry, verdict="checked" if readable(transcript) else "no evidence to check"))
        if readable(transcript):
            for text in hb.get("learned") or []:
                _learned(text, agent, event)
        return None
    if _blocks_so_far(agent) >= MAX_BLOCKS:
        _log(dict(entry, verdict="let go unchecked"))
        return None
    _log(dict(entry, verdict="sent back"))
    reason = "Your handback does not hold yet:\n- " + "\n- ".join(found)
    if pre:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    return {"decision": "block", "reason": reason}


def _looks_like_a_path(s):
    """A separator, or a name with an extension. `scope:` is often followed by a sentence."""
    return "/" in s or "\\" in s or bool(re.match(r"^[\w.-]+\.\w+$", s))


def folders(text):
    """The folders a brief allows, from its `scope:` lines.

    They come two ways. `scope: plugins/drop-log` says it outright, and rs3_brief writes a sentence
    on that line with the folders indented under it. Reading only the first shape took the sentence
    as the folder, matched nothing, and refused every edit an agent tried, including the one it was
    sent to make.
    """
    out, under = [], False
    for line in (text or "").splitlines():
        head = re.match(r"\s*scope:\s*(.*)$", line, re.I)
        if head:
            rest = head.group(1).strip().strip("`'\"")
            # a sentence can hold a path with spaces
            if " " not in rest or re.match(r"^([a-zA-Z]:[\\/]|~|/)", rest):
                if rest and _looks_like_a_path(rest):
                    out.append(rest)
            else:
                # first sentence only, the next says otherwise
                first = re.split(r"\.\s", rest + " ", 1)[0]
                out += [w.strip("`'\"(),.;:") for w in first.split()
                        if _looks_like_a_path(w.strip("`'\"(),.;:"))]
            under = True
            continue
        if under:
            # indented lines are the folders, flush left ends
            if line.strip() and line[:1] in (" ", "\t"):
                one = line.strip().strip("`'\",")
                if _looks_like_a_path(one):
                    out.append(one)
                continue
            if line.strip():
                under = False
    return out


# where a hand-over appended to a brief begins
HANDOVER = "What is already known about this work, matched to your brief."


def head(text):
    """What joins a launch to the agent it started: a hash of the whole brief, then its opening.

    The opening alone was not enough: every run of one breeding task shares its first 120
    characters, the folder named further down being all that differs.
    """
    import hashlib
    body = " ".join((text or "").split(HANDOVER)[0].split())
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12] + " " + body[:80]


def _brief_text(transcript):
    """The first user message of an agent's transcript, which is its brief."""
    try:
        for line in io.open(transcript, encoding="utf-8", errors="replace"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "user":
                continue
            content = (row.get("message") or {}).get("content")
            return content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
    except OSError:
        pass
    return ""


def scopes(transcript):
    """The folders an agent's brief allows it to edit. Empty means anywhere."""
    return folders(_brief_text(transcript))


def bases(text):
    """Absolute folders a brief names anywhere, for a relative scope to be read against.

    `scope: core/controller.lua` under a line naming the plugin folder means that plugin's file, and
    read against the session's folder and home it matched nothing and refused the edit.
    """
    found = [p.strip().rstrip(".,;") for p in re.findall(r"[a-zA-Z]:[\\/][^\n\"'`]*", text or "")]
    return [p for p in found if os.path.isdir(p)]


def _in_scratchpad(path):
    """Whether a path sits in a session scratchpad under the temp folder's claude directory."""
    import tempfile
    base = os.path.normcase(os.path.join(tempfile.gettempdir(), "claude")) + os.sep
    p = os.path.normcase(os.path.abspath(path))
    return p.startswith(base) and (os.sep + "scratchpad" + os.sep) in p


def guard(event):
    """Refuse an agent's edit outside the scope its brief gave. A path rule in capitals, boxed and
    repeated, still let two past agents edit a stale worktree; a hook does not rely on reading."""
    if not event.get("agent_id"):
        return None
    target = str((event.get("tool_input") or {}).get("file_path") or "")
    allowed = scopes(_agent_transcript(event))
    if not target or not allowed:
        return None
    # a scope line is relative to home
    roots = bases(_brief_text(_agent_transcript(event))) + [r for r in (event.get("cwd"), os.path.expanduser("~")) if r]
    t = _norm_path(target, event.get("cwd"))
    # a session's scratchpad sits outside every project
    if _in_scratchpad(t):
        return None
    here = [_norm_path(s, None, roots) for s in allowed]
    if any(t == s or t.startswith(s + os.sep) for s in here):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "%s is outside this task's scope (%s). If it "
                                   "really needs changing, say so in the handback under question."
                                   % (target, ", ".join(allowed))}}


NOWHERE = {"/dev/null", "nul", "$null"}
WRITERS = {"tee": "args", "touch": "args", "mkdir": "args", "rm": "args", "rmdir": "args",
           "cp": "last", "mv": "last", "install": "last"}


def _strip_heredocs(command):
    """The command without heredoc bodies, whose text is data and may hold a `>`."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        ends = re.findall(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?", line)
        i += 1
        for end in ends:
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            i += 1
    return "\n".join(out)


def _break_lines(command):
    """Unquoted line breaks end a command, as `;` does, which shlex would read as plain space."""
    out, quote, prev = [], None, ""
    for ch in command:
        if quote:
            if ch == quote and prev != "\\":
                quote = None
        elif ch in "'\"" and prev != "\\":
            quote = ch
        elif ch == "\n":
            ch = " ; "
        out.append(ch)
        prev = ch
    return "".join(out)


def shell_writes(command, cwd):
    """The files a shell command would write or remove, as (path as written, the folder it was in).

    A redirect, tee, sed -i, cp, mv, rm, touch or mkdir. The benchmark's rational-matrix agent wrote
    its scratch cases outside its task folder with `python - > file`, which the Edit/Write guard
    never saw. Paths built from variables cannot be known and are left alone.
    """
    import shlex
    try:
        lex = shlex.shlex(_break_lines(_strip_heredocs(command)), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    out, here, seg = [], cwd, []

    def flush(seg, here):
        if not seg:
            return here
        # redirects anywhere in the segment
        for k, tok in enumerate(seg):
            if tok in (">", ">>", "&>") and k + 1 < len(seg):
                out.append((seg[k + 1], here))
        words = [w for w in seg if w not in (">", ">>", "&>")]
        name = os.path.basename(words[0]).lower() if words else ""
        args = [w for w in words[1:] if not w.startswith("-")]
        if name == "cd" and args:
            here = args[0] if os.path.isabs(args[0]) or re.match(r"^[A-Za-z]:|^/", args[0]) else os.path.join(here or "", args[0])
        elif name == "sed" and any(w == "-i" or w.startswith("-i") for w in words[1:]):
            out.extend((a, here) for a in args[1:])
        elif WRITERS.get(name) == "args":
            out.extend((a, here) for a in args)
        elif WRITERS.get(name) == "last" and len(args) >= 2:
            out.append((args[-1], here))
        return here

    for tok in tokens:
        if tok in ("&&", "||", ";", "|", "&", "\n"):
            here = flush(seg, here)
            seg = []
        else:
            seg.append(tok)
    flush(seg, here)
    return [(p, where) for p, where in out if p and p.lower() not in NOWHERE and "$" not in p
            and not p.startswith("&")]


def shell_guard(event):
    """The Edit/Write guard, for what a shell command writes."""
    if not event.get("agent_id"):
        return None
    transcript = _agent_transcript(event)
    allowed = scopes(transcript)
    if not allowed:
        return None
    command = str((event.get("tool_input") or {}).get("command") or "")
    roots = bases(_brief_text(transcript)) + [r for r in (event.get("cwd"), os.path.expanduser("~")) if r]
    here = [_norm_path(s, None, roots) for s in allowed]
    outside = []
    for path, where in shell_writes(command, event.get("cwd")):
        t = _norm_path(path, where)
        if _in_scratchpad(t) or any(t == s or t.startswith(s + os.sep) for s in here):
            continue
        outside.append(path)
    if not outside:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "this command writes outside this task's scope "
                                   "(%s): %s. Write inside the scope, or say what you need under question."
                                   % (", ".join(allowed), ", ".join(outside[:3]))}}


# minutes and tool calls, from budget.json
BUDGET = {"softMinutes": 15, "softCalls": 60, "hardMinutes": 30, "hardCalls": 100}


def _limits():
    from rs3mcp.common import ROOT
    out = dict(BUDGET)
    try:
        out.update(json.load(io.open(os.path.join(ROOT, "agents", "budget.json"), encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return out


def budget(event, now=None):
    """What to tell an agent that has run long, after one of its tool calls. None for the rest.

    Only subagents, and only past a limit. Past the soft one it is said every tenth call, not every
    call, or it becomes the thing the agent learns to read past.
    """
    if not event.get("agent_id"):
        return None
    tr = _agent_transcript(event)
    if not readable(tr):
        return None
    import datetime
    first, calls = None, 0
    for line in io.open(tr, encoding="utf-8", errors="replace"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if first is None and row.get("timestamp"):
            first = row["timestamp"]
        for b in (row.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                calls += 1
    if first is None:
        return None
    try:
        began = datetime.datetime.strptime(first[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    minutes = ((now or datetime.datetime.utcnow()) - began).total_seconds() / 60.0
    lim = _limits()
    if minutes >= lim["hardMinutes"] or calls >= lim["hardCalls"]:
        say = ("%d minutes and %d tool calls. Stop here: hand back partial with what is done, what is "
               "left, and what is in the way. Start nothing new." % (minutes, calls))
    elif (minutes >= lim["softMinutes"] or calls >= lim["softCalls"]) and calls % 10 == 0:
        say = ("%d minutes and %d tool calls. Agents on jobs like this finish in under ten minutes. If "
               "you are trying more of the same, stop, and hand back partial with what you have and "
               "what is blocking you." % (minutes, calls))
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": say}}


def main():
    if "--hook" in sys.argv:
        try:
            event = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0
        if event.get("hook_event_name") == "PostToolUse":
            decision = budget(event)
            if decision:
                sys.stdout.write(json.dumps(decision))
            return 0
        if event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") in EDITS:
            decision = guard(event)
        elif event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") in SHELLS:
            decision = shell_guard(event)
        else:
            decision = hook(event)
        if decision:
            sys.stdout.write(json.dumps(decision))
        return 0
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1].strip())
        return 1
    path = sys.argv[1]
    message = ""
    for line in io.open(path, encoding="utf-8", errors="replace"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == "assistant":
            for b in (row.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    message = b["text"]
    found = problems(message, path)
    print("holds" if not found else "\n".join("- " + p for p in found))
    return 0 if not found else 2


if __name__ == "__main__":
    sys.exit(main())
