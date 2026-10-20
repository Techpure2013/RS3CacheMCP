-- twelve requires, so a page of five has to be asked for three times
local steps = require("src/core/steps")
local requirements = require("src/core/requirements")
local helpers = require("src/core/helpers/quest")
local zone = require("src/data/zone")
local npc = require("src/steps/npc")
local loc = require("src/steps/loc")
local item = require("src/steps/item")
local dialog = require("src/requirements/dialog")
local var = require("src/requirements/var")
local skill = require("src/requirements/skill")
local panel = require("src/ui/panel")
local log = require("src/util/log")

return { steps, requirements, helpers, zone, npc, loc, item, dialog, var, skill, panel, log }
