"""The compact profile: three tools that find, explain and run the other sixty.

A client that lists every tool pays for all of them each session, before it asks anything; listed
alone, these three are a few percent of that. Finding a tool costs one small call, paid only when a
model does not already know the name. test_gateway pins the size.

rs3_find ranks from what the server already says about its tools: the howto flows (the task each
tool serves and the note on each step) and each tool's name, description and argument names. No
second list to keep up to date.

bind() is handed the server's registry at start, since importing the server from here would load
it twice.
"""

import collections
import math
import re

WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an and any are as at be by can do does for from has how i in is it its me my of on "
           "one or so that the their them then there this to up use what when where which who why "
           "with you your".split())
# how much each source of words counts
FLOW_TITLE, FLOW_NOTE, NAME, DESCRIPTION, ARGS, ARG_NOTE = 3.0, 2.0, 3.0, 1.0, 0.5, 0.25
# first step of a flow counts double
FIRST_STEP = 2.0
# game words and writing verbs, mapped
SYNONYMS = {"item": "obj", "monster": "npc", "object": "loc", "scenery": "loc", "record": "append",
            "save": "append", "store": "append"}
FIND_LIMIT = 5
ONE_LINE = 110

_TOOLS, _BY_NAME, _INDEX = [], {}, {}


def _stem(w):
    for end in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(end) + 3 and w.endswith(end):
            return w[:-len(end)] + ("y" if end == "ies" else "")
    return w


def _words(text):
    return [_stem(SYNONYMS.get(w, w)) for w in WORD.findall(str(text).lower()) if w not in STOP]


def bind(tools, by_name, flows):
    """The server's tools and the howto flows, indexed once."""
    _TOOLS[:] = tools
    _BY_NAME.clear()
    _BY_NAME.update(by_name)
    weights = collections.defaultdict(collections.Counter)
    for t in tools:
        bag = weights[t["name"]]
        for w in _words(t["name"].replace("rs3_", "").replace("_", " ")):
            bag[w] += NAME
        for w in _words(t.get("description")):
            bag[w] += DESCRIPTION
        for arg, spec in ((t.get("inputSchema") or {}).get("properties") or {}).items():
            for w in _words(arg):
                bag[w] += ARGS
            for w in _words(spec.get("description") or ""):
                bag[w] += ARG_NOTE
        for example in t.get("examples") or []:
            for w in _words(" ".join(str(v) for v in example.values())):
                bag[w] += ARGS
    for flow in flows:
        title, steps = flow[0], flow[1]
        first = next((tool for tool, _ in steps if tool), None)
        for tool, note in steps:
            if tool not in weights:
                continue
            for w in _words(title):
                weights[tool][w] += FLOW_TITLE * (FIRST_STEP if tool == first else 1)
            for w in _words(note):
                weights[tool][w] += FLOW_NOTE
    # rarer words say more
    df = collections.Counter(w for bag in weights.values() for w in bag)
    n = len(weights)
    _INDEX.clear()
    _INDEX.update({name: {w: v * math.log(1 + n / df[w]) for w, v in bag.items()} for name, bag in weights.items()})


def _line(t):
    first = re.split(r"(?<=[.!?])\s", str(t.get("description") or ""), 1)[0]
    return first if len(first) <= ONE_LINE else first[:ONE_LINE - 1].rstrip() + "…"


def tool_find(args):
    task = str(args.get("task") or "").strip()
    if not task:
        raise ValueError("task is what you want done, in a few words")
    limit = max(1, min(int(args.get("limit") or FIND_LIMIT), 20))
    exact = task if task in _BY_NAME else "rs3_" + task if "rs3_" + task in _BY_NAME else None
    want = _words(task)
    scored = []
    for name, weights in _INDEX.items():
        score = sum(weights.get(w, 0.0) for w in want)
        if name == exact:
            score += 1e6
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda s: (-s[0], s[1]))
    out = []
    for _score, name in scored[:limit]:
        t = _BY_NAME[name]
        rec = {"tool": name, "does": _line(t)}
        needs = (t.get("inputSchema") or {}).get("required")
        if needs:
            rec["needs"] = needs
        out.append(rec)
    return {"tools": out, "next": "rs3_run {tool, args}; rs3_how {tool} for every argument"} if out else \
        {"tools": [], "next": "nothing matched; try other words"}


def tool_how(args):
    t = _BY_NAME.get(str(args.get("tool") or ""))
    if t is None:
        raise ValueError("no tool %r; rs3_find names them" % args.get("tool"))
    return {"tool": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}


def tool_run(args):
    raise ValueError("rs3_run is unwrapped by the dispatcher, never called")


# the dispatcher unwraps rs3_run
TOOLS = [
    {"name": "rs3_find", "fn": tool_find,
     "description": "Tools for a task, best first: name, what it does, required args.",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "rs3_how", "fn": tool_how,
     "description": "One tool's full description and arguments.",
     "inputSchema": {"type": "object", "required": ["tool"], "properties": {"tool": {"type": "string"}}}},
    {"name": "rs3_run", "fn": tool_run,
     "description": "Run a tool by name. Any tool also takes pick=a.b and outDir=<dir> to cut its answer.",
     "inputSchema": {"type": "object", "required": ["tool"], "properties": {
         "tool": {"type": "string"}, "args": {"type": "object"}}}},
]
