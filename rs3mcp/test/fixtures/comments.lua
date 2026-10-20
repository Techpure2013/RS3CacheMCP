-- Known-answer fixture for the comment parser.
--
-- EXPECT prose units: 7
-- EXPECT short units (2-7 words): 4  (the bracketed Stats line is six)
-- EXPECT the wrapped block below counts as ONE unit of 19 words, not three of 6

-- draw frame
local function a() end

-- reset cooldowns
local function b() end

-- input state
local c = 1

-- Re-read tile bounds each frame because a freshly opened interface reports
-- zero by zero until it has laid itself out, which only happens after the
-- first draw call completes
local d = 2

-- TODO: no idea why the client returns nil here on the first tick
local e = 3

-- Stats (always based on actual GP)
local f = 4

---@param x integer this is a LuaLS annotation and must NOT count as prose
---@return boolean
local function g(x) return x > 0 end

-- local shouldNotCount = 1
-- if a then return end
local h = 5
