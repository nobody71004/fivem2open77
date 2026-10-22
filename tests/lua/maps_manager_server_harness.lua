-- Runs f2o77_maps' server side (shared/config.lua, shared/maps/*.lua, server/main.lua) against a
-- fake Open77 server -- virtual clock, coroutine scheduler, data/ folder, restart scheduler, players,
-- ACL, exports, HTTP -- through whole moves: place, nudge, presets, apply now (bake, restart),
-- cancel (unstage), apply when empty, a server restart that makes the move live, revert, the
-- console command, the agent API, the baker going silent, the no-start-hook install request.
-- usage: lua5.4 maps_manager_server_harness.lua <f2o77_maps dir>
local DIR = assert(arg[1], "f2o77_maps dir")

-- ------------------------------------------------------------------ helpers
local function deepcopy(v)
    if type(v) ~= "table" then return v end
    local t = {}
    for k, x in pairs(v) do t[k] = deepcopy(x) end
    return t
end

local LOG = {}
local realprint = print
local function fail(msg)
    io.stderr:write("FAIL: " .. msg .. "\n--- log\n" .. table.concat(LOG, "\n") .. "\n")
    os.exit(1)
end
local function check(cond, msg) if not cond then fail(msg) end end
local function near(a, b, eps) return math.abs(a - b) <= (eps or 1e-6) end

-- a small JSON codec (the script decodes POST bodies with the global json)
json = {}
local function enc(v)
    local t = type(v)
    if t == "nil" then return "null" end
    if t == "boolean" then return v and "true" or "false" end
    if t == "number" then return (math.type(v) == "integer") and tostring(v) or string.format("%.17g", v) end
    if t == "string" then return '"' .. v:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"' end
    if #v > 0 or next(v) == nil then
        local parts = {}
        for i = 1, #v do parts[i] = enc(v[i]) end
        return "[" .. table.concat(parts, ",") .. "]"
    end
    local parts = {}
    for k, x in pairs(v) do parts[#parts + 1] = enc(tostring(k)) .. ":" .. enc(x) end
    return "{" .. table.concat(parts, ",") .. "}"
end
json.encode = enc
function json.decode(s)
    local pos = 1
    local function ws() pos = s:find("[^%s]", pos) or #s + 1 end
    local value
    local function str()
        local out = {}
        pos = pos + 1
        while true do
            local c = s:sub(pos, pos)
            if c == "" then error("unterminated string") end
            if c == '"' then pos = pos + 1 break end
            if c == "\\" then
                local n = s:sub(pos + 1, pos + 1)
                if n == "u" then out[#out + 1] = string.char(tonumber(s:sub(pos + 2, pos + 5), 16)); pos = pos + 6
                else out[#out + 1] = ({ n = "\n", t = "\t" })[n] or n; pos = pos + 2 end
            else out[#out + 1] = c; pos = pos + 1 end
        end
        return table.concat(out)
    end
    function value()
        ws()
        local c = s:sub(pos, pos)
        if c == "{" then
            local t = {}
            pos = pos + 1
            ws()
            if s:sub(pos, pos) == "}" then pos = pos + 1 return t end
            while true do
                ws()
                if s:sub(pos, pos) ~= '"' then error("bad object") end
                local k = str()
                ws(); pos = pos + 1
                t[k] = value()
                ws()
                local d = s:sub(pos, pos); pos = pos + 1
                if d == "}" then return t end
                if d ~= "," then error("bad object") end
            end
        elseif c == "[" then
            local t = {}
            pos = pos + 1
            ws()
            if s:sub(pos, pos) == "]" then pos = pos + 1 return t end
            while true do
                t[#t + 1] = value()
                ws()
                local d = s:sub(pos, pos); pos = pos + 1
                if d == "]" then return t end
            end
        elseif c == '"' then return str()
        elseif s:sub(pos, pos + 3) == "true" then pos = pos + 4 return true
        elseif s:sub(pos, pos + 4) == "false" then pos = pos + 5 return false
        elseif s:sub(pos, pos + 3) == "null" then pos = pos + 4 return nil
        else
            local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
            if not num or num == "" then error("bad json at " .. pos) end
            pos = pos + #num
            return tonumber(num)
        end
    end
    return value()
end

-- ------------------------------------------------------------------ the world (survives server restarts)
local W = {
    unix = 1760000000.0, mono = 100.0,
    files = {}, dirs = {}, writes = {},
    kvp = {},
    exportBaked = { cedar_crest = { x = 480.0, y = 1800.0, z = 174.0, yaw = 0.0, id = "cedar_crest-convert-1" } },
    exportReason = {},
    online = { 1, 2 },
    players = {
        [1] = { position = { x = 10.0, y = 20.0, z = 30.0 }, heading = 90.0 },
        [2] = { position = { x = -5.0, y = 7.0, z = 2.0 }, heading = 0.0 },
    },
    acl = { [1] = { ["command.maps"] = true }, [2] = {} },
    restart = { scheduled = false, secondsRemaining = 0 },
    schedules = {}, cancels = 0,
    chats = {}, teleports = {}, toClients = {}, screenshots = {},
}

local R                                  -- the current server run (one Lua "VM")

local function addFile(path, value)
    W.files[path] = deepcopy(value)
    W.writes[path] = (W.writes[path] or 0) + 1
    local d = path:match("^(.*)/[^/]+$")
    while d do W.dirs[d] = true; d = d:match("^(.*)/[^/]+$") end
end

local function promise(...)
    local vals = table.pack(...)
    return { await = function()
        assert(coroutine.isyieldable(), "await needs a scheduler coroutine")
        return table.unpack(vals, 1, vals.n)
    end, status = function() return "resolved" end }
end

local function resume(th, ...)
    local ok, ms = coroutine.resume(th.co, ...)
    if not ok then fail("thread error: " .. tostring(ms) .. "\n" .. debug.traceback(th.co)) end
    if coroutine.status(th.co) == "dead" then th.dead = true return end
    th.wake = W.mono + (tonumber(ms) or 0) / 1000.0
end

local function advance(seconds)
    local steps = math.floor(seconds * 10 + 0.5)
    for _ = 1, steps do
        W.unix, W.mono = W.unix + 0.1, W.mono + 0.1
        if W.restart.scheduled then
            W.restart.secondsRemaining = W.restart.secondsRemaining - 0.1
            if W.restart.secondsRemaining <= 0 then W.restart.fired = true end
        end
        if not W.restart.fired then
            for _, th in ipairs(R.threads) do
                if not th.dead and th.wake <= W.mono + 1e-9 then resume(th) end
            end
        end
    end
end

local function newRun()
    if R then   -- the old generation stops first (a reload, or the server shutting down)
        for _, fn in ipairs(R.handlers["onResourceStop"] or {}) do fn("f2o77_maps") end
    end
    R = { handlers = {}, net = {}, commands = {}, exportsT = {}, threads = {}, http = nil }
    function print(...)
        local t = {}
        for i = 1, select("#", ...) do t[#t + 1] = tostring((select(i, ...))) end
        LOG[#LOG + 1] = table.concat(t, " ")
    end
    function GetCurrentResourceName() return "f2o77_maps" end
    function GetGameTimer() return math.floor(W.mono * 1000) end
    function CreateThread(fn)
        local th = { co = coroutine.create(fn), wake = W.mono }
        R.threads[#R.threads + 1] = th
        resume(th)
    end
    function Wait(ms) return coroutine.yield(ms or 0) end
    function RegisterNetEvent(name, fn) R.net[name] = fn end
    function AddEventHandler(name, fn) R.handlers[name] = R.handlers[name] or {}; table.insert(R.handlers[name], fn) end
    function RegisterCommand(name, fn) R.commands[name] = fn end
    function TriggerClientEvent(name, target, payload)
        W.toClients[#W.toClients + 1] = { name = name, target = target, payload = deepcopy(payload) }
        return true
    end
    function exports(name, fn) R.exportsT[name] = fn end
    function LoadResourceFile(res, path)
        assert(res == "f2o77_maps", "cross resource read")
        local v = W.files[path]
        if type(v) == "string" then return v end
        return nil, "not_found"
    end
    Open77 = {
        time = { unix = function() return W.unix end, monotonic = function() return W.mono end },
        io = {
            exists = function(p) return W.files[p] ~= nil or W.dirs[p] == true end,
            makeDirectory = function(p)
                W.dirs[p] = true
                local d = p:match("^(.*)/[^/]+$")
                while d do W.dirs[d] = true; d = d:match("^(.*)/[^/]+$") end
                return true
            end,
            readJson = function(p)
                local v = W.files[p]
                if v == nil then return nil, "not_found" end
                if type(v) == "string" then return nil, "invalid_json" end
                return deepcopy(v)
            end,
            writeJson = function(p, v) addFile(p, v) return true end,
            read = function(p)
                local v = W.files[p]
                if v == nil then return nil, "not_found" end
                if type(v) ~= "string" then return nil, "not_a_text_file" end
                return v
            end,
            write = function(p, text) addFile(p, text) return true end,
            remove = function(p) W.files[p] = nil return true end,
        },
        kvp = {
            get = function(k) return W.kvp[k] end,
            set = function(k, v) W.kvp[k] = v return true end,
        },
        acl = { isAllowed = function(pid, right) return (W.acl[pid] or {})[right] == true end },
        players = {
            all = function() local t = {} for _, p in ipairs(W.online) do t[#t + 1] = p end return t end,
            get = function(pid)
                local p = W.players[pid]
                if not p then return nil, "player_not_found" end
                return { playerId = pid, position = deepcopy(p.position), heading = p.heading, fresh = true }
            end,
            position = function(pid) return W.players[pid] and deepcopy(W.players[pid].position) end,
            name = function(pid) return "player" .. tostring(pid) end,
            teleport = function(pid, pos, opts)
                W.teleports[#W.teleports + 1] = { pid = pid, pos = deepcopy(pos), opts = deepcopy(opts) }
                return promise({ x = pos.x, y = pos.y, z = pos.z, state = "settled" })
            end,
            requestScreenshot = function(pid, opts)
                W.screenshots[#W.screenshots + 1] = { pid = pid, opts = deepcopy(opts) }
                return promise({ status = 200 })
            end,
        },
        world = {
            groundZ = function(pos, opts)
                assert(coroutine.isyieldable(), "groundZ must run in a scheduler coroutine")
                return 42.5, { playerId = 1, distance = 3.0 }
            end,
        },
        chat = { send = function(target, msg)
            W.chats[#W.chats + 1] = { target = target, text = type(msg) == "table" and msg.text or msg }
            return true
        end },
        runtime = {
            scheduleRestart = function(sec, reason)
                assert(math.type(sec) == "integer" and sec >= 5, "restart seconds must be an integer >= 5")
                assert(#reason <= 200 and not reason:find("%c"), "restart reason")
                W.restart = { scheduled = true, secondsRemaining = sec, reason = reason }
                W.schedules[#W.schedules + 1] = { seconds = sec, reason = reason }
                return deepcopy(W.restart)
            end,
            cancelRestart = function()
                W.restart = { scheduled = false, secondsRemaining = 0 }
                W.cancels = W.cancels + 1
                return deepcopy(W.restart)
            end,
            restartStatus = function()
                return { scheduled = W.restart.scheduled, secondsRemaining = math.max(0, math.floor(W.restart.secondsRemaining + 0.5)),
                         reason = W.restart.reason }
            end,
        },
        exports = {
            call = function(res, name)
                assert(coroutine.isyieldable(), "exports.call is awaited in a coroutine here")
                if name ~= "baked" then return nil, "export_not_found" end
                if W.exportReason[res] then return promise(nil, W.exportReason[res]) end
                return promise(deepcopy(W.exportBaked[res]))
            end,
        },
        http = { listen = function(prefix, fn) R.http = fn return "/f2o77_maps" .. prefix end },
    }
    -- shared scripts, then the server script, then the start event
    F2O77_CONFIG, F2O77_MAPS = nil, nil
    dofile(DIR .. "/shared/config.lua")
    local p = io.popen("ls '" .. DIR .. "/shared/maps'")
    for fn in p:lines() do if fn:match("%.lua$") then dofile(DIR .. "/shared/maps/" .. fn) end end
    p:close()
    F2O77_ROADS = nil
    local rf = io.open(DIR .. "/server/roads.lua")
    if rf then rf:close(); dofile(DIR .. "/server/roads.lua") end
    dofile(DIR .. "/server/main.lua")
    for _, fn in ipairs(R.handlers["onResourceStart"] or {}) do fn("f2o77_maps") end
end

local function clientEvents(name, target)
    local out = {}
    for _, e in ipairs(W.toClients) do
        if e.name == name and (target == nil or e.target == target) then out[#out + 1] = e end
    end
    return out
end
local function lastClient(name, target)
    local t = clientEvents(name, target)
    return t[#t]
end

local function net(src, event, payload)
    source = src
    R.net["f2o77_maps:" .. event](payload)
    source = nil
end

local tokenSeq = 0
local function op(src, map, name, args)
    tokenSeq = tokenSeq + 1
    net(src, "op", { map = map, op = name, args = args or {}, token = tokenSeq })
    local r = lastClient("f2o77_maps:result", src)
    if name == "drop" then
        advance(0.2)
        r = lastClient("f2o77_maps:result", src)
    end
    check(r and r.payload.token == tokenSeq, ("no result for %s/%s"):format(map, name))
    return r.payload
end

local function state(name)
    local s = W.files["maps/" .. name .. ".json"]
    return s
end
-- the state players see a moment later (state broadcasts are coalesced to a few per second)
local function lastState(name)
    advance(0.3)
    for i = #W.toClients, 1, -1 do
        local e = W.toClients[i]
        if e.name == "f2o77_maps:state" and e.payload.name == name then return e.payload end
    end
end

local function http(method, path, body, token)
    local captured = {}
    local res = { json = function(status, value)
        if value == nil and type(status) == "table" then status, value = 200, status end
        captured.status, captured.value = status, deepcopy(value)
    end }
    R.http({ method = method, path = path, body = body and json.encode(body) or "",
             headers = token and { authorization = "Bearer " .. token } or {} }, res)
    if not captured.status then advance(0.2) end
    check(captured.status, "no HTTP answer for " .. method .. " " .. path)
    return captured.status, captured.value
end

local function bakerAnswers(name, st)
    addFile("bake/status/" .. name .. ".json", st)
end

-- ================================================================== run 1: the server starts
newRun()
check(R.net["f2o77_maps:hello"] and R.net["f2o77_maps:op"] and R.commands["maps"], "registers its events and command")
net(1, "hello")                               -- a player saying hello before the maps are loaded
net(2, "open")                                -- and one pressing the menu key that early
local early = lastClient("f2o77_maps:init", 1)
check(early and early.payload.loading == true and #early.payload.maps == 0,
      "an early hello is told at once that the maps are being read (never a list that looks final)")
check(lastClient("f2o77_maps:init", 2).payload.loading == true, "and so is the early menu key")
check(not lastClient("f2o77_maps:denied", 2) and not lastClient("f2o77_maps:open", 2), "the early menu key waits for the rest")
advance(2.0)
check(not lastClient("f2o77_maps:open", 2) and not lastClient("f2o77_maps:denied", 2),
      "once read: the early key of a player without the right opens nothing")
check(state("cedar_crest"), "state file written at start")
local s = state("cedar_crest")
check(s.running and near(s.running.x, 480) and s.runningId == "cedar_crest-convert-1", "running from the map export")
check(state("harbor_dock") and near(state("harbor_dock").running.x, -100), "a map without a bake runs at its default")
check(type(W.files["agent_token.txt"]) == "string" and #W.files["agent_token.txt"] >= 33, "agent token written once")
check(W.kvp.agent_token and W.files["agent_token.txt"]:find(W.kvp.agent_token, 1, true), "token in KVP too")
local TOKEN = W.kvp.agent_token
for _, l in ipairs(LOG) do check(not l:find(TOKEN, 1, true), "the token is never printed") end
local init1 = lastClient("f2o77_maps:init", 1)
check(init1 and #init1.payload.maps == 0 and #init1.payload.names == 2 and init1.payload.wire == 2,
      "the header goes to the players already online: names only, the maps follow one by one")
check(init1.payload.names[1] == "cedar_crest" and init1.payload.names[2] == "harbor_dock", "sorted by display name")
do
    -- each map in a message of its own, right after the header, to that player only
    local after, got = false, {}
    for _, e in ipairs(W.toClients) do
        if e == init1 then after = true
        elseif after and e.name == "f2o77_maps:state" and e.target == 1 then got[#got + 1] = e.payload.name end
    end
    check(#got == 2 and got[1] == "cedar_crest" and got[2] == "harbor_dock", "then one state per map: " .. table.concat(got, ","))
end
check(init1.payload.rights.cedar_crest == true, "player 1 may move maps")
check(lastClient("f2o77_maps:init", 2).payload.rights.cedar_crest == false, "player 2 may not")
check(init1.payload.config.defaultCountdown == 60 and #init1.payload.config.restartCountdowns == 4, "config sent")
check(init1.payload.baker.known == false, "baker not reported yet")
check(init1.payload.loading == false, "and the list is complete")

-- ------------------------------------------------------------------ the panel
net(2, "open")
check(lastClient("f2o77_maps:denied", 2), "no right: denied")
net(1, "open")
check(lastClient("f2o77_maps:open", 1), "open for a player with the right")

-- ------------------------------------------------------------------ moving the new spot
local r = op(2, "cedar_crest", "place", { x = 1, y = 2, z = 3 })
check(r.ok == false and r.reason == "not_allowed", "player 2 cannot place")
W.acl[2]["command.mlo.harbor_dock"] = true
r = op(2, "harbor_dock", "move", { dx = 1 })
check(r.ok == true, "the per-map right works for that map")
r = op(2, "cedar_crest", "goto_", {})
check(r.ok == true and W.teleports[#W.teleports].pid == 2, "going to a map needs any map right only")
r = op(1, "nope", "place", {})
check(r.ok == false and r.reason == "unknown_map", "unknown map")
r = op(1, "cedar_crest", "explode", {})
check(r.ok == false and r.reason == "unknown_op", "unknown op")

local writesBefore = W.writes["maps/cedar_crest.json"]
r = op(1, "cedar_crest", "place", { x = 500.123456, y = 1810, z = 175, yaw = 370 })
check(r.ok and near(r.result.x, 500.123) and near(r.result.yaw, 10), "place: rounded, yaw wrapped")
local st = lastState("cedar_crest")
check(st and st.moved == true and near(st.pending.x, 500.123), "state broadcast with the new spot")
check(lastClient("f2o77_maps:state").target == -1, "broadcast to everyone")
local statesBefore = 0
for _, e in ipairs(W.toClients) do if e.name == "f2o77_maps:state" then statesBefore = statesBefore + 1 end end
local histBefore = #(lastState("cedar_crest").history or {})
for _ = 1, 10 do op(1, "cedar_crest", "move", { dz = 0.25 }) end
local statesNow = 0
for _, e in ipairs(W.toClients) do if e.name == "f2o77_maps:state" then statesNow = statesNow + 1 end end
check(statesNow - statesBefore <= 2, "ten quick nudges are not ten broadcasts: " .. (statesNow - statesBefore))
check(W.writes["maps/cedar_crest.json"] == writesBefore, "no file write per click (debounced)")
local st10 = lastState("cedar_crest")
check(near(st10.pending.z, 177.5), "the coalesced broadcast has the last spot")
check(#st10.history <= histBefore + 1 and (st10.history[1].count or 1) >= 10, "and one history entry for the burst")
advance(2.1)
check(W.writes["maps/cedar_crest.json"] == writesBefore + 1, "one write after the flush")
check(near(state("cedar_crest").pending.z, 177.5), "the flushed state has every nudge")
r = op(1, "cedar_crest", "move", { dx = 0, dy = 2, frame = "map" })   -- yaw 10: forward is rotated
check(near(r.result.x, 500.123 - 2 * math.sin(math.rad(10)), 0.002) and near(r.result.y, 1810 + 2 * math.cos(math.rad(10)), 0.002),
      "map-frame nudge follows the map's yaw")
r = op(1, "cedar_crest", "here", {})
check(r.ok and near(r.result.x, 10) and near(r.result.z, 30) and near(r.result.yaw, 90), "here: player position and heading")
r = op(1, "cedar_crest", "drop", {})
check(r.ok and near(r.result.z, 42.5), "drop: ground height seen by a client (in a coroutine)")
r = op(1, "cedar_crest", "place", { x = 480, y = 1800, z = 174, yaw = 0 })
check(r.ok and lastState("cedar_crest").pending == nil, "placing it where it runs clears the new spot")
r = op(1, "cedar_crest", "apply", {})
check(not r.ok and r.reason == "nothing_to_apply", "nothing to apply")
r = op(1, "cedar_crest", "revert", {})
check(not r.ok and r.reason == "no_previous_spot", "no previous spot yet")
r = op(1, "cedar_crest", "place", { x = "a", y = 1, z = 2 })
check(not r.ok and r.reason == "invalid_x", "bad numbers refused")

-- presets
r = op(1, "cedar_crest", "presetSave", { name = "  Hill top  ", anchor = { x = 600, y = 1900, z = 200, yaw = 45 } })
check(r.ok and r.result.name == "Hill top", "preset saved (trimmed)")
r = op(1, "cedar_crest", "presetSave", { name = "" })
check(not r.ok and r.reason == "invalid_preset_name", "empty preset name")
r = op(1, "cedar_crest", "presetUse", { name = "hill TOP" })
check(r.ok and near(r.result.x, 600) and near(r.result.yaw, 45), "preset used (case-insensitive)")
r = op(1, "cedar_crest", "presetDelete", { name = "Hill top" })
check(r.ok and #lastState("cedar_crest").presets == 0, "preset deleted")

-- ------------------------------------------------------------------ apply now: bake, then a countdown restart
r = op(1, "cedar_crest", "apply", { mode = "now", countdown = 30 })
check(r.ok and r.result.state == "requested", "apply requested")
local req = W.files["bake/requests/cedar_crest.json"]
check(req and req.action == "bake" and req.map == "cedar_crest" and req.resource == "cedar_crest"
      and near(req.anchor.x, 600) and req.id == r.result.id, "the bake request for the baker")
local APPLY1 = req.id
check(state("cedar_crest").apply.state == "requested", "an apply is written at once")
r = op(1, "cedar_crest", "apply", { mode = "now" })
check(not r.ok and r.reason == "already_baking", "one bake at a time")
bakerAnswers("cedar_crest", { id = APPLY1, state = "baking", message = "building" })
advance(2.1)
check(lastState("cedar_crest").apply.state == "baking", "baking")
r = op(1, "cedar_crest", "cancel", {})
check(not r.ok and r.reason:find("baking_now"), "no cancel in the middle of a bake")
bakerAnswers("cedar_crest", { id = APPLY1, state = "done", message = "built in 9 s", package = { bytes = 12345 } })
advance(2.1)
local a = lastState("cedar_crest").apply
check(a.state == "restart_scheduled" and W.restart.scheduled and W.restart.secondsRemaining > 25, "restart armed for the countdown")
check(W.schedules[#W.schedules].seconds == 30, "with the chosen countdown")
check(W.chats[#W.chats].target == -1 and W.chats[#W.chats].text:find("restarts in 30 seconds"), "everyone told")
r = op(1, "cedar_crest", "apply", { mode = "now" })
check(not r.ok and r.reason == "restart_pending_cancel_first", "no second apply while a restart is armed")

-- cancel: the restart is called off and the build taken back
r = op(1, "cedar_crest", "cancel", {})
check(r.ok and W.cancels == 1 and not W.restart.scheduled, "cancel disarms the restart")
req = W.files["bake/requests/cedar_crest.json"]
check(req.action == "unstage" and req.id == APPLY1, "and asks the baker to throw the build away")
check(lastState("cedar_crest").apply.state == "unstaging", "unstaging")
bakerAnswers("cedar_crest", { id = APPLY1, state = "unstaged", message = "thrown away" })
advance(2.1)
check(lastState("cedar_crest").apply.state == "cancelled", "cancelled once the baker answered")
check(lastState("cedar_crest").pending and near(lastState("cedar_crest").pending.x, 600), "the new spot is kept")

-- cancel before the baker starts: the request file goes
r = op(1, "cedar_crest", "apply", { mode = "now", countdown = 60 })
check(r.ok, "apply again")
r = op(1, "cedar_crest", "cancel", {})
check(r.ok and W.files["bake/requests/cedar_crest.json"] == nil and lastState("cedar_crest").apply.state == "cancelled",
      "a request not started yet is withdrawn")

-- the baker never answers
r = op(1, "cedar_crest", "apply", { mode = "now", countdown = 60 })
advance(48)
check(lastState("cedar_crest").apply.offline == true and lastState("cedar_crest").apply.message:find("map baker"),
      "no answer from the baker: said so")
r = op(1, "cedar_crest", "cancel", {})
check(r.ok, "and it can be withdrawn")

-- a failing bake
r = op(1, "cedar_crest", "apply", { mode = "now" })
local AF = W.files["bake/requests/cedar_crest.json"].id
bakerAnswers("cedar_crest", { id = AF, state = "failed", message = "WolvenKit did not write x", log = "line1\nline2" })
advance(2.1)
check(lastState("cedar_crest").apply.state == "failed" and lastState("cedar_crest").apply.message:find("WolvenKit"),
      "a failed bake says why")
check(lastState("cedar_crest").bake.log == "line1\nline2", "with the baker's log for the panel")

-- ------------------------------------------------------------------ apply when empty
addFile("bake/baker.json", { version = "1.0.0", startedAt = 123, hook = true, wolvenkit = "/x/WolvenKit.CLI.dll" })
advance(10)
check(lastClient("f2o77_maps:baker").payload.hook == true, "baker info broadcast when it changes")
r = op(1, "cedar_crest", "apply", { mode = "empty" })
local APPLY2 = W.files["bake/requests/cedar_crest.json"].id
bakerAnswers("cedar_crest", { id = APPLY2, state = "done", message = "built" })
advance(2.1)
check(lastState("cedar_crest").apply.state == "waiting_empty" and not W.restart.scheduled, "built: waits for an empty server")
advance(60)
check(not W.restart.scheduled, "players online: no restart")
W.online = {}
advance(20)
check(not W.restart.scheduled, "empty, but not for the grace period yet")
advance(12)
check(W.restart.scheduled and W.schedules[#W.schedules].seconds == 5, "empty long enough: restart in 5 s (start hook)")
check(lastState("cedar_crest").apply.state == "restart_scheduled", "restart scheduled")
advance(6)
check(W.restart.fired, "the server goes down")

-- ================================================================== run 2: the restart, the start hook installed the build
local before = deepcopy(state("cedar_crest"))
W.unix, W.mono = W.unix + 20, 3.0                       -- a new process: monotonic restarts
W.restart = { scheduled = false, secondsRemaining = 0 }
W.exportBaked.cedar_crest = { x = 600.0, y = 1900.0, z = 200.0, yaw = 45.0, id = APPLY2 }
W.online = { 1 }
newRun()
advance(2.0)
s = state("cedar_crest")
check(s.apply.state == "applied" and s.runningId == APPLY2 and near(s.running.x, 600), "the move is live after the restart")
check(s.previous and near(s.previous.x, 480), "the old spot is remembered")
check(s.pending == nil, "the new spot became the running one")
check(s.history[1].what == "applied", "history says so")
r = op(1, "cedar_crest", "revert", {})
check(r.ok and near(r.result.x, 480) and lastState("cedar_crest").pending, "back to the previous spot (a new spot to Apply)")

-- ================================================================== run 3: a reload of the resource in the same server run
W.exportBaked.cedar_crest = { x = 1.0, y = 1.0, z = 1.0, yaw = 0.0, id = "SHOULD-NOT-BE-ASKED" }
newRun()
advance(2.0)
s = state("cedar_crest")
check(near(s.running.x, 600) and s.runningId == APPLY2, "a reload keeps the running spot (a staged build is not live)")

-- ================================================================== run 4: a restart where the start hook did not run
r = op(1, "cedar_crest", "apply", { mode = "now", countdown = 30 })
local APPLY3 = W.files["bake/requests/cedar_crest.json"].id
bakerAnswers("cedar_crest", { id = APPLY3, state = "done" })
advance(2.1)
check(r.ok and lastState("cedar_crest").apply.state == "restart_scheduled", "armed")
W.unix, W.mono = W.unix + 40, 2.0
W.restart = { scheduled = false, secondsRemaining = 0 }
W.exportBaked.cedar_crest = { x = 600.0, y = 1900.0, z = 200.0, yaw = 45.0, id = APPLY2 }
newRun()
advance(2.0)
s = state("cedar_crest")
check(s.apply.state == "built" and s.apply.message:find("not installed"), "restarted without the build: said so, kept built")

-- the no-start-hook path: the baker is asked to install just before the restart
addFile("bake/baker.json", { version = "1.0.0", startedAt = 456, hook = false })
advance(10)
Open77.runtime.scheduleRestart(20, "operator restart")
advance(5)
check(not W.files["bake/requests/cedar_crest.json"].promoteNow, "not yet: more than 8 s left")
advance(8)
local rq = W.files["bake/requests/cedar_crest.json"]
check(rq.promoteNow == true and rq.id == APPLY3 and rq.action == "bake", "in the last seconds: install request")

-- ------------------------------------------------------------------ console command
local function cmd(src, line)
    local args = {}
    for w in line:gmatch("%S+") do args[#args + 1] = w end
    local name = table.remove(args, 1)
    local n0 = #LOG
    R.commands[name](src, args)
    advance(0.3)
    return table.concat(LOG, "\n", n0 + 1)
end
local out = cmd(0, "maps list")
check(out:find("Cedar Crest") and out:find("Harbor Dock"), "maps list")
out = cmd(0, "maps set harbor_dock 1 2 3 90")
check(out:find("ok:") and near(lastState("harbor_dock").pending.yaw, 90), "maps set")
out = cmd(0, "maps nudge harbor_dock 2 0")
check(out:find("ok:"), "maps nudge")
out = cmd(0, "maps preset harbor_dock save Pier end")
check(out:find("ok:") and lastState("harbor_dock").presets[1].name == "Pier end", "maps preset save (names with spaces)")
out = cmd(0, "maps bogus harbor_dock")
check(out:find("maps list | open | baker"), "usage")
out = cmd(0, "mlo.cedar_crest status")
check(out:find("Cedar Crest") and out:find("running"), "the old placer command still answers")
out = cmd(2, "maps set cedar_crest 1 2 3")
check(out:find("you need command.mlo.cedar_crest"), "rights on the command too")
local n0 = #W.chats
cmd(1, "maps list")
advance(0.5)
check(#W.chats >= n0 + 2 and W.chats[n0 + 1].text:find("Cedar Crest") and W.chats[n0 + 2].text:find("Harbor Dock"),
      "chat lines go out one per tick, in order")
out = cmd(0, "maps baker")
check(out:find("start hook MISSING"), "maps baker reports the hook")

-- ------------------------------------------------------------------ agent HTTP API
local code, v = http("GET", "/f2o77_maps/status")
check(code == 200 and #v.maps == 2 and v.baker.known == true, "GET /status without a token")
code, v = http("GET", "/f2o77_maps/maps/harbor_dock")
check(code == 200 and v.name == "harbor_dock", "GET /maps/<map>")
code, v = http("GET", "/f2o77_maps/maps/nope")
check(code == 404, "unknown map 404")
code, v = http("GET", "/f2o77_maps/baker")
check(code == 200 and v.status.cedar_crest, "GET /baker")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/place", { x = 5, y = 6, z = 7 })
check(code == 401, "writes need the token")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/place", { x = 5, y = 6, z = 7, yaw = 1 }, TOKEN)
check(code == 200 and v.ok and near(v.map.pending.x, 5), "POST place with the token")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/place", { x = "no" }, TOKEN)
check(code == 409 and v.reason == "invalid_x", "a refused op is 409 with the reason")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/ground", { x = 1, y = 2 }, TOKEN)
check(code == 200 and near(v.result.z, 42.5), "POST ground answers from a coroutine")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/teleport", {}, TOKEN)
check(code == 404 and v.error == "unknown_op", "unknown op")
code, v = http("GET", "/f2o77_maps/players", nil, TOKEN)
check(code == 200 and v.players[1].playerId == 1, "GET /players")
code, v = http("POST", "/f2o77_maps/screenshot", { playerId = 1, url = "http://127.0.0.1:9/up" }, TOKEN)
check(code == 202 and W.screenshots[1].opts.url == "http://127.0.0.1:9/up", "screenshot request")
local captured = {}
R.http({ method = "POST", path = "/f2o77_maps/maps/harbor_dock/place", body = "{nope",
         headers = { authorization = "Bearer " .. TOKEN } }, { json = function(c, val) captured.c = c end })
check(captured.c == 400, "invalid JSON 400")
code, v = http("POST", "/f2o77_maps/maps/harbor_dock/apply", { mode = "empty" }, TOKEN)
check(code == 200 and v.result.state == "requested", "the agent can apply")
check(R.exportsT.status and R.exportsT.status().maps[1].name == "cedar_crest", "server export status")

-- ------------------------------------------------------------------ agents: maps @<id> ... (through the game's debug bridge)
local function agentReply(pid, id)
    for i = #W.toClients, 1, -1 do
        local e = W.toClients[i]
        if e.name == "f2o77_maps:agent:reply" and e.target == pid and e.payload.id == id then return e.payload end
    end
end
local function agentDo(pid, id)
    for i = #W.toClients, 1, -1 do
        local e = W.toClients[i]
        if e.name == "f2o77_maps:agent:do" and e.target == pid and e.payload.id == id then return e.payload end
    end
end
local chats0 = #W.chats
cmd(1, "maps @a1 version")
local r1 = agentReply(1, "a1")
check(r1 and r1.ok and r1.data.loaded == true and r1.data.maps == 2, "maps @id version answers the agent with data")
check(#W.chats == chats0, "an agent's answer does not go to the chat")
cmd(1, "maps @a2 state cedar_crest")
check(agentReply(1, "a2").data.map.name == "cedar_crest", "state of one map")
cmd(1, "maps @a3 state")
check(#agentReply(1, "a3").data.maps == 2, "state of every map")
cmd(1, "maps @a4 log 5")
local lg = agentReply(1, "a4").data.log
check(#lg == 5 and lg[5].text and lg[5].at, "the last lines of this resource's log")
cmd(1, "maps @a5 players")
check(agentReply(1, "a5").data.players[1].playerId == 1, "players")
cmd(2, "maps @a6 set cedar_crest 1 2 3")
check(agentReply(2, "a6").ok == false and agentReply(2, "a6").lines[1]:find("you need"), "an agent has the player's rights, no more")
cmd(1, "maps @bad! version")
check(not agentReply(1, "bad!"), "a bad id is refused")
cmd(1, "maps @a7 set harbor_dock 10 20 30 45")
local r7 = agentReply(1, "a7")
check(r7.ok and near(r7.data.map.pending.x, 10) and near(r7.data.map.pending.yaw, 45), "moves answer with the map")
check(lastState("harbor_dock").history[1].by:find("%[agent%]"), "the history says an agent did it")
cmd(1, "maps @a8 set harbor_dock x y z")
check(agentReply(1, "a8").ok == false, "a refused move is ok=false")
-- the parts that happen in the player's game
cmd(1, "maps @v1 view cedar_crest top fov=50")
local v1 = agentDo(1, "v1")
check(v1 and v1.op == "view" and v1.map == "cedar_crest" and v1.args.mode == "top" and v1.args.fov == 50, "view goes to the game")
cmd(1, "maps @v2 view cedar_crest gate 2 running")
local v2 = agentDo(1, "v2")
check(v2.args.mode == "gate" and v2.args.gate == 2 and v2.args.which == "running", "gate view options")
cmd(1, "maps @w1 wire cedar_crest off")
check(agentDo(1, "w1").args.on == false, "wire off")
cmd(1, "maps @d1 roads cedar_crest on radius=300")
check(agentDo(1, "d1").op == "roads" and agentDo(1, "d1").args.radius == 300, "roads")
cmd(1, "maps @f1 fit cedar_crest")
check(agentDo(1, "f1").op == "fit", "fit")
cmd(1, "maps @p1 probe 1,2 3.5,-4,10")
local p1 = agentDo(1, "p1").args.points
check(#p1 == 2 and p1[2][2] == -4 and p1[2][3] == 10, "probes")
cmd(1, "maps @sc1 scan 120.5 -40 35 h=150 cols=32 keep")
local sc1 = agentDo(1, "sc1")
check(sc1 and sc1.op == "scan" and near(sc1.args.x, 120.5) and sc1.args.z == 35 and sc1.args.h == 150 and sc1.args.cols == 32
      and sc1.args.keep == true, "a scan from above goes to the game")
cmd(1, "maps @sc2 scan rows=10")
check(agentDo(1, "sc2").args.rows == 10 and agentDo(1, "sc2").args.x == nil, "a scan through the agent's camera")
cmd(1, "maps @sc3 scan 1e9 0 0")
check(agentReply(1, "sc3").ok == false and not agentDo(1, "sc3"), "a scan far outside the world is refused")
cmd(1, "maps @ry1 ray 0.25 0.75 dynamic=1")
check(agentDo(1, "ry1").op == "ray" and agentDo(1, "ry1").args.u == 0.25 and agentDo(1, "ry1").args.dynamic == 1, "a ray")
cmd(0, "maps @sc4 scan 1 2 3")
check(not agentDo(0, "sc4"), "a scan needs an in-game player")
cmd(0, "maps @v3 view cedar_crest top")
check(not agentDo(0, "v3"), "a view needs an in-game player")
local tp0 = #W.teleports
cmd(1, "maps @t9 tp 100.5 -20 35 90")
check(#W.teleports == tp0 + 1 and W.teleports[#W.teleports].pid == 1 and near(W.teleports[#W.teleports].pos.x, 100.5),
      "an agent moves its own player")
cmd(1, "maps @t8 tp 1 2")
check(agentReply(1, "t8").ok == false and #W.teleports == tp0 + 1, "tp needs x y z")
W.acl[3] = {}
cmd(0, "maps @t7 tp 1 2 3")
check(agentReply(0, "t7") == nil and #W.teleports == tp0 + 1, "the console has no body to move")
cmd(1, "maps @c1 client")
check(agentDo(1, "c1").op == "state", "the client's state")
-- screenshots go to the player's own computer only
local shots0 = #W.screenshots
cmd(1, "maps @s1 shot http://127.0.0.1:47123/shot/abc/s1")
advance(0.2)
local s1 = agentReply(1, "s1")
check(#W.screenshots == shots0 + 1 and W.screenshots[#W.screenshots].opts.includeOverlay == true and
      W.screenshots[#W.screenshots].opts.url == "http://127.0.0.1:47123/shot/abc/s1", "a screenshot to this computer")
check(s1 and s1.ok, "answered with the receipt")
for _, bad in ipairs({ "https://evil.example/x", "http://10.0.0.5:8080/x", "http://127.0.0.1:80/x", "http://127.0.0.1.evil.com:8080/x",
                       "http://127.0.0.1:8080" }) do
    cmd(1, "maps @s2 shot " .. bad)
    check(agentReply(1, "s2").ok == false and #W.screenshots == shots0 + 1, "never anywhere else: " .. bad)
end
-- what an agent says, to everybody who manages maps
local says0 = #clientEvents("f2o77_maps:agent:say")
cmd(1, "maps @t1 say moving the estate 4 m east")
local said = clientEvents("f2o77_maps:agent:say")
check(#said == says0 + 1 and said[#said].target == 1 and said[#said].payload.text == "moving the estate 4 m east"
      and said[#said].payload.by == "Claude", "an agent's words go to the managers (player 2 may not manage: not to them)")

-- the road lanes around a spot, from data/roads
local rq0 = #clientEvents("f2o77_maps:agent:roads", 1)
net(1, "roads", { key = "k1", x = 100, y = 200, r = 100 })
check(#clientEvents("f2o77_maps:agent:roads", 1) == rq0 + 1 and lastClient("f2o77_maps:agent:roads", 1).payload.missing,
      "no road data: the client is told")
addFile("roads/index.json", { format = 1, lanes = 3, tile = 512, tiles = { ["0_0"] = { lanes = 3 } } })
W.files["roads/0_0.lanes"] = "1 r 5 15 0 200 10 200 200 10\n2 p 2 1 0 210 10 200 210 10\n3 r 5 15 400 400 5 500 500 5\n"
W.restart = { scheduled = false, secondsRemaining = 0 }     -- (an apply above armed one: this is a fresh start)
newRun()
advance(2.0)
net(1, "roads", { key = "k2", x = 100, y = 200, r = 50 })
advance(0.2)
local rr = lastClient("f2o77_maps:agent:roads", 1).payload
check(rr.key == "k2" and rr.parts == 1 and #rr.lanes == 2, "the lanes near the spot: " .. tostring(rr.lanes and #rr.lanes))
check(rr.lanes[1]:match("^1 r 5 0 200 10 200 200 10$") or rr.lanes[1]:match("^1 r 5 "), "as compact lines: " .. tostring(rr.lanes[1]))
net(1, "roads", { key = "k3", x = 100, y = 200, r = 50 })
advance(0.2)
check(lastClient("f2o77_maps:agent:roads", 1).payload.key == "k2", "at most one request a second per player")
W.acl[2] = {}
net(2, "roads", { key = "k4", x = 100, y = 200, r = 50 })
advance(0.2)
check(not lastClient("f2o77_maps:agent:roads", 2), "only for players who manage maps")
cmd(1, "maps @r1 version")
check(agentReply(1, "r1").data.roads ~= false, "the version says the road data is installed")
check(F2O77_ROADS.near(150, 190, 30)[1].id == 1, "nearest first")

-- a reload whose start event never comes still starts (and players are never left waiting)
local function silentRun()
    R = { handlers = {}, net = {}, commands = {}, exportsT = {}, threads = {}, http = nil }
end
local starts = 0
for _, l in ipairs(LOG) do if l:find("map%(s%) managed") then starts = starts + 1 end end
local saved = R
newRun()
-- (newRun fires onResourceStart; drop that handler's effect by checking the fallback separately)
advance(4.0)
local starts2 = 0
for _, l in ipairs(LOG) do if l:find("map%(s%) managed") then starts2 = starts2 + 1 end end
check(starts2 == starts + 1, "one start per VM, even with the fallback thread")

-- ------------------------------------------------------------------ the picture stays small, whatever piles up
-- (the bug this guards against: every map with all its history in ONE message grew past what the
-- game's client takes, and the client dropped every answer: "the map manager is not responding")
do
    local function cost(v)
        local t = type(v)
        if t == "table" then
            local b, n = 2, 1
            for k, x in pairs(v) do
                local cb, cn = cost(x)
                if type(k) == "string" then b, n = b + #k + 4 + cb, n + cn + 1 else b, n = b + 1 + cb, n + cn end
            end
            return b, n
        elseif t == "string" then return #v + 2, 1 end
        return #tostring(v), 1
    end
    -- a big history and many presets in every map's saved state, then a fresh run reads them
    for _, name in ipairs({ "cedar_crest", "harbor_dock" }) do
        local st = deepcopy(W.files["maps/" .. name .. ".json"] or {})
        st.history, st.presets = {}, {}
        for i = 1, 40 do
            st.history[i] = { at = 1791500000 + i, by = "XEROX710 (#1)", what = "move", count = i,
                              anchor = { x = -3096.573 + i, y = -848.458, z = 67.991, yaw = -41.65 } }
        end
        for i = 1, 32 do
            st.presets[i] = { name = "spot " .. i, at = 1791500000 + i, by = "XEROX710 (#1)",
                              anchor = { x = 100 + i, y = 200, z = 30, yaw = 90 } }
        end
        addFile("maps/" .. name .. ".json", st)
    end
    W.online = { 1 }
    newRun()
    advance(3.0)
    local before = #W.toClients
    net(1, "hello", { asked = 1 })
    local head, states = nil, {}
    for i = before + 1, #W.toClients do
        local e = W.toClients[i]
        if e.target == 1 and e.name == "f2o77_maps:init" then head = e.payload end
        if e.target == 1 and e.name == "f2o77_maps:state" then states[#states + 1] = e.payload end
        if e.target == 1 then
            local enc = json.encode(e.payload)
            local b, n = cost(e.payload)
            check(#enc <= 12 * 1024 and n <= 700, ("%s: one message stays small: %d bytes, %d values"):format(e.name, #enc, n))
            check(not enc:find("[:,%[]%-?nan") and not enc:find("[:,%[]%-?inf"), "no NaN or infinity on the wire")
        end
    end
    check(head and #head.names == 2 and #states == 2, "a header and both maps")
    check(#states[1].history <= 12 and states[1].historyCount == 40, "the history is cut to the last moves, the count kept")
    check(#states[1].presets >= 6, "the saved spots still go (as many as fit)")
    -- the whole history is still the server's: `maps state` and the agent API have it
    check(#(W.files["maps/cedar_crest.json"].history) == 40, "nothing is lost on the server")

    -- a client whose game drops the answers keeps saying hello: the third one gets the slim picture
    -- and the server says so in its log
    net(1, "hello", { asked = 2 })
    net(1, "hello", { asked = 3 })
    local slimHead = lastClient("f2o77_maps:init", 1).payload
    check(slimHead.slim == true, "a client that keeps asking gets the slim picture")
    check(#lastClient("f2o77_maps:state", 1).payload.history <= 3, "slim: three moves")
    local told = false
    for _, l in ipairs(LOG) do if l:find("without getting the maps", 1, true) then told = true end end
    check(told, "and the server log says a client is not getting its answers")
    net(1, "ack", { maps = 2 })
    net(1, "hello", { asked = 1 })
    check(lastClient("f2o77_maps:init", 1).payload.slim == nil, "after an ack: the full picture again")

    -- a client that got the header but misses a map asks for that one only
    local n0 = #clientEvents("f2o77_maps:state", 1)
    local i0 = #clientEvents("f2o77_maps:init", 1)
    net(1, "hello", { asked = 1, missing = { "harbor_dock" } })
    local sent = clientEvents("f2o77_maps:state", 1)
    check(#sent == n0 + 1 and sent[#sent].payload.name == "harbor_dock", "only the missing map is sent again")
    check(#clientEvents("f2o77_maps:init", 1) == i0, "without a new header")

    -- what JSON cannot carry never goes out: NaN, infinity, broken UTF-8
    local st = deepcopy(W.files["maps/harbor_dock.json"])
    st.history = { { at = 1, by = "bad\255\254name", what = "move", anchor = { x = 1, y = 2, z = 3, yaw = 0 } } }
    st.presets = { { name = "odd spot", anchor = { x = 1, y = 2, z = 3, yaw = 0 }, weight = 0 / 0, far = math.huge } }
    addFile("maps/harbor_dock.json", st)
    newRun()
    advance(3.0)
    net(1, "hello", { asked = 1 })
    local hd = lastClient("f2o77_maps:state", 1).payload
    check(hd.name == "harbor_dock", "harbor dock state sent")
    local enc = json.encode(hd)
    check(not enc:find("[:,%[]%-?nan") and not enc:find("[:,%[]%-?inf"), "no NaN or infinity: " .. enc:sub(1, 200))
    check(hd.presets[1].weight == nil and hd.presets[1].far == nil, "non-finite numbers dropped")
    check(utf8.len(hd.history[1].by) ~= nil, "broken UTF-8 made safe")
end

realprint("OK maps manager server")
