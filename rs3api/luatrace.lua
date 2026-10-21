-- Runs one test file and reports every check it makes, without the test knowing.
--   lua luatrace.lua test/run.lua        (from the plugin root, as the test expects)
--
-- Every harness here counts through a local eq(got, want, label), and some add near. A call
-- hook sees those by name as they are entered, arguments and all, so nothing in the tests changes.
-- Records go to stderr, one per line, and print is routed there too so a FAIL line lands in order
-- after the check that caused it.

local file = assert(arg[1], "usage: lua luatrace.lua test/file.lua")
local err = io.stderr

-- Which argument is the label. eq and near are judged here; the rest answer by printing FAIL, so
-- they go out as undecided and the FAIL line that follows, or does not, decides them.
local CHECKS = { eq = 3, near = 3, has = 3, lacks = 3, expect = 1 }
local JUDGED = { eq = true, near = true }

local function flat(s)
  return (tostring(s):gsub("[\t\r\n]", " "))
end

local function show(v, deep)
  local t = type(v)
  if t == "string" then
    return string.format("%q", #v > 120 and v:sub(1, 120) .. "..." or v)
  elseif t ~= "table" or deep then
    return tostring(v)
  end
  local parts, n = {}, 0
  for k, x in pairs(v) do
    n = n + 1
    if n > 6 then
      parts[#parts + 1] = "..."
      break
    end
    parts[#parts + 1] = tostring(k) .. "=" .. show(x, true)
  end
  return "{" .. table.concat(parts, ", ") .. "}"
end

local source = "@" .. file
debug.sethook(function()
  local fn = debug.getinfo(2, "nS")
  if not (fn and CHECKS[fn.name] and fn.source == source) then return end
  local _, label = debug.getlocal(2, CHECKS[fn.name])
  if type(label) ~= "string" then return end
  local _, got = debug.getlocal(2, 1)
  local _, want = debug.getlocal(2, 2)
  local at = debug.getinfo(3, "l")
  -- A check made through a helper (isTrue calls eq) is reported at the helper. The line in the
  -- test's own body that led there is the one a reader wants, so it goes out as well.
  local outer, level = 0, 3
  while true do
    local frame = debug.getinfo(level, "Sl")
    if not frame then break end
    if frame.source == source and frame.what == "main" then
      outer = frame.currentline
      break
    end
    level = level + 1
  end
  local verdict = 2
  if fn.name == "near" then
    verdict = (type(got) == "number" and type(want) == "number" and math.abs(got - want) < 0.001) and 1 or 0
  elseif JUDGED[fn.name] then
    verdict = got == want and 1 or 0
  end
  err:write(
    "\1CHECK\t",
    at and at.currentline or 0, "\t",
    outer, "\t",
    verdict, "\t",
    flat(label), "\t",
    flat(show(got)), "\t",
    flat(show(want)), "\n"
  )
end, "c")

print = function(...)
  local parts = table.pack(...)
  for i = 1, parts.n do parts[i] = tostring(parts[i]) end
  for line in (table.concat(parts, "\t") .. "\n"):gmatch("([^\n]*)\n") do
    err:write("\1OUT\t", line, "\n")
  end
end

arg = { [0] = file, table.unpack(arg, 2) }
dofile(file)
