-- Known-answer fixture for the lua block parser.
--
-- Every function below is annotated with what the parser must return. If these numbers stop
-- matching, the parser is wrong, not the fixture. This file exists because `for x do` was counted
-- as two block openers for weeks and nothing noticed.
--
-- EXPECT total functions: 8

-- EXPECT alpha lines=3 depth=0
local function alpha()
    return 1
end

-- EXPECT beta lines=5 depth=1
-- a for loop opens ONE block and closes with one end
local function beta(list)
    for _, v in ipairs(list) do
        print(v)
    end
end

-- EXPECT gamma lines=7 depth=2
-- nested loop, two levels
local function gamma(rows)
    for _, row in ipairs(rows) do
        for _, cell in ipairs(row) do
            print(cell)
        end
    end
end

-- EXPECT delta lines=6 depth=1
-- while also opens one block despite carrying `do`
local function delta(n)
    while n > 0 do
        n = n - 1
    end
    return n
end

-- EXPECT epsilon lines=4 depth=0
-- if..then..return..end on one line: nothing ever sits inside it
local function epsilon(x)
    if x == nil then return 0 end
    return x
end

-- EXPECT zeta lines=9 depth=3
local function zeta(t)
    if t then
        for k, v in pairs(t) do
            if v then
                print(k)
            end
        end
    end
end

-- EXPECT eta lines=7 depth=1
-- elseif needs no end of its own
local function eta(n)
    if n == 1 then
        return "one"
    elseif n == 2 then
        return "two"
    end
end

-- EXPECT theta lines=6 depth=1
-- a bare do block is a real opener
local function theta()
    do
        local scoped = 1
        return scoped
    end
end
