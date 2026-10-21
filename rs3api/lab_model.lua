-- The lab's client, answering only from what the live client was measured doing. Loaded by
-- lab_runner.lua when a run asks for the model; see clientmodel.py for where each answer comes from.
--
-- A call is sorted by its first argument into one of the kinds qd groundtruth tried: none, nil, a
-- number, a string, a boolean, a table. A number is one of four: negative, zero, huge, or an
-- ordinary id. An ordinary id to a FromID reads the harvest; anything else gets the answer that was
-- measured for its kind. A call with no measurement stops the run as a model gap, and a call that
-- took the client down stops it as a client crash, since the plugin would have gone with it.

return function(G, M, out, harvestDir, stop, lab, listGaps)
  -- In the client config.X.FromID is a userdata with __call. Pure lua can't make one, so here it is
  -- a table with __call, and type() says so; said in every report so it is never a quiet difference.
  local report = { answered = 0, gaps = {}, crashes = {}, notInClient = {},
                   differences = { "type(config.X.FromID) is \"table\" here, \"userdata\" in the client" } }
  out.model = report

  -- answers worked out rather than measured: named once each, so a run says what it leaned on
  local function derived(what)
    report.derived = report.derived or {}
    for _, d in ipairs(report.derived) do
      if d == what then return end
    end
    report.derived[#report.derived + 1] = what
  end

  -- Listing, the run carries on past a gap with nil, so one run names everything a plugin reads
  -- that the model can't answer. That run is not a verdict on the plugin, and says so.
  local listed = {}
  local function gap(what)
    if listGaps then
      if not listed[what] then
        listed[what] = true
        report.gaps[#report.gaps + 1] = what
      end
      report.verdict = "none: gaps were listed and read as nil, so what the plugin did after them is not the client's"
      return nil
    end
    report.gaps[#report.gaps + 1] = what
    stop("model gap: " .. what)
  end

  local function notInClient(path)
    if report.notInClient[path] == nil then report.notInClient[path] = true end
  end

  -- the harvest, a shard at a time
  local shards = {}
  local function harvested(ns, id)
    local name = M.harvested[ns]
    if name == nil then return nil, false end
    local n = id // 1000
    local key = name .. "/" .. n
    if shards[key] == nil then
      local fn = loadfile(harvestDir .. "/" .. name .. "/" .. n .. ".lua", "t", {})
      shards[key] = fn and fn() or false
    end
    return shards[key] and shards[key][id] or nil, true
  end

  -- things the model handed out, so passing one back to a call is known for what it is
  local handedOut = setmetatable({}, { __mode = "k" })

  -- a thing the client returned: fields it was measured to have, values from the harvest row or
  -- from what the walk read off it
  local function object(path, fields, row, measured)
    local obj = {}
    -- by its client type: ConfigTypeProxy<jag::game::InvType,...> is an InvType, and
    -- sol.jag::game::CoordGrid is a CoordGrid
    handedOut[obj] = path:match("::(%w+)[,>]") or path:match("::(%w+)%**$") or path
    return setmetatable(obj, {
      __name = path,
      __index = function(_, k)
        if row ~= nil and row[k] ~= nil then return row[k] end
        if measured ~= nil and measured[k] ~= nil then return measured[k] end
        local t = fields and fields[k]
        if t == nil then
          notInClient(path .. "." .. tostring(k))
          return nil
        end
        if t == "nil" then return nil end
        if t == "function" then
          return function() gap(path .. ":" .. tostring(k) .. "() has not been measured") end
        end
        gap(path .. "." .. tostring(k) .. " is a " .. tostring(t) .. " with no measured value")
      end,
      __newindex = function(_, k)
        gap("writing " .. path .. "." .. tostring(k) .. " has not been measured")
      end,
    })
  end

  local function kind(n, v)
    if n == 0 then return "()" end
    local t = type(v)
    if t == "nil" then return "(nil)" end
    if t == "number" then
      -- a fraction is its own kind: ui takes colour channels and ratios, and what the client does
      -- with 0.78 is not what it does with a whole number
      if math.type(v) == "float" and v ~= math.floor(v) then return "(0.5)" end
      if v < 0 then return "(-1)" end
      if v == 0 then return "(0)" end
      if v >= 99999999 then return "(99999999)" end
      return "id"
    end
    if t == "string" then return '("qdev")' end
    if t == "boolean" then return "(true)" end
    -- an object the client made is its own kind, by type: treating one as {} would claim
    -- Inventory.Find(config.Inv.INV) throws, which is not what was measured
    if handedOut[v] then return "(" .. handedOut[v] .. ")" end
    -- only an empty {} was ever passed; a filter with something in it is its own kind
    if type(v) == "table" and next(v) ~= nil then return "(a filled table)" end
    return "({})"
  end

  local function answer(api, fact, ns, id)
    -- raised as measured, with no position: the client's message names no lua file
    if fact.o == "throws" then error(fact.m, 0) end
    if fact.o == "crashed" then
      report.crashes[#report.crashes + 1] = api
      stop("client crash: " .. api)
    end
    if fact.o == "skipped" then return gap(api .. " was never tried: " .. fact.m) end
    report.answered = report.answered + 1
    if fact.t == "none" then return end
    if fact.t == "nil" then return nil end
    if fact.fields then
      local row = id ~= nil and harvested(ns, id) or nil
      return object(fact.meta or api, fact.fields, row, fact.values)
    end
    if fact.v ~= nil then return fact.v end
    gap(api .. " returns a " .. tostring(fact.t) .. " with no measured value")
  end

  -- The world: what a probe says the game holds right now, eg what is in the backpack. It fills in
  -- the values of a call that was measured to answer, and nothing else: a wrong argument still
  -- throws and a crash still crashes, as measured, before the world is asked.
  local worldAnswers = {}

  -- an object the world gave: reads only the fields its shape has, and only the values given
  local function worldObject(name, fields, values)
    local obj = {}
    handedOut[obj] = name:match("::(%w+)[,>]") or name:match("::(%w+)%**$") or name
    return setmetatable(obj, {
      __name = name,
      __index = function(_, k)
        local v = values[k]
        if v ~= nil then return v end
        if fields[k] == nil then
          notInClient(name .. "." .. tostring(k))
          return nil
        end
        return gap(name .. "." .. tostring(k) .. " was read but the world didn't give it")
      end,
      __newindex = function(_, k)
        gap("writing " .. name .. "." .. tostring(k) .. " has not been measured")
      end,
    })
  end

  -- named here, defined below: dispatch and the namespaces both reach for them
  local answerByName, idTable, idReverse

  -- CoordGrid.new(packed) unpacks level<<28 | x<<14 | z. Measured twice: new(0) came back all
  -- zeroes, and the tile the run passes as an argument (Lumbridge) came back x 3222, z 3218,
  -- localX 22, mapSquareX 50. Every tile between was never tried, so this is worked out, not read.
  local function packedCoord(fact, packed)
    derived("CoordGrid.new(packed), unpacked as level<<28 | x<<14 | z, as both measured coords were")
    local x, z = (packed >> 14) & 0x3FFF, packed & 0x3FFF
    return object("sol.jag::game::CoordGrid", fact and fact.fields, nil,
      { x = x, z = z, level = packed >> 28, localX = x % 64, localZ = z % 64,
        mapSquareX = x // 64, mapSquareZ = z // 64, valid = true })
  end

  local function dispatch(api, owner, ns, ...)
    local how, n, first = ".", select("#", ...), (...)
    if n >= 1 and first == owner then
      how = ":"
      n, first = n - 1, select(2, ...)
    end
    local byHow = M.calls[api][how]
    -- id.Quest.FromID(6) is the same client list read the other way round, for any id
    local idType = api:match("^id%.(%w+)%.FromID$")
    if idType ~= nil and math.type(first) == "integer" and idTable(idType) ~= nil then
      derived("id.<type>.FromID(id), the client's name list read the other way")
      report.answered = report.answered + 1
      return idReverse(idType)[first]
    end
    local k = kind(n, first)
    local harvestedFromID = api:match("%.FromID$") ~= nil and M.harvested[ns] ~= nil
    -- any id from 0 up goes to the harvest, which has 0's real row and answers nil for an id it
    -- doesn't have, as FromID(99999999) was measured to
    if harvestedFromID and math.type(first) == "integer" and first >= 0 then k = "id" end
    -- A filled table: what LocList.Filter({ ... }) finds is the world's, and {} was measured empty.
    -- The {} answer says a table is taken, so the world may answer it; nothing else may.
    if k == "(a filled table)" then
      if worldAnswers[api] == nil then
        return gap(api .. how .. "(a filled table): only an empty {} was measured")
      end
      k = "({})"
    end
    if k == "id" then
      -- an ordinary id: the harvest knows whether it exists; without one, answer as for 0
      if harvestedFromID then
        local row = harvested(ns, first)
        if row == nil then
          report.answered = report.answered + 1
          return nil
        end
        return answer(api .. how .. "(id)", byHow and byHow["(0)"] or { o = "returns", t = "table" }, ns, first)
      end
      -- Only 0, -1 and 99999999 were tried. Answering 1530 as 0 was measured to answer is a guess,
      -- and for a lookup like LocList it's wrong: what is there depends on the world. The 0 answer
      -- still says a number is taken, so the world may answer it; nothing else may.
      if api == "CoordGrid.new" and math.type(first) == "integer" then
        report.answered = report.answered + 1
        return packedCoord(byHow and byHow["(0)"], first)
      end
      if worldAnswers[api] == nil then
        return gap(api .. how .. "(" .. tostring(first) .. "): only 0, -1 and 99999999 were measured")
      end
      k = "(0)"
    end
    local fact = byHow and byHow[k]
    if fact == nil then
      return gap(api .. how .. k .. " has not been measured")
    end
    -- Only one argument was ever passed, so an answer measured that way says nothing about a call
    -- with more: formatStr(fmt, a, b) would come back as the one string measured for ("qdev"), and
    -- Quest_Helper's cache keys all collapsed into one. A probe can answer it through lab.world.
    if n > 1 and fact.o == "returns" and worldAnswers[api] == nil then
      return gap(api .. how .. " with " .. n .. " arguments: only one was ever measured")
    end
    if fact.o == "returns" and worldAnswers[api] then
      report.answered = report.answered + 1
      report.fromWorld = (report.fromWorld or 0) + 1
      local v = worldAnswers[api](select(how == ":" and 2 or 1, ...))
      -- a plain table from the world is checked against the shape this call was measured to return
      -- a map of values is checked against the shape this call was measured to return; a list
      -- (what the world holds, eg the npcs around you) is handed back as it is
      if type(v) == "table" and getmetatable(v) == nil and fact.fields and next(fact.fields) ~= nil
         and v[1] == nil then
        v = worldObject(fact.meta or api, fact.fields, v)
      end
      return v
    end
    return answer(api .. how .. k, fact, ns, nil)
  end

  -- the harvest's names, a type at a time: config.Inv.INV is the row whose configName is INV
  local nameIndex = {}
  local function byName(ns, name)
    local file = M.harvested[ns]
    if file == nil then return nil, false end
    if nameIndex[file] == nil then
      local fn = loadfile(harvestDir .. "/" .. file .. "/names.lua", "t", {})
      nameIndex[file] = fn and fn() or false
    end
    return nameIndex[file] and nameIndex[file][name] or nil, true
  end


  -- id.<Type>: the names the client has for that type, from its own lists. `id` is a namespace of
  -- calls as well (id.Quest.FromID), so this is reached both ways.
  local idLists = {}
  function idTable(typeName)
    if idLists[typeName] ~= nil then return idLists[typeName] or nil end
    local ns = "config." .. tostring(typeName)
    local list
    if M.harvested[ns] == nil then
      if not (M.idTypes and M.idTypes[typeName]) then
        idLists[typeName] = false
        return nil
      end
      local fn = loadfile(M.idsDir .. "/" .. tostring(typeName) .. ".lua", "t", {})
      list = fn and fn() or {}
    end
    local names = setmetatable({}, { __index = function(_, name)
      -- Only an ALL_CAPS name a complete list lacks is known to be absent. Anything else here,
      -- like id.Quest.FromID, which the typings leave out but Quest_Helper calls, is unmeasured.
      if type(name) ~= "string" or not name:match("^[%u%d_]+$") then
        return gap("id." .. tostring(typeName) .. "." .. tostring(name) .. " has never been measured")
      end
      derived("id.<type>.<NAME>, from the client's name lists")
      local found
      if list then found = list[name] else found = byName(ns, name) end
      if found == nil then notInClient("id." .. tostring(typeName) .. "." .. tostring(name)) end
      return found
    end })
    idLists[typeName] = names
    return names
  end

  -- the same list by id, for FromID
  local reversed = {}
  function idReverse(typeName)
    if reversed[typeName] == nil then
      local byId = {}
      local ns = "config." .. tostring(typeName)
      local list
      if M.harvested[ns] ~= nil then
        local fn = loadfile(harvestDir .. "/" .. M.harvested[ns] .. "/names.lua", "t", {})
        list = fn and fn() or {}
      else
        local fn = loadfile(M.idsDir .. "/" .. tostring(typeName) .. ".lua", "t", {})
        list = fn and fn() or {}
      end
      for name, id in pairs(list) do byId[id] = name end
      reversed[typeName] = byId
    end
    return reversed[typeName]
  end

  -- a namespace: what the model knows, and nil for the rest, as the client gives
  local function namespace(path)
    return setmetatable({}, { __index = function(_, k)
      if path == "id" then
        local made = idTable(k)
        if made ~= nil then return made end
      end
      -- id.Obj and the rest are namespaces of their own here, since id.Obj.FromID is a call, so
      -- the names have to be reachable through them as well as through id itself
      local idType = path:match("^id%.(%w+)$")
      if idType ~= nil then
        local names = idTable(idType)
        if names ~= nil then return names[k] end
      end
      if type(k) == "string" and k:match("^[%u%d_]+$") and M.harvested[path] then
        local id, known = byName(path, k)
        if known then
          -- measured by qd throwprobe: a name the client doesn't have is nil
          if id == nil then return nil end
          return answerByName(path, id)
        end
      end
      -- Quest_Helper indexes config.Quest with a name that may be nil and lets a pcall catch it,
      -- so what the client does with a key that isn't a name is something a plugin leans on and
      -- nobody has measured
      if type(k) ~= "string" then
        local label = k == nil and "[nil]" or (type(k) == "number" and "[0]" or nil)
        local fact = label ~= nil and (M.keys or {})[path] and M.keys[path][label] or nil
        if fact ~= nil then return answer("index " .. path .. label, fact, nil, nil) end
        return gap("indexing " .. path .. " with a " .. type(k) .. " key has never been measured")
      end
      -- the walk saw it, so it is there; it just hasn't been measured
      if M.exists and M.exists[path .. "." .. tostring(k)] then
        return gap(path .. "." .. tostring(k) .. " is in the client but has never been measured")
      end
      notInClient(path .. "." .. tostring(k))
      return nil
    end })
  end

  local function place(api)
    local parts = {}
    for p in api:gmatch("[^%.]+") do parts[#parts + 1] = p end
    local here, path = G, nil
    for i = 1, #parts - 1 do
      path = path and (path .. "." .. parts[i]) or parts[i]
      local child = rawget(here, parts[i])
      if child == nil then
        child = namespace(path)
        rawset(here, parts[i], child)
      elseif type(child) ~= "table" then
        return
      end
      here = child
    end
    local owner, ns, name = here, path, parts[#parts]
    if rawget(owner, name) ~= nil then return end
    if name == "FromID" and ns and ns:match("^config%.") then
      -- in the client these are userdata with __call, not functions
      rawset(owner, name, setmetatable({}, {
        __name = api,
        __call = function(_, ...) return dispatch(api, owner, ns, ...) end,
      }))
    else
      rawset(owner, name, function(...) return dispatch(api, owner, ns, ...) end)
    end
  end

  -- the same object FromID gives for that id
  answerByName = function(ns, id)
    local api = ns .. ".FromID"
    local byHow = M.calls[api] and M.calls[api]["."]
    local fact = byHow and byHow["(0)"]
    if fact == nil then return gap(ns .. " names need " .. api .. "(0) measured") end
    return answer(api .. "(name)", fact, ns, id)
  end

  local names = {}
  for api in pairs(M.calls) do names[#names + 1] = api end
  table.sort(names)
  for _, api in ipairs(names) do place(api) end
  report.calls = #names

  -- the constants, as the walk read them: SkillType.ATTACK is 0
  local values = 0
  for path, v in pairs(M.values or {}) do
    local ns, name = path:match("^(.*)%.([^%.]+)$")
    if ns then
      local here = G
      for part in ns:gmatch("[^%.]+") do
        local child = rawget(here, part)
        if child == nil then
          child = namespace(ns)
          rawset(here, part, child)
        end
        if type(child) ~= "table" then here = nil break end
        here = child
      end
      if here ~= nil and rawget(here, name) == nil then
        rawset(here, name, v)
        values = values + 1
      end
    end
  end
  report.values = values

  -- Events. A payload has the fields its event was measured to have, with the values the probe
  -- gives. The client recorded types, not values, so a field the plugin reads that the probe left
  -- out is a gap that names it: what a handler depends on is learnt, never guessed.
  local liveness = setmetatable({}, { __mode = "k" })

  local function strict(what, meta, fields, given, isEntity)
    local obj = {}
    liveness[obj] = isEntity and "live" or nil
    return setmetatable(obj, {
      __name = meta or what,
      __index = function(_, k)
        local goneShape = liveness[obj] == "gone" and M.gone[isEntity]
        if goneShape then
          -- measured: once gone, isValid is false and the rest reads what the gone shape says
          if k == "isValid" then return false end
          local t = goneShape.fields[k]
          if t == nil then notInClient(what .. "." .. tostring(k)) return nil end
          if t == "nil" then return nil end
          return gap(what .. "." .. tostring(k) .. " on a gone entity has not been measured")
        end
        if given[k] ~= nil then return given[k] end
        if isEntity and k == "isValid" then return true end
        local t = fields[k]
        -- A live entity's field list comes from the gone shape, where every field read nil. That
        -- nil is what a gone one says, not a live one, so here the list gives names and nothing more.
        if isEntity and t ~= nil and k ~= "new" and k ~= "class_cast" and k ~= "class_check" then
          return gap(what .. "." .. tostring(k) .. " was read but the probe didn't give it")
        end
        if t == nil then notInClient(what .. "." .. tostring(k)) return nil end
        if t == "nil" then return nil end
        if k == "new" or k == "class_cast" or k == "class_check" then
          return gap(what .. "." .. tostring(k) .. " has not been measured")
        end
        gap(what .. "." .. tostring(k) .. " (a " .. tostring(t) .. ") was read but the probe didn't give it")
      end,
      __newindex = function(_, k)
        gap("writing " .. what .. "." .. tostring(k) .. " has not been measured")
      end,
    })
  end

  ---An entity for a payload field, eg lab.entity("npcEntity", { configID = 49, displayName = "Cow" }).
  function lab.entity(field, values)
    local shape = M.gone[field]
    if shape == nil then return gap("no measured entity for a payload's " .. tostring(field)) end
    return strict(field, shape.meta, shape.fields, values or {}, field)
  end

  ---What it reads once the client has let it go, as measured on a gone NPC.
  function lab.gone(entity)
    if liveness[entity] == nil then error("lab.gone: not an entity from lab.entity", 2) end
    liveness[entity] = "gone"
  end

  function lab.payload(eventName, values)
    local shape = M.events[eventName]
    if shape == nil then
      return gap("Event." .. tostring(eventName) .. " has never been recorded firing; play until it does, with qd groundtruth running")
    end
    if shape.t == "nil" then return nil end
    local given = {}
    for k, v in pairs(values or {}) do
      if type(v) == "table" and getmetatable(v) == nil and shape.fields[k] == "userdata" and M.gone[k] then
        v = lab.entity(k, v)
      end
      given[k] = v
    end
    return strict("Event." .. eventName, shape.meta, shape.fields, given, nil)
  end

  -- every payload the probe fires goes through the measured shape
  local rawFire = lab.fire
  function lab.fire(eventName, payload)
    if M.events[eventName] == nil and listGaps then
      gap("Event." .. tostring(eventName) .. " has never been recorded firing; not fired")
      return
    end
    local already = type(payload) == "table" and getmetatable(payload) ~= nil
    return rawFire(eventName, already and payload or lab.payload(eventName, payload))
  end
  report.events = 0
  for _ in pairs(M.events or {}) do report.events = report.events + 1 end

  -- id.Obj.COINS is the number for the row the client calls COINS, and id.Component's names come
  -- from `qd iddump`. They sit behind a C __index like config's, so the walk never read them.
  if rawget(G, "id") == nil then
    rawset(G, "id", setmetatable({}, { __index = function(ids, typeName)
      local made = idTable(typeName)
      if made == nil then
        return gap("id." .. tostring(typeName) .. " names are not in the model (no harvest or name list)")
      end
      rawset(ids, typeName, made)
      return made
    end }))
  end

  -- var.VarPlayer.QUEST_POINTS and the like. The names and ids come from the client's own lists,
  -- and what a var object holds was measured on the two the walk was told to read. Only the name
  -- and the id are known for any other var: its type differs, and its value is the world's, so
  -- both are gaps unless a probe gives them with lab.world.vars.
  local varValues = {}
  local varLists = {}
  local function varList(typeName)
    if varLists[typeName] == nil then
      local fn = M.varsDir and loadfile(M.varsDir .. "/" .. typeName .. ".lua", "t", {})
      varLists[typeName] = fn and fn() or false
    end
    return varLists[typeName] or {}
  end
  -- Every var table's members are the same client type (sol.jag::VarProxy), and the walk records a
  -- second object of a type it has already seen as that type rather than reading it again. So one
  -- var read anywhere is the shape for all of them, which the report says when it is used.
  local function varSample(typeName)
    local shapes = M.shapes or {}
    for path, shape in pairs(shapes) do
      if path:sub(1, #typeName + 5) == "var." .. typeName .. "." then return shape end
    end
    for path, shape in pairs(shapes) do
      if path:sub(1, 4) == "var." then
        derived("the shape of a var, from one read under another var table: they are all VarProxy")
        return shape
      end
    end
    return nil
  end

  if rawget(G, "var") == nil then
    rawset(G, "var", setmetatable({}, { __index = function(vars, typeName)
      if not (M.idTypes and M.idTypes[typeName]) then
        return gap("var." .. tostring(typeName) .. " names are not in the model (no name list)")
      end
      local sample = varSample(typeName)
      if sample == nil then
        return gap("no var of " .. tostring(typeName) .. " has ever been read, so the model has no shape for one")
      end
      local names = setmetatable({}, { __index = function(_, name)
        if type(name) ~= "string" or not name:match("^[%u%d_]+$") then
          return gap("var." .. tostring(typeName) .. "." .. tostring(name) .. " has never been measured")
        end
        derived("var.<type>.<NAME>, from the client's own var lists; its value is the world's")
        local info = varList(typeName)[name]
        if info == nil then
          notInClient("var." .. typeName .. "." .. name)
          return nil
        end
        -- everything the client's list knows: the id, the value type, and for a varbit its mask
        -- and the var it sits in. What it holds right now is not in any list.
        local values = { configName = name, id = info.id, typeName = info.typeName,
                         valueType = info.valueType, isVarBit = info.varBitMask ~= nil,
                         varBitMask = info.varBitMask }
        if info.baseVar ~= nil then
          values.baseVar = worldObject(sample.meta or "VarProxy", sample.fields,
            { configName = info.baseVar, typeName = info.typeName })
        end
        for k, v in pairs(varValues[name] or {}) do values[k] = v end
        return worldObject(sample.meta or ("var." .. typeName), sample.fields, values)
      end })
      rawset(vars, typeName, names)
      return names
    end }))
  end

  lab.world = {}

  -- what the probe set, so a run says which world it passed in and nobody reads a pass as the
  -- client's own answer
  local function setUp(what)
    report.world = report.world or {}
    for _, w in ipairs(report.world) do
      if w == what then return end
    end
    report.world[#report.world + 1] = what
  end

  ---A tile: lab.world.at(3222, 3218) or lab.world.at(3222, 3218, 1).
  function lab.world.at(x, z, level)
    return G.CoordGrid.new(((level or 0) << 28) | ((x or 0) << 14) | (z or 0))
  end

  ---Interfaces that are open, and what is in them:
  ---  lab.world.interfaces({ [1184] = { components = { [10] = { text = "Hello", visible = true } } } })
  ---An interface the probe didn't name is closed, which is what GetInterface was measured to say
  ---for one that isn't there: nil.
  function lab.world.interfaces(byId)
    setUp("interfaces")
    local made = {}
    for iid, spec in pairs(byId or {}) do
      local comps, order = {}, {}
      for cid, values in pairs((spec or {}).components or {}) do
        local v = {}
        for k, x in pairs(values) do v[k] = x end
        v.interfaceID, v.staticID = v.interfaceID or iid, v.staticID or cid
        -- a walk over an interface asks each component for its children; a probe that gave none
        -- has none, which is not the same as the client refusing to say
        local kids = v.children or {}
        v.children = nil
        local comp
        local function iterate(_, fn)
          for _, kid in ipairs(kids) do
            if fn(kid) == true then return end
          end
        end
        v.IterateStaticComponents = v.IterateStaticComponents or iterate
        v.IterateDynamicComponents = v.IterateDynamicComponents or function() end
        comp = lab.world.entity("Component", v)
        comps[cid] = comp
        order[#order + 1] = cid
      end
      table.sort(order)
      made[iid] = {
        id = iid,
        GetComponent = function(_, cid) return comps[cid] end,
        components = comps,
        order = order,
      }
    end

    lab.world.answer("ui.Interfaces.GetInterface", function(iid)
      local i = made[iid]
      if i == nil then return nil end
      return lab.world.entity("Interface", { id = iid, GetComponent = i.GetComponent })
    end)
    lab.world.answer("ui.Interfaces.GetComponent", function(a, b)
      -- either a combined id, or the interface and the component
      local iid, cid = a, b
      if b == nil and math.type(a) == "integer" then iid, cid = a >> 16, a & 0xFFFF end
      local i = made[iid]
      return i and i.components[cid] or nil
    end)
  end

  ---What the vars hold right now: lab.world.vars({ QUEST_POINTS = { value = 42 } }).
  function lab.world.vars(byName)
    setUp("vars")
    for name, v in pairs(byName or {}) do
      varValues[name] = type(v) == "table" and v or { value = v }
    end
  end

  ---The probe answers a call: fn gets the call's arguments and returns what the world holds.
  function lab.world.answer(api, fn)
    if M.calls[api] == nil then return gap(api .. " has never been measured, so the world can't answer it") end
    setUp(api)
    worldAnswers[api] = fn
  end

  ---An object of a type the typings name, holding only what the probe gives.
  function lab.world.object(typeName, values)
    local fields = M.types and M.types[typeName]
    if fields == nil then return gap("no type " .. tostring(typeName) .. " in the typings") end
    return worldObject(typeName, fields, values or {})
  end

  -- Inventories the probe has set, by id. Inventory.Find is given an InvType or an id; the
  -- InvType's id is the key either way.
  local inventories = {}
  local function invKey(key)
    if type(key) == "number" then return key end
    if type(key) == "table" and handedOut[key] then return key.id end
    return nil
  end

  ---An entity the world holds: an npc, a loc, a ground item, the player.
  ---@param typeName string # NPCEntity, LocEntity, ObjStackEntity, PlayerEntity
  function lab.world.entity(typeName, values)
    local fields = (M.types and M.types[typeName])
      or (M.gone and M.gone[typeName:sub(1, 1):lower() .. typeName:sub(2)] or {}).fields
    if fields == nil then return gap("no type " .. tostring(typeName) .. " for the world to make") end
    return worldObject(typeName, fields, values or {})
  end

  ---Who the player is and where they stand: lab.world.player({ coordGrid = CoordGrid.new(p) }).
  function lab.world.player(values)
    setUp("player")
    local v = {}
    for k, x in pairs(values or {}) do v[k] = x end
    -- at = { x, z, level } is the short way to stand somewhere
    if v.at ~= nil then
      v.coordGrid, v.at = lab.world.at(v.at[1] or v.at.x, v.at[2] or v.at.z, v.at[3] or v.at.level), nil
    end
    lab.world.answer("Player.GetEntity", function() return v end)
  end

  ---What is around: lab.world.npcs({ { configID = 81, coordGrid = c } }), and the same for locs and
  ---ground items. Each list answers the lookups that were measured to return one.
  local function listWorld(prefix, typeName, items, byField)
    local made = {}
    for i, v in ipairs(items or {}) do
      made[i] = getmetatable(v) ~= nil and v or lab.world.entity(typeName, v)
    end
    local function matching(pick)
      local out = {}
      for _, e in ipairs(made) do
        if pick(e) then out[#out + 1] = e end
      end
      return out
    end
    -- only the lookups this client has: answering one nobody measured would be the world inventing
    local function hasIt(api, fn)
      if M.calls[api] ~= nil then lab.world.answer(api, fn) end
    end
    hasIt(prefix .. ".All", function() return made end)
    hasIt(prefix .. ".ByConfigID", function(id) return matching(function(e) return e.configID == id end) end)
    hasIt(prefix .. ".ByName", function(name) return matching(function(e) return e[byField] == name end) end)
    hasIt(prefix .. ".Filter", function(f)
      local fn = type(f) == "table" and f.func or nil
      if fn == nil then return made end
      return matching(function(e) return fn(e) == true end)
    end)
  end

  function lab.world.npcs(items) setUp("npcs") listWorld("NPCList", "NPCEntity", items, "displayName") end
  function lab.world.locs(items) setUp("locs") listWorld("LocList", "LocEntity", items, "displayName") end
  function lab.world.groundItems(items) setUp("ground items") listWorld("ObjStackList", "ObjStackEntity", items, "displayName") end

  ---lab.world.inventory(93, { { obj = 4151, count = 1 }, { obj = 995, count = 5000 } })
  function lab.world.inventory(id, items)
    local slots = {}
    for i, it in ipairs(items or {}) do
      local obj = type(it.obj) == "number" and G.config.Obj.FromID(it.obj) or it.obj
      slots[i] = lab.world.object("InvSlot", { obj = obj, count = it.count })
    end
    local invConfig = G.config.Inv.FromID(id)
    local function total(match)
      local sum = 0
      for i, it in ipairs(items or {}) do
        if match(slots[i].obj) then sum = sum + (it.count or 0) end
      end
      return sum
    end
    local inv = {
      id = id,
      isValid = true,
      config = invConfig,
      GetSlots = function() return slots end,
      GetSlot = function(_, i) return slots[i] end,
      -- GetTotal and the counts were never measured on a real inventory; they are summed from the
      -- slots the probe gave, and the size is the one the client's harvest gives this inventory
      GetTotal = function(_, obj)
        derived("InvHandle:GetTotal, summed from the slots lab.world was given")
        local want = type(obj) == "number" and obj or (obj ~= nil and obj.id or nil)
        return total(function(o) return o ~= nil and o.id == want end)
      end,
      GetCategoryTotal = function(_, cat)
        derived("InvHandle:GetCategoryTotal, summed by each item's harvested category")
        return total(function(o) return o ~= nil and o.category == cat end)
      end,
    }
    local size = invConfig ~= nil and invConfig.size or nil
    if type(size) == "number" then
      derived("InvHandle.slotCount/usedSlotCount/freeSlotCount, from the harvested size and the slots given")
      inv.slotCount, inv.usedSlotCount, inv.freeSlotCount = size, #slots, size - #slots
    end
    setUp("inventory " .. tostring(id))
    inventories[id] = inv
    if worldAnswers["Inventory.Find"] == nil then
      lab.world.answer("Inventory.Find", function(key)
        local k = invKey(key)
        local inv = k ~= nil and inventories[k] or nil
        if inv == nil then return gap("inventory " .. tostring(k) .. " is not in lab.world") end
        return inv
      end)
    end
  end
end
