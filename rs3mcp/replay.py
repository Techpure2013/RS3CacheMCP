"""Every real handback the gate has judged, judged again by the gate as it is now.

    python -m rs3mcp.replay            what changed since the verdicts were last recorded
    python -m rs3mcp.replay --record   record the current verdicts, after reading every change

The gate's false positives only ever surfaced when a live agent tripped over one: prose read into a
path, a refused write counted as an edit, a bare file name never looked for. Each cost a round, and
four of one batch's five bounces were the gate's. So the handbacks agents really wrote are kept,
with their transcripts, and a change to the gate that moves any verdict shows up here first.

Each is judged against its transcript cut off at that handback, since a command the agent ran
afterwards would otherwise answer an earlier "never run". The file checks still read today's disk,
so a verdict can also move because a file was moved; the note on a recorded verdict says so.
"""

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

from rs3mcp import handback  # noqa: E402
from rs3mcp.common import ROOT  # noqa: E402

FOLDER = os.path.join(ROOT, "agents", "replay")
RECORDED = os.path.join(FOLDER, "verdicts.json")


def _text(row):
    content = (row.get("message") or {}).get("content") or []
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def attempts(folder=None):
    """(key, transcript lines up to the handback, the handback, cwd) for every kept handback."""
    folder = folder or FOLDER
    out = []
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".jsonl"):
            continue
        lines = io.open(os.path.join(folder, name), encoding="utf-8", errors="replace").readlines()
        k = 0
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            text = _text(row)
            if handback.parse(text) is None:
                continue
            k += 1
            out.append(("%s#%d" % (name[:-6], k), lines[:i + 1], text, row.get("cwd")))
    return out


def judge(folder=None):
    """{key: {"verdict", "problems"}} from the gate as it is now."""
    out = {}
    work = tempfile.mkdtemp()
    try:
        for key, lines, text, cwd in attempts(folder):
            cut = os.path.join(work, key.replace("#", "-") + ".jsonl")
            io.open(cut, "w", encoding="utf-8").writelines(lines)
            found = handback.problems(text, cut, cwd)
            out[key] = {"verdict": "sent back" if found else "checked", "problems": found}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def recorded(path=None):
    try:
        return json.load(io.open(path or RECORDED, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def changes(folder=None, path=None):
    """Keys whose verdict is not the recorded one, as (key, was, now, now's first problem)."""
    was, now = recorded(path), judge(folder)
    out = []
    for key in sorted(set(was) | set(now)):
        a, b = was.get(key, {}).get("verdict"), now.get(key, {}).get("verdict")
        if a != b:
            out.append((key, a, b, (now.get(key, {}).get("problems") or [""])[0]))
    return out


def keep(agent_ids):
    """Copy these agents' transcripts in, so the set outlives the sessions they came from."""
    os.makedirs(FOLDER, exist_ok=True)
    kept = 0
    for agent in agent_ids:
        tr = handback._agent_transcript({"agent_id": agent})
        if handback.readable(tr):
            shutil.copyfile(tr, os.path.join(FOLDER, agent + ".jsonl"))
            kept += 1
    return kept


def main(argv):
    if "--record" in argv:
        now = judge()
        notes = {k: v.get("note") for k, v in recorded().items() if v.get("note")}
        for k, v in now.items():
            if notes.get(k):
                v["note"] = notes[k]
        os.makedirs(FOLDER, exist_ok=True)
        io.open(RECORDED, "w", encoding="utf-8", newline="\n").write(json.dumps(now, indent=1, sort_keys=True))
        print("recorded %d verdicts" % len(now))
        return 0
    moved = changes()
    total = len(attempts())
    if not moved:
        print("%d handbacks, every verdict as recorded" % total)
        return 0
    for key, was, now, first in moved:
        print("%-22s %-9s -> %-9s %s" % (key, was, now, first[:90]))
    print("\n%d of %d moved. Read each, then --record" % (len(moved), total))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
