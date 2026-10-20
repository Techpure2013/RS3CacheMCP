"""
Varbits, and the packing that hides them.

A varbit is a named slice of bits inside a base VarPlayer. Only the base transmits, so watching a
varbit and watching its base are different jobs.

Varbits are stored here under negative ids, so one id space holds both without a clash.

A declared varbit is not a promise the game writes one. Check a value moves before keying on it.

Tools:
  rs3_var  a var by name or id, with everything else packed into the same base
"""

from . import addons
from .common import connect

BITS = 32


def _quest_vars():
    """Quest vars matched to readable titles, when an add-on has them. Without, QUEST_ARCH2_MAIN has
    to be guessed at."""
    offered = addons.first("quest_vars")
    return offered() if offered else {"by_var": {}, "by_base": {}}


def _width(mask):
    """Bit count, and whether they sit in one run.

    Every cache mask is contiguous. Checked anyway -- a gap would break the shift."""
    n = bin(mask).count("1")
    if n == 0:
        return 0, True
    lo = (mask & -mask).bit_length() - 1
    return n, mask >> lo == (1 << n) - 1


def _slice(row):
    mask = row["mask"] or 0
    width, solid = _width(mask)
    out = {"varbit": row["id"], "name": row["name"], "shift": row["shift"],
           "mask": mask, "bits": width,
           "range": [0, (1 << width) - 1] if width else [0, 0]}
    if width:
        hi = row["shift"] + width - 1
        out["holds"] = ("bit %d" % row["shift"] if width == 1
                        else "bits %d-%d" % (row["shift"], hi))
    if not solid:
        out["note"] = "mask is not one run of bits, shift alone does not describe it"
    return out


def _layout(slices):
    """Which of the 32 bits are claimed and which are free. Reading it as a picture is how you see
    that a base var is nearly full, or that a value you are chasing has nowhere to live.

    216 base vars have overlapping varbits, nearly all nesting rather than collision: a wide one
    laid over several narrow so the group reads in one call. 2,282 pairs nest that way against 34
    sharing a mask outright.
    """
    claimed = [[] for _ in range(BITS)]
    for s in slices:
        for b in range(BITS):
            if s["mask"] >> b & 1:
                claimed[b].append(s["name"])
    used = sum(1 for x in claimed if x)
    free, run = [], None
    for b in range(BITS + 1):
        taken = b == BITS or claimed[b]
        if taken and run is not None:
            free.append("%d" % run if run == b - 1 else "%d-%d" % (run, b - 1))
            run = None
        elif not taken and run is None:
            run = b
    out = {"bitsUsed": used, "bitsFree": BITS - used, "freeRanges": free or ["none"]}

    covers, aliases, partial = [], [], []
    for i, a in enumerate(slices):
        inside = []
        for j, b in enumerate(slices):
            if i == j or not (a["mask"] & b["mask"]):
                continue
            if a["mask"] == b["mask"]:
                if i < j:
                    aliases.append([a["name"], b["name"]])
            elif a["mask"] & b["mask"] == b["mask"]:
                inside.append(b["name"])
            elif a["mask"] & b["mask"] != a["mask"]:
                if i < j:
                    partial.append([a["name"], b["name"]])
        if inside:
            covers.append({"wide": a["name"], "holds": a.get("holds"), "over": inside})
    if covers:
        out["widerVarbitsOver"] = covers
    if aliases:
        out["sameMask"] = aliases
    if partial:
        out["partialOverlap"] = partial
    if covers or aliases or partial:
        bits = []
        if covers:
            bits.append("a wide varbit sits over narrower ones, reading the whole group at once")
        if aliases:
            bits.append("two names share one mask outright, so they are the same value")
        if partial:
            bits.append("two masks overlap without either containing the other, which is odd "
                        "enough to check by hand")
        out["note"] = "overlapping slices here: " + "; ".join(bits) + "."
    return out


def _base(c, base_id):
    r = c.execute("select id, name from config where type='VarPlayer' and id=?",
                  (base_id,)).fetchone()
    return dict(r) if r else {"id": base_id, "name": None}


def _find(c, who):
    who = str(who).strip()
    if who.lstrip("-").isdigit():
        # VarPlayer first: varbits only live in VarPlayers
        return c.execute("select type,id,name from config where type in ('VarPlayer','VarClient') "
                         "and id=? order by type desc", (int(who),)).fetchall()
    rows = c.execute("select type,id,name from config where type in ('VarPlayer','VarClient') "
                     "and name=? collate nocase", (who,)).fetchall()
    return rows or c.execute(
        "select type,id,name from config where type in ('VarPlayer','VarClient') "
        "and name like ? collate nocase order by abs(id) limit 20",
        ("%" + who + "%",)).fetchall()


def tool_var(args):
    who = args.get("id")
    if who is None:
        who = args.get("name")
    search = args.get("search")
    limit = min(int(args.get("limit", 60)), 300)
    c = connect()

    if search:
        hits = c.execute(
            "select type,id,name from config where type in ('VarPlayer','VarClient') "
            "and name like ? collate nocase order by abs(id) limit ?",
            ("%" + str(search).strip() + "%", limit)).fetchall()
        return {"found": len(hits),
                "vars": [{"domain": r["type"], "id": r["id"], "name": r["name"],
                          "kind": "varbit" if r["id"] < 0 else "base var"} for r in hits],
                "note": "a negative id is a varbit, a slice of some base var. Ask again by name "
                        "or id to see the base it sits in and what shares it."}

    if who is None:
        return {"counts": {
            "baseVarPlayer": c.execute("select count(*) from config where type='VarPlayer' "
                                       "and id>=0").fetchone()[0],
            "varbits": c.execute("select count(*) from varbit").fetchone()[0],
            "VarClient": c.execute("select count(*) from config where type='VarClient'"
                                   ).fetchone()[0]},
            "note": "pass name=, id= or search=. Only base vars transmit; a varbit is bits inside "
                    "one of them."}

    rows = _find(c, who)
    domain = args.get("domain")
    if domain:
        rows = [r for r in rows if r["type"].lower() == str(domain).strip().lower()]
    if not rows:
        return {"found": 0, "note": "no var by that id or name. Try search= for a substring."}
    also = None
    if len(rows) == 2 and rows[0]["id"] == rows[1]["id"]:
        also, rows = rows[1], rows[:1]
    if len(rows) > 1:
        return {"found": len(rows),
                "ambiguous": [{"domain": r["type"], "id": r["id"], "name": r["name"]}
                              for r in rows[:limit]],
                "note": "several match, ask again by id"}

    row = rows[0]
    q = _quest_vars()
    out = {"domain": row["type"], "id": row["id"], "name": row["name"]}
    if also is not None:
        out["sameIdIn"] = {"domain": also["type"], "name": also["name"],
                           "ask": "domain=%s for that one" % also["type"]}

    bit = c.execute("select id,name,base_name,base_id,mask,shift from varbit where id=?",
                    (row["id"],)).fetchone()
    if bit is not None:
        out["kind"] = "varbit"
        out.update(_slice(bit))
        out["base"] = _base(c, bit["base_id"])
        base_id = bit["base_id"]
    else:
        out["kind"] = "base var"
        base_id = row["id"] if row["type"] == "VarPlayer" and row["id"] >= 0 else None

    if base_id is not None:
        sibs = c.execute("select id,name,mask,shift from varbit where base_id=? order by shift, id",
                         (base_id,)).fetchall()
        if sibs:
            slices = [_slice(s) for s in sibs]
            out["packedInto" if bit is not None else "packs"] = slices[:limit]
            if len(slices) > limit:
                out["truncated"] = "%d of %d shown, raise limit" % (limit, len(slices))
            out["layout"] = _layout(slices)
        elif bit is None:
            out["layout"] = {"bitsUsed": 0, "bitsFree": BITS, "freeRanges": ["0-31"],
                             "note": "no varbits declared on this one, it is read whole"}

    qv = q["by_var"].get(row["name"]) or q["by_base"].get(row["name"])
    if bit is not None and not qv:
        qv = q["by_base"].get(bit["base_name"])
    if qv:
        out["quest"] = {"title": qv.get("title"), "config": qv.get("questConfig"),
                        "startsAt": qv.get("start"), "completeAt": qv.get("complete"),
                        "progressVar": qv.get("var")}
    else:
        # only the progression var is in the table
        try:
            from . import addons
            quest_var = addons.first("quest_var")
            alt = quest_var({"var": row["name"]}) if quest_var else None
        except Exception:
            alt = None
        hit = ((alt or {}).get("quests") or [None])[0]
        if hit:
            # possibleQuest, since a short name swallows others
            out["possibleQuest"] = {
                "title": hit.get("title"), "config": hit.get("configName"),
                "matchedBy": "the quest config as a name prefix, not the var that tracks it",
                "check": "a short config name over-matches. rs3_quest stateVars lists what it "
                         "really owns"}

    out["transmits"] = (out.get("base", {}).get("name") if bit is not None else row["name"])
    out["note"] = ("Only the base var moves on the wire; a varbit is bits inside it, so watch the "
                   "base and mask the value yourself. And a declared varbit is not proof "
                   "the game writes it -- check a value actually moves before keying anything on "
                   "it.")
    return out


TOOLS = [
    {"name": "rs3_var", "fn": tool_var,
     "description": "A VarPlayer or VarClient decoded: for a varbit, its base var "
                    "with mask, shift and range; for a base var, its varbits and free bits. "
                    "quest is set when a quest tracks it, possibleQuest when only the name says so.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "var name, exact or partial"},
         "id": {"type": "string", "description": "var id, negative for a varbit"},
         "domain": {"type": "string", "description": "VarPlayer or VarClient"},
         "search": {"type": "string", "description": "list every var whose name contains this"},
         "limit": {"type": "integer", "description": "max slices or hits, default 60"}}}},
]
