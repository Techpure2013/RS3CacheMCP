-- Two mistakes the client only reports at runtime, and three shapes that must not be reported.
--
-- Event.Tick is invented. It reads as plausible because the game has ticks, and it took a pasted
-- client stack trace to find. json is a per-plugin vendored local rather than a global, so calling
-- it without requiring it is nil at the call, not at load.

local Interface = ui.Interface

-- Real, and named in the docs, though only one installed plugin uses it.
Event.InventoryUpdated.Subscribe("fixture", function() end)

-- Invented. Nothing declares it and nothing else calls it.
Event.Tick.Subscribe("fixture", function() end)

-- Data, not a call. The require check read a path like this as a module once.
local vendored = { here = "json.lua", there = "shared/json.lua" }

local function send(rows)
    -- Nothing required json, so this is nil at runtime.
    return json.encode(rows)
end

local function draw(parent)
    -- A local alias of a client namespace stays the plugin's own.
    return Interface.new(parent)
end

return { send = send, draw = draw, vendored = vendored }
