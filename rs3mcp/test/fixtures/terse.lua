-- draw the frame

local M = {}

-- max rows on screen
local LIMIT = 20

-- clamp n to the visible range
function M.clamp(n)
    -- only positive rows
    if n < 1 then
        return 1
    end
    return math.min(n, LIMIT)
end

return M
