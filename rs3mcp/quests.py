"""Quests: which var tracks one, and at what values it starts and completes.

The cache holds a Quest row's id and name and nothing else -- no title, no var, no thresholds.
Those come from a running client, so they ship as quests.json beside this module, built by
rs3api/questdata.py and refreshed after an update adds a quest.

The varbit half is read from the cache, so a mask or a shift is always current even when the
shipped table is a version behind.
"""

import io
import json
import os
import re

from rs3mcp.common import connect

TABLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quests.json")

_ROWS = None


def _rows():
    global _ROWS
    if _ROWS is None:
        try:
            _ROWS = (json.load(io.open(TABLE, encoding="utf-8")) or {}).get("rows") or []
        except (OSError, ValueError):
            _ROWS = []
    return _ROWS


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _shift(mask):
    return (mask & -mask).bit_length() - 1 if mask else 0


def _varbit(name):
    """mask, shift and base var for one varbit, from the cache rather than the shipped table."""
    try:
        c = connect()
        row = c.execute("select id, name, base_name, base_id, mask, shift from varbit "
                        "where name = ?", (name,)).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def _answer(r):
    out = {"title": r.get("title") or r.get("sortName"),
           "configName": r.get("questConfig"), "configID": r.get("configID")}
    if r.get("var"):
        var = {"name": r["var"], "id": r.get("varId"),
               "startsAt": r.get("start"), "completeAt": r.get("complete")}
        live = _varbit(r["var"])
        if live:
            var["baseVar"] = live["base_name"]
            var["baseVarId"] = live["base_id"]
            var["mask"] = live["mask"]
            var["shift"] = live["shift"] if live["shift"] is not None else _shift(live["mask"])
            var["from"] = "mask and shift read from the cache"
        else:
            var["baseVar"] = r.get("baseVar")
            var["baseVarId"] = r.get("baseVarId")
            var["mask"] = r.get("mask")
            var["shift"] = _shift(r.get("mask") or 0)
        out["var"] = var
    else:
        out["var"] = None
        out["note"] = "no progression var recorded for this quest"
    for k in ("members", "questPoints", "difficulty", "preReqQuests", "preReqSkills",
              "preReqQuestPoints", "startCoords"):
        if r.get(k) is not None:
            out[k] = r[k]
    return out


def tool_quest(args):
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("give a quest title, config name, or config id")
    key = _norm(name)
    rows = _rows()

    exact = [r for r in rows
             if key in (_norm(r.get("title")), _norm(r.get("sortName")),
                        _norm(r.get("questConfig")), _norm(r.get("var")), _norm(r.get("baseVar")))]
    if exact:
        return _answer(next((r for r in exact if r.get("var")), exact[0]))
    if name.lstrip("-").isdigit():
        want = int(name)
        for r in rows:
            if r.get("configID") == want:
                return _answer(r)
    loose = [r for r in rows if key and (key in _norm(r.get("title"))
                                         or key in _norm(r.get("questConfig")))]
    if len(loose) == 1:
        return _answer(loose[0])
    if loose:
        return {"found": len(loose), "did_you_mean":
                [{"title": r.get("title"), "configName": r.get("questConfig")} for r in loose[:20]]}
    return {"found": 0, "note": "no quest by that title, config name or var"}


def tool_quest_var(args):
    v = args.get("var")
    if v is None:
        raise ValueError("give a var id or a var name")
    hits = []
    for r in _rows():
        if str(v).lstrip("-").isdigit():
            n = int(v)
            if n in (r.get("varId"), r.get("baseVarId")) or r.get("varId") == -n:
                hits.append(r)
        elif _norm(v) in (_norm(r.get("var")), _norm(r.get("baseVar"))):
            hits.append(r)
    return {"found": len(hits), "quests": [_answer(r) for r in hits[:20]],
            "note": "A quest varbit is a slice of a shared var, so one base var id can carry "
                    "several quests. Only the base var transmits."}


TOOLS = [
    {"name": "rs3_quest", "fn": tool_quest,
     "description": "One quest by title, config name or config id: its cache id, and the var that "
                    "tracks it with the values it starts and completes at, plus the base var, mask "
                    "and shift the bits sit at. rs3_quest_var answers the other direction.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "title, config name, var name, or config id"}}}},
    {"name": "rs3_quest_var", "fn": tool_quest_var,
     "description": "Which quest a var belongs to, from a varbit id, a base var id, or a var name. "
                    "Answers the other direction from rs3_quest.",
     "inputSchema": {"type": "object", "required": ["var"], "properties": {
         "var": {"type": "string",
                 "description": "var id (negative for a varbit) or name, as rs3_var returns"}}}},
]
