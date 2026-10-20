-- Known-answer fixture for lua long brackets.
--
-- A long string or long comment can contain the words `function`, `end`, `if` and `do`. A
-- line-based parser counts those as real block keywords and the whole file goes wrong from there.
--
-- EXPECT functions: 3
-- EXPECT alpha lines=7 depth=0   (its long string mentions end and function, neither is code)
-- EXPECT beta  lines=3 depth=0   (the long comment above it mentions if/then/end)
-- EXPECT gamma lines=5 depth=1

local function alpha()
    local sql = [[
        select * from t where x = 1 -- if this counted as a comment, the next line vanishes
        function end end end do do
    ]]
    return sql
end

--[[
    A long comment. It mentions if x then return end, and function, and do, several times:
    function do if then end end end
]]
local function beta()
    return 1
end

local function gamma(t)
    for _, v in ipairs(t) do
        print(v)
    end
end
