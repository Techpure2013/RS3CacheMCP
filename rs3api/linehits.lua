-- Which lines a suite actually runs
--   lua54 linehits.lua test/run.lua test/units.lua
--
-- Prints "file:line" for every line executed. A mutant on a line nobody runs is a coverage gap,
-- not a weak assertion, and the two want different work

local hits = {}

debug.sethook(function(_, line)
    local info = debug.getinfo(2, "S")
    if info ~= nil and info.short_src ~= nil then
        hits[info.short_src .. ":" .. line] = true
    end
end, "l")

-- a suite ends by exiting, which would take this with it
local leave = os.exit
os.exit = function() end

for i = 1, #arg do
    local ok, err = pcall(dofile, arg[i])
    if not ok then
        io.stderr:write("linehits: " .. arg[i] .. ": " .. tostring(err) .. "\n")
    end
end

debug.sethook()
os.exit = leave

local out = {}
for key in pairs(hits) do
    out[#out + 1] = key
end
table.sort(out)
-- one file per run: beside the script it was shared, and two runs wrote over each other
local fh = io.open(os.getenv("LINEHITS_OUT") or (arg[0] .. ".hits"), "w")
for _, key in ipairs(out) do
    fh:write(key .. "\n")
end
fh:close()
print(#out .. " lines run")
