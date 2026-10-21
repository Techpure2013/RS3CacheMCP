"""
The client API surface, read out of the typings so it cannot drift, plus where the typings are
wrong.

The second half is the point. The typings are generated and incomplete: properties that exist on
the live client are missing, one documented property does not exist, and one is counted in units
nothing else uses. Each cost an afternoon of probing and none is discoverable by reading.

Where a gotcha contradicts the typings, the gotcha wins. It came off a running client.

Tools:
  rs3_api          the surface, by name or member
  rs3_api_gotchas  what has been proved wrong about it
"""

import io
import json
import os
import re

from rs3api.paths import TYPINGS

DECL = re.compile(r"^(\s*)(?:declare\s+)?(?:export\s+)?"
                  r"(class|interface|namespace|enum|type)\s+([A-Za-z_]\w*)")

MEMBER = re.compile(r"^\s*(?:readonly\s+|static\s+|const\s+|function\s+|let\s+|var\s+)*"
                    r"([A-Za-z_]\w*)\s*[?(:<]")


# each one is something a running client did
GOTCHAS = [
    {
        "subject": "NPCEntity.aabb, PlayerEntity.aabb",
        "claim": "Both exist and work, though neither is declared in game.d.ts.",
        "evidence": "Read back live on a wolf: aabb = { min = { 1463658, 4843, 1769085 }, "
                    "max = { 1464760, 5418, 1769347 } }. The math.d.ts header does mention them, "
                    "so the two files disagree with each other and math.d.ts is the right one.",
        "build": "949-1",
    },
    {
        "subject": "LocEntity.aabb",
        "claim": "Exists, despite both files saying it is the one entity without a bounding box.",
        "evidence": "A Tree returned 2706 x 4083 x 2877. The width and length fallback could not "
                    "have produced that: it builds X out of whole tiles and 2706 is not a "
                    "multiple of 512.",
        "build": "949-1",
    },
    {
        "subject": "NPCEntity.facing",
        "claim": "Counts a full turn in 512, not the 2048 that every other angle in this api uses.",
        "evidence": "Twelve exact fits of a cache model's outline against the live aabb. Pin the "
                    "multiplier at four and the leftover holds at 1403 give or take twelve. "
                    "Treating it as 2048ths turns a model at a quarter of the right rate, which "
                    "looks like an alignment that is nearly right and drifts as the thing turns.",
        "build": "949-1",
    },
    {
        "subject": "ui.Model.rotation",
        "claim": "Documented but absent. Not writable and not readable.",
        "evidence": "Write returned false and the read came back nothing, while screenOffset and "
                    "worldOffset, both Vector3 on the same object, took fine. There is no way to "
                    "pose a model in the client, and ModelGroup is not a way round it either: "
                    "AddCamera and activeCamera both return nothing.",
        "build": "949-1",
    },
    {
        "subject": "ui.Sprite.spriteID",
        "claim": "Stays -1 when a plugin's own art is bound. That is correct, not a failure.",
        "evidence": "spriteID is the stock sprite slot. runtimeSprite is the property that says "
                    "whether your own png actually bound, and the resource's own inUse flag is "
                    "the other half of the answer.",
        "build": "949-1",
    },
    {
        "subject": "ui.Sprite alpha and colour",
        "claim": "Sprite has none of its own. They are inherited from Component, and there is a "
                 "separate SpriteDataConfig besides.",
        "evidence": "Reading a class's own block in the typings gives half its surface. The base "
                    "class holds the rest, and these files do not repeat inherited members.",
        "build": "949-1",
    },
    {
        "subject": "Plugin png in a client owned interface",
        "claim": "Binds and reports healthy, and never draws.",
        "evidence": "A Layer plus Sprite parented into the backpack, interface 1473: write true, "
                    "runtimeSprite a live object, resource inUse true, hidden false, visible true, "
                    "sized to the slot and positioned on it. Nothing appears. A stock sprite id in "
                    "the very same component draws immediately. job-gauges does the identical "
                    "assignment and its sprites do draw, but into its own overlay rather than into "
                    "an interface the client owns.",
        "build": "949-1",
    },
    {
        "subject": "Writing any property through sol",
        "claim": "pcall returning true does not mean the value was kept.",
        "evidence": "sol accepts a property assignment it cannot convert and silently drops it, so "
                    "the write raises nothing and changes nothing. The only trustworthy test is to "
                    "read the value back.",
        "build": "949-1",
    },
    {
        "subject": "nil reaching a native argument",
        "claim": "Takes the client down. It is not a lua error and pcall cannot catch it.",
        "evidence": "Vector3.new(x, nil, z) from an entity that went out of range mid frame. "
                    "Check the type before the call, not around it.",
        "build": "949-1",
    },
    {
        "subject": "pairs(id.VarPlayer), pairs(id.Interface) and the other id tables",
        "claim": "Yield id, name: the key is the number and the value is the name. Indexing goes the "
                 "other way, id.VarPlayer[name] is the id, so a loop written as `for name, vid in "
                 "pairs(...)` gets strings where it wants numbers.",
        "evidence": "Made twice. necro-probe's first interface snapshot threw the name away and "
                    "printed bare numbers; quest-dev's worldscan compared a name with 0 and died "
                    "with \"attempt to compare number with string\". Varbit ids come back negative, "
                    "base vars positive, and some var types throw on read, so guard the read.",
        "build": "949-1",
    },
    {
        "subject": "Entity handles kept between frames",
        "claim": "Dangle. The client frees them and isValid cannot save you.",
        "evidence": "An NPCEntity taken from a MiniMenu entry and read on a later frame crashes "
                    "once the npc despawns, because reading isValid is itself the read that "
                    "faults. Store an identity -- a uid, or a tile and level -- and look the "
                    "entity up again from the lists each frame.",
        "build": "949-1",
    },
    {
        "subject": "HTTPRequest.new",
        "claim": "The first argument is a url object from Plugin.urls, not a string, whatever the "
                 "typings say. They declare it as baseURL: string.",
        "evidence": "Passing a string returns \"stack index 1, expected userdata, received string\" "
                    "out of the shared_ptr constructor, naming LuaPluginMetadata::URL. The url has "
                    "to be declared in plugin.json under a urls block and reached as "
                    "Plugin.urls.<name>, and a manifest change needs plugins_refresh. It does not "
                    "go the other way either: concatenating one raises \"attempt to concatenate a "
                    "sol.jag::scripting::LuaPluginMetadata::URL value\", so it cannot be logged by "
                    "building a string out of it. Pass it to HTTPRequest and say the route "
                    "separately.",
        "build": "949-1",
    },
    {
        "subject": "config.Quest",
        "claim": "Enumerates with pairs and gives 533 quest configs, which is more than the cache "
                 "has decoded and far more than quest_vars records.",
        "evidence": "The cache holds 531 Quest rows with an empty data column for every one, so "
                    "titles are only reachable from a running client. QuestType there carries "
                    "name, sortName, difficulty, questPoints, masterQuestVars with their start and "
                    "end states, preReqQuests, preReqSkills and startCoordGrids.",
        "build": "949-1",
    },
    {
        "subject": "Event.Logic",
        "claim": "Carries logicTick, not tick.",
        "evidence": "EventChannel<{ logicTick: number }>. Subscribing and reading e.tick gets nil "
                    "forever, with no error.",
        "build": "949-1",
    },
    {
        "subject": "NPCList.ByConfigID, ByConfig, Filter by config",
        "claim": "All return nothing.",
        "evidence": "Every config keyed lookup comes back empty, so matching has to fall back to "
                    "the display name.",
        "build": "949-1",
    },
    {
        "subject": "Component category and dynamic ids",
        "claim": "Hard limited to 0 to 256.",
        "evidence": "Going past it fails to create. Use a distinct category per panel rather than "
                    "one category with many ids.",
        "build": "949-1",
    },
    {
        "subject": "A focused InputField",
        "claim": "Eats every key, Escape included.",
        "evidence": "There is no key left to close a window with while a text box has focus, so "
                    "give it a button.",
        "build": "949-1",
    },
    {
        "subject": "Coins in the inventory",
        "claim": "Are not in INV. They live in MONEY_POUCH.",
        "evidence": "Counting coins by walking the backpack finds none.",
        "build": "949-1",
    },
    {
        "subject": "Lua truthiness",
        "claim": "0 is truthy, and so is the empty string.",
        "evidence": "`hull.rest or DEFAULT` takes a stored 0 rather than the default, so a field "
                    "meaning \"not measured\" has to be absent, not zero. Only nil and false are "
                    "falsy in lua.",
        "build": "any",
    },
]


def _files():
    out = []
    for root, _dirs, names in os.walk(TYPINGS):
        for n in names:
            if n.endswith(".d.ts"):
                out.append(os.path.join(root, n))
    return sorted(out)


_CACHE = {"decls": None}


def _parse(path):
    """Every class, interface, namespace and enum in one file, with its body kept verbatim.

    Brace depth, not a parser. These files are hand written and regular, and keeping the body
    verbatim means what comes back is what is in the file.
    """
    text = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
    out, stack, depth = [], [], 0

    for i, line in enumerate(text):
        m = DECL.match(line)
        opens = line.count("{") - line.count("}")
        if m and "{" in line:
            stack.append({"kind": m.group(2), "name": m.group(3), "start": i, "depth": depth,
                          "ns": [s["name"] for s in stack if s["kind"] == "namespace"]})
        depth += opens
        while stack and depth <= stack[-1]["depth"]:
            d = stack.pop()
            body = text[d["start"]:i + 1]
            qual = ".".join(d["ns"] + [d["name"]]) if d["ns"] else d["name"]
            members = []
            for b in body[1:-1]:
                mm = MEMBER.match(b)
                if mm and not DECL.match(b):
                    members.append(mm.group(1))
            out.append({
                "name": d["name"], "qualified": qual, "kind": d["kind"],
                "file": os.path.basename(path), "path": path.replace("\\", "/"),
                "line": d["start"] + 1,
                "extends": (re.search(r"extends\s+([A-Za-z_][\w.]*)", body[0]) or [None, None])[1]
                if "extends" in body[0] else None,
                "members": members,
                "text": "\n".join(body),
            })
    return out


def model():
    """The Lua typings generated from the public docs (docstypes.py) with the extras files'
    members merged in, or None before the first fetch."""
    from rs3api import docstypes
    try:
        m = json.load(io.open(docstypes.MODEL, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for q, add in docstypes.extras().items():
        t = m["types"].setdefault(q, {"kind": "class", "extends": None, "file": "extras", "line": 0,
                                      "text": "", "members": {}})
        t["members"].update(add["members"])
        t["extends"] = add["extends"] or t["extends"]
    return m


def stamp():
    """Each typings file with its change time, so a long-running server sees an edit: the
    generated model and the extras when there is one, else the hand-written .d.ts files."""
    from rs3api import docstypes
    if os.path.isfile(docstypes.MODEL):
        return tuple((f, os.stat(f).st_mtime_ns) for f in [docstypes.MODEL] + docstypes.extras_files())
    return tuple((f, os.stat(f).st_mtime_ns) for f in _files())


def _from_model(m):
    from rs3api import docstypes
    return [{"name": q.rsplit(".", 1)[-1], "qualified": q, "kind": t["kind"], "file": t["file"],
             "path": os.path.join(docstypes.OUT, t["file"]).replace("\\", "/"), "line": t["line"],
             "extends": t["extends"], "members": sorted(t["members"]), "text": t["text"]}
            for q, t in m["types"].items()]


def _all():
    now = stamp()
    if _CACHE["decls"] is None or _CACHE.get("stamp") != now:
        m = model()
        if m is not None:
            decls = _from_model(m)
        else:
            decls = []
            for f, _ in now:
                decls.extend(_parse(f))
        _CACHE["decls"] = decls
        _CACHE["stamp"] = now
    return _CACHE["decls"]


def _gotchas_for(names):
    low = [n.lower() for n in names]
    hits = []
    for g in GOTCHAS:
        s = g["subject"].lower()
        if any(n in s for n in low if len(n) > 2):
            hits.append(g)
    return hits


def tool_api(args):
    name = (args.get("name") or "").strip()
    member = (args.get("member") or "").strip()
    kind = (args.get("kind") or "").strip().lower()
    full = bool(args.get("full"))
    if not name and not member:
        raise ValueError("give a name, or a member to search for")

    decls = _all()
    if not decls:
        return {"found": 0, "note": "no plugin api typings at %s. They come with the RS3 plugin project: "
                                    "set RS3MCP_PROJECT to the folder holding shared/types" % TYPINGS}
    if kind:
        decls = [d for d in decls if d["kind"] == kind]

    if member:
        low = member.lower()
        hit = [d for d in decls if any(m.lower() == low for m in d["members"])]
        if name:
            hit = [d for d in hit if name.lower() in d["qualified"].lower()]
    else:
        low = name.lower()
        exact = [d for d in decls if d["name"].lower() == low or d["qualified"].lower() == low]
        hit = exact or [d for d in decls if low in d["qualified"].lower()]

    # an exact hit comes back whole
    whole = full or len(hit) <= 3
    out = []
    for d in hit[:60]:
        # the full path, so a claim resolves elsewhere
        rec = {"qualified": d["qualified"], "kind": d["kind"],
               "file": d["file"], "path": d["path"], "line": d["line"],
               "extends": d["extends"], "members": d["members"]}
        if whole:
            rec["declaration"] = d["text"]
        out.append(rec)

    names = [d["qualified"] for d in hit[:60]] + ([name] if name else []) + \
            ([member] if member else [])
    return {"found": len(hit), "showing": len(out), "results": out,
            "gotchas": _gotchas_for(names),
            "note": "declarations omitted, too many matches; narrow it or pass full=true"
            if not whole else None}


def tool_gotchas(args):
    subject = (args.get("subject") or "").strip().lower()
    got = GOTCHAS
    if subject:
        got = [g for g in GOTCHAS
               if subject in g["subject"].lower() or subject in g["claim"].lower()
               or subject in g["evidence"].lower()]
    return {"gotchas": got,
            "note": "Every one of these was observed on a running client. Where one contradicts "
                    "the typings, the observation is the one to believe."}


TOOLS = [
    {"name": "rs3_api", "fn": tool_api,
     "description": "Find a class, interface, namespace or enum in the RS client API typings, or "
                    "find everything that has a given member. Returns the declaration verbatim "
                    "plus anything known to be wrong about it. Use member= to answer questions "
                    "like which entities expose aabb.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "type name, qualified or not, e.g. Sprite or ui.Sprite"},
         "member": {"type": "string", "description": "a property or method name to search for across all types"},
         "kind": {"type": "string", "description": "narrow to class, interface, namespace or enum"},
         "full": {"type": "boolean", "description": "return full declarations even for a wide search"}}}},
    {"name": "rs3_api_gotchas", "fn": tool_gotchas,
     "description": "Everything known to be wrong or surprising about the RS client API: "
                    "properties that exist but are not declared, one that is documented and does "
                    "not exist, units that differ from the rest of the api, and the crash shapes. "
                    "Each with what was actually observed. Worth reading before trusting a typing.",
     "inputSchema": {"type": "object", "properties": {
         "subject": {"type": "string", "description": "filter, e.g. aabb, sprite, facing, crash"}}}},
]
