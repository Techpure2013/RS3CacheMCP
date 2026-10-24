"""Build the quest table that ships with rs3mcp, out of the client harvest.

    python rs3api/questdata.py [-o rs3mcp/quests.json]

The cache holds a Quest row's id and name and nothing else: every title, var and threshold comes
from a running client. This folds the two harvests in RS3_API_INFO into one compact file so the
shipped rs3_quest and rs3_quest_var can answer without one. Re-run it after a game update adds a
quest, and commit the result.
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rs3api.paths import API_INFO  # noqa: E402  run as a script, rs3cache isn't on the path

VARS = os.path.join(API_INFO, "quest_vars.json")
CONFIGS = os.path.join(API_INFO, "quest_configs.json")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rs3mcp", "quests.json")

VAR_KEYS = ("title", "questConfig", "var", "varId", "baseVar", "baseVarId", "isVarBit",
            "mask", "start", "complete")
CONFIG_KEYS = ("members", "questPoints", "difficulty", "preReqQuests", "preReqSkills",
               "preReqQuestPoints", "startCoords", "sortName", "deprecated")


def rows(path):
    if not os.path.exists(path):
        return []
    return (json.load(io.open(path, encoding="utf-8")) or {}).get("rows") or []


def build():
    by_config = {}
    for r in rows(VARS):
        name = r.get("questConfig")
        if not name:
            continue
        kept = {k: r[k] for k in VAR_KEYS if r.get(k) is not None}
        by_config.setdefault(name, {}).update(kept)

    extra = 0
    for r in rows(CONFIGS):
        name = r.get("configName")
        if not name:
            continue
        seat = by_config.setdefault(name, {"questConfig": name})
        seat.setdefault("title", r.get("title"))
        seat.setdefault("configID", r.get("id"))
        for k in CONFIG_KEYS:
            if r.get(k) not in (None, [], ""):
                seat.setdefault(k, r[k])
        # count the ones left without a var
        extra += 0 if seat.get("var") else 1

    out = sorted(by_config.values(), key=lambda s: (s.get("title") or s["questConfig"]).lower())
    return out, extra


def main(argv):
    dest = argv[argv.index("-o") + 1] if "-o" in argv else OUT
    if not os.path.exists(VARS) and not os.path.exists(CONFIGS):
        print("no harvest in %s; this script is for refreshing the shipped file" % API_INFO)
        return 1
    out, extra = build()
    with io.open(dest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"rows": out}, fh, separators=(",", ":"), sort_keys=True)
    print("%d quests -> %s (%d KB), %d of them with no var recorded"
          % (len(out), dest, os.path.getsize(dest) // 1024, extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
