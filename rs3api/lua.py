"""
Writing lua for this client: what the sandbox is, the traps that cost real time, and working code
pulled out of plugins that actually run.

The recipes here are all taken from installed plugins that were watched working in the client, not
written from the docs. Where a recipe disagrees with the documentation, the
recipe is the one that ran.

Tools:
  rs3_lua          notes and recipes by topic: the sandbox, the traps, how to do a given thing
  rs3_lua_example  every real use of a symbol across the plugin tree, with its surrounding lines
"""

import io
import os
import re

from .common import PLUGINS

PLUGIN_ROOTS = [PLUGINS]

SANDBOX = """\
The sandbox, and how it differs from stock lua:

- Lua 5.4 is the target. The box here has 5.5, so `luac -p` compiles a file that the client might
  still refuse; it catches syntax, not version drift.
- `os` is absent. So is io. Anything wanting a clock uses the tick events.
- `require` takes a path, not a module name: require("core/store"), require("engine/ui"). It is
  relative to the plugin folder. loadfile behaves require-like.
- `log(...)` is the print. It goes to the developer console.
- Globals the client injects: Plugin, Event, ui, id, config, log, and one global per required
  dependency, named after the dependency folder. prettyui appears as `prettyui` only when the
  manifest asks for it.
- A plugin.json change needs `plugins_refresh`. `prl <name>` reloads lua only and keeps the old
  manifest, so a newly added dependency is silently not there.
- Resources under resources/ appear as Plugin.resources, keyed by base name without the extension,
  nested by folder. No manifest entry is needed for them.
"""

TRAPS = [
    {
        "topic": "truthiness",
        "trap": "0 and the empty string are truthy. Only nil and false are falsy.",
        "cost": "`hull.rest or DEFAULT` takes a stored 0 instead of the default. A field meaning "
                "'not measured' has to be absent, not zero.",
    },
    {
        "topic": "pcall, native calls",
        "trap": "pcall only catches lua errors. A nil reaching a native argument takes the client "
                "down and pcall cannot see it coming.",
        "cost": "Vector3.new(x, nil, z) from an entity that walked out of range mid frame. Check "
                "types before the call, not around it.",
    },
    {
        "topic": "pcall, property writes",
        "trap": "pcall returning true does not mean the write landed. sol accepts a value it "
                "cannot convert and drops it silently.",
        "cost": "A sprite reported write=true for hours while binding nothing. Read the value back;"
                " that is the only honest test.",
    },
    {
        "topic": "entity handles",
        "trap": "An entity handle must not outlive the frame it was handed out in.",
        "cost": "The client frees it, and isValid is no defence because reading isValid is itself "
                "the read that faults. Store a uid, or a tile and level, and look it up again.",
    },
    {
        "topic": "NaN",
        "trap": "math.floor(NaN) stays a float in 5.4, with no integer to convert to.",
        "cost": "Feeding a projected coordinate to a component position crashes. Test with "
                "`v == v`, which is false only for NaN.",
    },
    {
        "topic": "averaging angles",
        "trap": "The arithmetic mean of two angles is halfway round the wrong side as often as not.",
        "cost": "2047 and 1 are neighbours in a 2048 turn but average to 1024. Sum unit vectors "
                "and take atan2 of the total instead.",
    },
    {
        "topic": "string.format",
        "trap": "A %d with a float argument errors in 5.4 rather than truncating.",
        "cost": "Anything read off the client can come back a float even when it looks integral. "
                "Use %s with tostring for diagnostics.",
    },
    {
        "topic": "typings",
        "trap": "A class's own block in the typings is only half its surface. The base class holds "
                "the rest and these files do not repeat inherited members.",
        "cost": "Concluding ui.Sprite has no alpha, when it inherits alpha, rgba and colour from "
                "Component.",
    },
    {
        "topic": "generated ids",
        "trap": "Component category and dynamic ids are limited to 0 to 256, and hand picked ids "
                "collide with the engine's own allocator once components have come and gone.",
        "cost": "Let ids auto assign. Use a distinct category per panel.",
    },
    {
        "topic": "recursive self decoration",
        "trap": "A walk over an interface finds components the plugin itself added, since they "
                "carry the same fields the walk keys on.",
        "cost": "Nested duplicates, wrong slot counts, and creation caps hit for no reason. Keep a "
                "set of what you made, keyed the same way the walk keys things.",
    },
]

RECIPES = [
    {
        "name": "safe read",
        "about": "Every read off a client object, wrapped. A validity check taken beforehand can "
                 "be stale by the time the read happens, so the read itself is what gets wrapped.",
        "code": '''local function safe(fn)
  local ok, v = pcall(fn)
  if ok then return v end
  return nil
end

---A number or nothing, for anything heading into a native call.
local function num(fn)
  local v = safe(fn)
  if type(v) == "number" then return v end
  return nil
end

local x = num(function() return entity.aabb.minX end)''',
    },
    {
        "name": "prettyui window and ribbon",
        "about": "Window.new takes the collection to live in first, then its options. The game "
                 "area is the usual host. SimpleButton has no SetText; FancyButton does.",
        "code": '''if prettyui == nil then
  log("prettyui is not loaded, check plugin.json and run plugins_refresh")
  return
end

local ui_ = {}

local function buildPanel()
  local gameArea = ui.Interfaces:GetComponent(id.Component.TOPLEVEL_V2__GAME_AREA)
  if gameArea == nil then return nil end
  local w = prettyui.Window.new(gameArea, { title = "Panel", width = 300, x = 40, y = 80 })
  if w == nil then return nil end

  w:AddText("What this does.")
  w:AddFancyButton("Off", function(btn)
    btn:SetText("On")
    btn:SetVariant("positive")
  end, { variant = "neutral", width = 130 })
  w:AddSimpleButton("A toggle", function(_, active) end, { toggleable = true, width = 170 })
  w:AddDivider()
  ui_.readout = w:AddText({ text = "-", colour = 0xAFA27FFF })
  return w
end

ui_.ribbon = prettyui.RibbonBar.Register({
  id = "my-plugin",
  object = config.Obj.COINS,
  tooltip = "My Plugin",
  onClick = function(handle, active)
    if ui_.window == nil then ui_.window = buildPanel() end
    if ui_.window == nil then handle:SetActive(false) return end
    if active then ui_.window:Show() else ui_.window:Close() end
  end,
})''',
    },
    {
        "name": "events",
        "about": "Logic is the game tick and carries logicTick, not tick. Draw is per frame and is "
                 "where immediate mode drawing goes. MiniMenuOpened is a ready made picker: its "
                 "entries carry npcData, playerData, objData and locData.",
        "code": '''Event.Logic.Subscribe("myTick", function(e)
  if e and type(e.logicTick) == "number" and e.logicTick % 5 == 0 then
    -- every five ticks
  end
end)

Event.Draw.Subscribe("myDraw", function()
  -- per frame. Do not create components in here.
end)

Event.MiniMenuOpened.Subscribe("myPick", function(e)
  if e == nil or e.miniMenu == nil then return end
  for _, entry in ipairs(e.miniMenu.entries) do
    local npc = safe(function() return entry.npcData end)
    if npc ~= nil then
      -- keep npc.entity.uid, never the entity itself
      break
    end
  end
end)''',
    },
    {
        "name": "world drawing",
        "about": "ShapeList draws lines and triangles in world space. 512 units is one tile, "
                 "indices are 0 based, and lines are a flat list of pairs rather than tuples. "
                 "ignoreDepth draws through walls.",
        "code": '''local data = ShapeData.new()
data.positions = { Vector3.new(x0, y, z0), Vector3.new(x1, y, z0),
                   Vector3.new(x1, y, z1), Vector3.new(x0, y, z1) }
data.lines = { 0, 1, 1, 2, 2, 3, 3, 0 }

ShapeList.Draw({
  shapeData = data,
  colour = 0x33FF66FF,   -- packed 0xRRGGBBAA, alpha is the last byte
  lineWidth = 1,
  ignoreDepth = true,
})''',
    },
    {
        "name": "walk an interface",
        "about": "Budgeted, because an interface can be enormous and this usually runs on a tick. "
                 "Skip anything the plugin itself added, or the walk decorates its own work.",
        "code": '''local MAX_COMPS, MAX_STATIC, MAX_DEPTH = 600, 200, 6

local function walk(interfaceId, visit)
  local root = ui.Interfaces:GetInterface(interfaceId)
  if root == nil then return false end
  local budget = MAX_COMPS
  local recurse
  recurse = function(node, depth)
    if depth > MAX_DEPTH or budget <= 0 then return end
    local function step(child)
      if budget <= 0 then return true end
      budget = budget - 1
      visit(child, node)
      recurse(child, depth + 1)
      return budget <= 0
    end
    pcall(function() node:IterateStaticComponents(step) end)
    pcall(function() node:IterateDynamicComponents(step) end)
  end
  for staticId = 0, MAX_STATIC do
    if budget <= 0 then break end
    local comp = nil
    pcall(function() comp = root:GetComponent(staticId) end)
    if comp ~= nil then
      budget = budget - 1
      visit(comp, nil)
      recurse(comp, 1)
    end
  end
  return true
end''',
    },
    {
        "name": "plugin resources",
        "about": "A png under resources/ arrives as a PluginResource, keyed by base name and "
                 "nested by folder. It is assigned straight to a Sprite; no Load call is needed. "
                 "Note the standing problem: this draws in a plugin's own overlay but not in an "
                 "interface the client owns.",
        "code": '''local art = Plugin.resources.sprites.myfolder.myicon
local sprite = ui.Sprite.new(parent)
sprite:SetSize(36, 32)
sprite:MoveToFront()
sprite.sprite = art
-- spriteID stays -1; runtimeSprite is what says whether it bound''',
    },
]


def _lua_files():
    out = []
    for root in PLUGIN_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "dist")]
            for n in names:
                if n.endswith(".lua"):
                    out.append(os.path.join(dirpath, n))
    return sorted(out)


def tool_lua(args):
    topic = (args.get("topic") or "").strip().lower()
    if not topic:
        return {"sandbox": SANDBOX,
                "traps": [t["topic"] for t in TRAPS],
                "recipes": [r["name"] for r in RECIPES],
                "note": "pass a topic to get the detail, or a recipe name for its code"}

    traps = [t for t in TRAPS if topic in t["topic"].lower() or topic in t["trap"].lower()
             or topic in t["cost"].lower()]
    recipes = [r for r in RECIPES if topic in r["name"].lower() or topic in r["about"].lower()
               or topic in r["code"].lower()]
    out = {"traps": traps, "recipes": recipes}
    if topic in ("sandbox", "syntax", "require", "globals"):
        out["sandbox"] = SANDBOX
    if not traps and not recipes and "sandbox" not in out:
        out["note"] = ("nothing on that. traps: " + ", ".join(t["topic"] for t in TRAPS)
                       + ". recipes: " + ", ".join(r["name"] for r in RECIPES))
    return out


def tool_lua_example(args):
    sym = (args.get("symbol") or "").strip()
    if not sym:
        raise ValueError("give a symbol, e.g. ShapeList.Draw or MoveToFront")
    limit = min(int(args.get("limit", 12)), 60)
    context = min(int(args.get("context", 3)), 12)
    pat = re.compile(re.escape(sym))

    hits = []
    for path in _lua_files():
        try:
            lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if pat.search(line):
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                hits.append({
                    "file": path.replace("\\", "/"),
                    "line": i + 1,
                    "plugin": path.replace("\\", "/").split("/RS3Plugins/")[-1].split("/")[0],
                    "code": "\n".join(lines[lo:hi]),
                })
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    plugins = sorted({h["plugin"] for h in hits})
    return {"found": len(hits), "plugins": plugins, "examples": hits,
            "note": "these are lines from plugins that run, so where one disagrees with the "
                    "typings the code is the one that was observed working"}


TOOLS = [
    {"name": "rs3_lua", "fn": tool_lua,
     "description": "Writing lua for the RS client: what the sandbox provides and withholds, the "
                    "traps that have actually cost time, and working recipes for windows, events, "
                    "world drawing, walking an interface and plugin resources. Call with no topic "
                    "for the index.",
     "inputSchema": {"type": "object", "properties": {
         "topic": {"type": "string", "description": "e.g. sandbox, pcall, events, window, "
                                                    "drawing, resources, truthiness"}}}},
    {"name": "rs3_lua_example", "fn": tool_lua_example,
     "description": "Every real use of a symbol across the installed plugins, with surrounding "
                    "lines. Use it to see how something is actually called before calling it.",
     "inputSchema": {"type": "object", "required": ["symbol"], "properties": {
         "symbol": {"type": "string", "description": "e.g. ShapeList.Draw, RibbonBar.Register, aabb"},
         "limit": {"type": "integer", "description": "max examples, default 12"},
         "context": {"type": "integer", "description": "lines either side, default 3"}}}},
]
