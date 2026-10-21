-- Runs one probe against one plugin, the way the RS3 client would load it, and writes what
-- happened as json. Run as: lua54 lab_runner.lua <spec.lua> <out.json>
--
-- The spec is a lua file returning { root, entry, script, seed, seconds, mb }. The plugin code
-- sees a client-shaped world: one shared _G, its own _ENV over it, a require that returns nil for
-- a missing module, and none of os, io, package or debug.

local specPath, outPath = arg[1], arg[2]
local spec = dofile(specPath)
local root = spec.root
local out = { ok = true, prints = {}, findings = {}, missing = {}, required = {}, root = root }

-- json, only as much as the result needs
local function encode(v, depth, seen)
  depth, seen = depth or 0, seen or {}
  local t = type(v)
  if v == nil then return "null" end
  if t == "boolean" then return tostring(v) end
  if t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return '"' .. tostring(v) .. '"' end
    if math.type(v) == "integer" then return tostring(v) end
    return string.format("%.17g", v)
  end
  if t == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c)
      local named = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }
      return named[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  end
  if t ~= "table" then return '"<' .. t .. '>"' end
  if seen[v] then return '"<cycle>"' end
  if depth > 8 then return '"<too deep>"' end
  seen[v] = true
  local n = #v
  local parts = {}
  local isList = n > 0 or next(v) == nil
  if isList then
    for k in pairs(v) do
      if math.type(k) ~= "integer" or k < 1 or k > n then isList = false break end
    end
  end
  -- an empty table goes out as a list: the result's own lists start empty and stay lists
  if isList then
    for i = 1, n do parts[i] = encode(v[i], depth + 1, seen) end
    seen[v] = nil
    return "[" .. table.concat(parts, ",") .. "]"
  end
  local keys = {}
  for k in pairs(v) do keys[#keys + 1] = k end
  table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
  for _, k in ipairs(keys) do
    parts[#parts + 1] = encode(tostring(k)) .. ":" .. encode(v[k], depth + 1, seen)
  end
  seen[v] = nil
  return "{" .. table.concat(parts, ",") .. "}"
end

local function finish()
  local fh = assert(io.open(outPath, "w"))
  fh:write(encode(out))
  fh:close()
end

-- the shared world every plugin sees
local G = {}
for _, name in ipairs({ "assert", "error", "getmetatable", "ipairs", "next", "pairs", "pcall",
                        "rawequal", "rawget", "rawlen", "rawset", "select", "setmetatable",
                        "tonumber", "tostring", "type", "xpcall" }) do
  G[name] = _G[name]
end
-- as qd probe measured in the client (2026-09-13): load and collectgarbage are there, utf8 is not
G.string, G.table, G.math, G.coroutine = string, table, math, coroutine
G.collectgarbage = collectgarbage
-- text only, and a chunk with no environment of its own gets the shared world, never the
-- runner's, which holds io and os
G.load = function(chunk, name, mode, chunkEnv)
  return load(chunk, name, "t", chunkEnv or G)
end
G._G = G
G._VERSION = _VERSION

G.print = function(...)
  if #out.prints >= 2000 then return end
  local parts = {}
  for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
  out.prints[#out.prints + 1] = table.concat(parts, "\t")
end

local discoverRecorder

-- Discovery: every global the plugin reaches for that the lab doesn't have answers with a recorder
-- that accepts anything and writes down the path it was reached by. It exists to list what a client
-- model has to cover, never to judge a plugin: a recorder answers everything, so nothing fails.
if spec.discover then
  local touched = {}
  local function recorder(path)
    touched[path] = (touched[path] or 0) + 1
    local r = {}
    return setmetatable(r, {
      -- a numbered lookup ends the list, so ipairs over a recorded list stops instead of counting
      -- forever; the element shape is still recorded, once, under [1]
      __index = function(_, k)
        if math.type(k) == "integer" then
          if k == 1 and not touched[path .. "[1]"] then return recorder(path .. "[1]") end
          return nil
        end
        return recorder(path .. "." .. tostring(k))
      end,
      __newindex = function(_, k) touched[path .. "." .. tostring(k) .. " (set)"] = true end,
      __call = function(_, ...)
        local n = select("#", ...)
        if n > 0 and select(1, ...) == r then n = n - 1 end
        return recorder(path .. "(" .. n .. ")")
      end,
      __len = function() return 0 end,
      __concat = function(a, b) return tostring(a) .. tostring(b) end,
      __tostring = function() return "<" .. path .. ">" end,
      __unm = function() return 0 end,
      __add = function() return 0 end, __sub = function() return 0 end,
      __mul = function() return 0 end, __div = function() return 0 end,
      __mod = function() return 0 end, __idiv = function() return 0 end,
      __lt = function() return false end, __le = function() return false end,
      __band = function() return 0 end, __bor = function() return 0 end,
      __shl = function() return 0 end, __shr = function() return 0 end,
    })
  end
  setmetatable(G, { __index = function(_, k)
    if k == "package" or k == "os" or k == "io" or k == "debug" then return nil end
    return recorder(tostring(k))
  end })
  out.touched = touched
  discoverRecorder = recorder
end

local env = setmetatable({}, { __index = G })
local loaded = {}
env.__PACKAGES_LOADED = loaded

local function readFile(path)
  local fh = io.open(path, "rb")
  if not fh then return nil end
  local src = fh:read("a")
  fh:close()
  -- the client loads files the way luaL_loadfile does, which skips a UTF-8 byte-order mark; a
  -- string handed to load() does not, so without this a file that runs in game fails here
  if src:sub(1, 3) == "\239\187\191" then src = src:sub(4) end
  return src
end

-- the client's require: relative to the plugin root, no extension, runs once, and hands back nil
-- for a module it cannot find instead of raising
env.require = function(path)
  if loaded[path] ~= nil then return loaded[path] or nil end
  local src = readFile(root .. "/" .. path .. ".lua")
  if not src then
    out.missing[#out.missing + 1] = path
    loaded[path] = false
    return nil
  end
  out.required[#out.required + 1] = path
  local chunk, err = load(src, "@" .. path .. ".lua", "t", env)
  if not chunk then error(err, 0) end
  local value = chunk()
  loaded[path] = value == nil and false or value
  if env.lab.watch and type(value) == "table" then env.lab.watch(path, value) end
  return value
end

local lab = { require = env.require }
env.lab = lab

-- Recording. A traced run snapshots the watched tables at every boundary (after the entry loads,
-- either side of each event, at the end) and keeps only what changed, plus which lines ran in
-- between. Snapshots rather than proxy tables, because a proxy changes how next and rawget see the
-- plugin's own tables, and a recorder must not change what it records.
local watching = spec.trace or spec.whyPath or spec.breakWhen
local watched = {}
local trace = spec.trace and { segments = {} } or nil
local segLines = {}
local last = {}
local eventSeq = 0

if watching then
  function lab.watch(name, t)
    for _, w in ipairs(watched) do
      if w.t == t then return end
    end
    watched[#watched + 1] = { name = name, t = t }
  end
end

local function keyPart(k)
  if type(k) == "string" and k:match("^[%a_][%w_]*$") then return "." .. k end
  if math.type(k) == "integer" then return "[" .. k .. "]" end
  return "[" .. string.format("%q", tostring(k)) .. "]"
end

local function leaf(v)
  local t = type(v)
  if t == "number" or t == "string" or t == "boolean" then return v end
  return "<" .. t .. ">"
end

local function flatten(into, v, path, depth, seen, budget)
  if type(v) ~= "table" then into[path] = leaf(v) return end
  if seen[v] then into[path] = "<cycle>" return end
  if depth >= 6 then into[path] = "<table>" return end
  seen[v] = true
  local any = false
  -- raw next, so a plugin's own __pairs or __index never runs on the recorder's behalf
  for k, x in next, v do
    any = true
    budget.n = budget.n - 1
    if budget.n <= 0 then into[path .. "..."] = "<more>" break end
    flatten(into, x, path .. keyPart(k), depth + 1, seen, budget)
  end
  if not any then into[path] = "{}" end
  seen[v] = nil
end

local function snapshot()
  local s = {}
  for _, w in ipairs(watched) do flatten(s, w.t, w.name, 0, {}, { n = 5000 }) end
  return s
end

local function boundary(kind, seq, name, payload)
  if not trace then return end
  local now = snapshot()
  local changes = {}
  for p, v in pairs(now) do
    if last[p] ~= v then changes[#changes + 1] = { path = p, old = last[p], new = v } end
  end
  for p, v in pairs(last) do
    if now[p] == nil then changes[#changes + 1] = { path = p, old = v } end
  end
  table.sort(changes, function(a, b) return a.path < b.path end)
  trace.segments[#trace.segments + 1] = { kind = kind, seq = seq, name = name, payload = payload,
                                          changes = changes, lines = segLines }
  last, segLines = now, {}
end

-- A path like core/counter.state.loot[1].item, read straight off the watched tables.
local function resolve(path)
  local rootName, rest = path:match("^([^%.%[]+)(.*)$")
  if not rootName then return nil end
  local v
  for _, w in ipairs(watched) do
    if w.name == rootName then v = w.t end
  end
  local i = 1
  while i <= #rest do
    if type(v) ~= "table" then return nil end
    local name, after = rest:match("^%.([%a_][%w_]*)()", i)
    if name then
      v, i = rawget(v, name), after
    else
      local num, after2 = rest:match("^%[(%-?%d+)%]()", i)
      if num then
        v, i = rawget(v, tonumber(num)), after2
      else
        local quoted, after3 = rest:match('^%[(%b"")%]()', i)
        if not quoted then return nil end
        v, i = rawget(v, load("return " .. quoted)()), after3
      end
    end
  end
  return v
end
lab.get = resolve

local function valueKey(v)
  if type(v) ~= "table" then return type(v) .. ":" .. tostring(v) end
  local flat = {}
  flatten(flat, v, "", 0, {}, { n = 2000 })
  local keys = {}
  for k in pairs(flat) do keys[#keys + 1] = k end
  table.sort(keys)
  local parts = {}
  for _, k in ipairs(keys) do parts[#parts + 1] = k .. "=" .. tostring(flat[k]) end
  return table.concat(parts, ";")
end

local function captureLocals(level)
  local locals = {}
  for i = 1, 60 do
    local name, value = debug.getlocal(level, i)
    if not name then break end
    if name:sub(1, 1) ~= "(" then locals[name] = leaf(value) end
  end
  return locals
end

-- why: replaying to one event with a watch on one value. Lines run one at a time, so the first time
-- the value differs from what it was, the line that ran just before is the one that wrote it.
local whyActive, whyPrev, whyLast
local function whyStep(line)
  local now = valueKey(resolve(spec.whyPath))
  if now ~= whyPrev and whyLast and not out.why then
    out.why = { line = whyLast.src .. ":" .. whyLast.line, locals = whyLast.locals }
  end
  whyPrev = now
  local info = debug.getinfo(3, "Sl")
  if info and info.source:sub(1, 1) == "@" then
    whyLast = { src = info.source:sub(2), line = line or info.currentline, locals = captureLocals(4) }
  end
end

-- Event.X.Subscribe(name, fn) as the client has it. A name already taken on that event is dropped
-- without a word in the client, which cost hours once, so here it is recorded as a finding.
local function bus(eventName)
  local order, byName = {}, {}
  local b = {}
  -- the client takes Event.X.Subscribe(name, fn); a colon call passes the bus first, so drop it
  function b.Subscribe(...)
    local name, fn = ...
    if name == b then name, fn = select(2, ...) end
    if byName[name] then
      out.findings[#out.findings + 1] = string.format(
        "Event.%s.Subscribe(%q) reused a name already subscribed; the client drops the second one",
        eventName, tostring(name))
      return
    end
    byName[name] = fn
    order[#order + 1] = name
  end
  function b.Unsubscribe(...)
    local name = ...
    if name == b then name = select(2, ...) end
    byName[name] = nil
  end
  function b.fire(payload)
    for _, name in ipairs(order) do
      local fn = byName[name]
      if fn then fn(payload) end
    end
  end
  return b
end

local buses = {}
G.Event = setmetatable({}, { __index = function(t, eventName)
  local b = bus(eventName)
  rawset(t, eventName, b)
  buses[#buses + 1] = eventName
  return b
end })

-- the events the plugin has asked for, in the order it first asked
function lab.events()
  local names = {}
  for _, n in ipairs(buses) do names[#names + 1] = n end
  return names
end

-- discovery only: a payload that records every field a handler reads off it
function lab.recorder(path)
  return discoverRecorder and discoverRecorder(path) or nil
end

local breakHit
local stopped
local function checkBreak(payload)
  if not spec.breakWhen or breakHit then return end
  local scope = setmetatable({ event = payload }, { __index = env })
  local cond = load("return " .. spec.breakWhen, "@break", "t", scope)
  local okc, hit = pcall(cond)
  if okc and hit then
    breakHit = { seq = eventSeq, values = {} }
    for _, expr in ipairs(spec.breakShow or {}) do
      local f = load("return " .. expr, "@show", "t", scope)
      local okv, v = pcall(f)
      breakHit.values[expr] = okv and leaf(v) or ("<error: " .. tostring(v) .. ">")
    end
    -- through the same door as a limit, so a pcall in the plugin cannot catch it
    stopped = "break"
    error("lab: break", 0)
  end
end

function lab.fire(eventName, payload)
  eventSeq = eventSeq + 1
  boundary("probe", eventSeq)
  if spec.whyPath and eventSeq == spec.whyAt then
    whyActive, whyPrev, whyLast = true, valueKey(resolve(spec.whyPath)), nil
  end
  G.Event[eventName].fire(payload)
  whyActive = false
  boundary("event", eventSeq, eventName, payload)
  checkBreak(payload)
end

-- a runaway probe or a memory hog stops the run rather than the machine
local started = os.clock()
local function limit()
  if not stopped then
    if os.clock() - started > spec.seconds then
      stopped = "time limit"
    elseif collectgarbage("count") > spec.mb * 1024 then
      stopped = "memory limit"
    end
  end
  if stopped then error("lab: " .. stopped, 0) end
end

-- The limit rides on the count hook. A traced run also counts lines, and a why replay also sees
-- returns, so a write on a function's last line is still caught before the frame is gone.
local mask = spec.whyPath and "lr" or (trace and "l" or "")
local function hook(ev, line)
  if ev == "count" then return limit() end
  if trace and ev == "line" then
    local src = debug.getinfo(2, "S").source
    if src:sub(1, 1) == "@" then
      local key = src:sub(2) .. ":" .. line
      segLines[key] = (segLines[key] or 0) + 1
    end
  end
  if whyActive then whyStep(line) end
end
local function installHook(thread)
  if mask == "" then
    if thread then debug.sethook(thread, limit, "", 10000) else debug.sethook(limit, "", 10000) end
  elseif thread then
    debug.sethook(thread, hook, mask, 10000)
  else
    debug.sethook(hook, mask, 10000)
  end
end
installHook()

-- A stop has to get out through anything the probe can catch it with. pcall, xpcall and
-- coroutine.resume hand it straight back up once the run is stopped, and hooks are per thread, so
-- every coroutine the probe makes gets the same one.
local function passStop(ok, ...)
  if stopped then error("lab: " .. stopped, 0) end
  return ok, ...
end
G.pcall = function(...) return passStop(pcall(...)) end
G.xpcall = function(...) return passStop(xpcall(...)) end
local co = {}
for k, v in pairs(coroutine) do co[k] = v end
co.create = function(fn)
  local thread = coroutine.create(fn)
  installHook(thread)
  return thread
end
co.resume = function(...) return passStop(coroutine.resume(...)) end
co.wrap = function(fn)
  local thread = co.create(fn)
  return function(...)
    local res = table.pack(coroutine.resume(thread, ...))
    if stopped then error("lab: " .. stopped, 0) end
    if not res[1] then error(res[2], 0) end
    return table.unpack(res, 2, res.n)
  end
end
G.coroutine = co

-- the client, when the run asks for it: answers only from what the live client was measured doing,
-- and a crash or a gap in the model stops the run the way a limit does
if spec.model then
  local install = dofile(spec.modelCode)
  install(G, dofile(spec.model), out, spec.harvestDir, function(reason)
    stopped = reason
    error("lab: " .. reason, 0)
  end, lab, spec.listGaps)
end

math.randomseed(spec.seed)

local function run()
  if spec.entry then
    local src = readFile(root .. "/" .. spec.entry)
    if not src then error("entry " .. spec.entry .. " is not in the plugin", 0) end
    local chunk, err = load(src, "@" .. spec.entry, "t", env)
    if not chunk then error(err, 0) end
    chunk()
    boundary("load", 0)
  end
  if spec.script then
    local chunk, err = load(spec.script, "@probe", "t", env)
    if not chunk then error(err, 0) end
    return chunk()
  end
end

local ok, result = xpcall(run, debug.traceback)
debug.sethook()
boundary("end", eventSeq)
out.trace = trace
out.hit = breakHit
if breakHit then
  -- a break is where the question was answered, not a failure
  ok, result, stopped = true, nil, nil
end
out.ok = ok and not stopped
if stopped then
  out.stopped = stopped
  out.error = "lab: " .. stopped
elseif not ok then
  out.error = tostring(result)
else
  out.result = result
end
out.seconds = os.clock() - started
out.memoryKB = math.floor(collectgarbage("count"))
finish()
