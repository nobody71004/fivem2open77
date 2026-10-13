-- fivem2open77 car hub (server).  See open77.lua for what it is for.
--
-- data/pending.json   written by the Studio's installer: cars whose package waits for a restart
--                     [{ "name", "version", "record", "display", "staged_at" (unix seconds) }]
-- data/installed.json written here: what came online (or failed to) after a restart
-- data/status.json    written here for the Studio: waiting cars, what went live, restart state.
--                     Only when something in it changes: every file written into a resource folder
--                     makes the server re-scan that resource, which stalls its tick for a moment, and
--                     a stalled tick makes a fast car's next motion sample look like a jump (the car
--                     loses its lease and snaps back -- an "invisible wall").
-- data/config.json    settings (all optional):
--   restart_when_empty  true    restart once nobody has been online for empty_grace_seconds
--   empty_grace_seconds 60
--   daily_restart_utc   ""      "HH:MM" (UTC): restart then if cars are waiting, warning players
--   max_wait_minutes    0       >0: restart after this long even with players online, with a warning
--   warn_seconds        300     countdown players get before a restart that happens while they play
--   announce            true    tell players about waiting and new cars
--
-- Neon kits: every converted car resource announces itself ("f2o77:car": record, appearance, neon
-- colours) when it starts and when this hub starts ("f2o77:hubReady").  /neon <colour> switches
-- the driver's car to that colour's appearance with Open77.vehicles.setAppearance, which every
-- player sees; /neon off goes back to the car's own appearance.  The kit shines with the car's
-- lights: the hub switches them on with a colour and when a driver gets into a car whose neon is on.
local HUB_VERSION = "1.1.3"
local CHECK_MS = 5000
local DEFAULTS = {
    restart_when_empty = true, empty_grace_seconds = 60, daily_restart_utc = "",
    max_wait_minutes = 0, warn_seconds = 300, announce = true,
}

local config = {}
local installed = {}          -- name -> { version, record, display, state, at, error? }
local waiting = {}            -- pending entries that need a restart
local emptySince = nil
local armedBy = nil           -- reason of the restart this hub scheduled
local lastDaily = -1          -- day number of the last daily restart armed
local started = 0             -- unix time this server process started
local checks = 0
local lastStatus = nil        -- signature of the last status.json written

local function log(fmt, ...)
    print(("[f2o77_hub] " .. fmt):format(...))
end

local function now() return Open77.time.unix() end

local function readJson(path, fallback)
    local ok, exists = pcall(Open77.io.exists, path)
    if not ok or not exists then return fallback end
    local value, err = Open77.io.readJson(path)
    if value == nil then
        log("cannot read data/%s: %s", path, tostring(err))
        return fallback
    end
    return value
end

local function writeJson(path, value)
    local ok, err = Open77.io.writeJson(path, value)
    if not ok then log("cannot write data/%s: %s", path, tostring(err)) end
end

local function loadConfig()
    local c = readJson("config.json", {})
    local out = {}
    for k, v in pairs(DEFAULTS) do
        local x = type(c) == "table" and c[k]
        if x ~= nil and type(x) == type(v) then out[k] = x else out[k] = v end
    end
    out.empty_grace_seconds = math.max(0, math.floor(out.empty_grace_seconds))
    out.warn_seconds = math.max(5, math.floor(out.warn_seconds))
    out.max_wait_minutes = math.max(0, out.max_wait_minutes)
    return out
end

local function playerCount()
    local all = Open77.players.all()
    return type(all) == "table" and #all or 0
end

local function resources()
    local by = {}
    for _, e in ipairs(Open77.resource.list() or {}) do by[e.name] = e end
    return by
end

local function isCar(e) return type(e.record) == "string" and e.record ~= "" end
local function carName(e)
    if type(e.display) == "string" and e.display ~= "" then return e.display end
    return e.name
end

-- names of the cars in a list (their sibling resources, such as a door chime, are not cars)
local function namesOf(list)
    local t = {}
    for _, e in ipairs(list) do
        if isCar(e) then t[#t + 1] = carName(e) end
    end
    if #t == 0 and #list > 0 then return "car updates" end
    return table.concat(t, ", ")
end

local function say(target, text)
    if not config.announce then return end
    if target == -1 then Open77.chat.broadcast("[cars] " .. text) else Open77.chat.send(target, "[cars] " .. text) end
end

-- ------------------------------------------------------------------ pending -> live
-- A car is live only after a server start that came AFTER it was staged: a resource reload picks
-- up its new Lua and even its new version string at once, but the server declares preload
-- packages (what players download) only when it starts.
local function refresh()
    local pending = readJson("pending.json", {})
    if type(pending) ~= "table" then pending = {} end
    local by = #pending > 0 and resources() or {}
    local newWaiting, changed = {}, false
    for _, p in ipairs(pending) do
        if type(p) == "table" and type(p.name) == "string" then
            local e = by[p.name]
            local done = installed[p.name]
            local staged = tonumber(p.staged_at) or 0
            local already = done and done.version == p.version and (done.at or 0) >= staged
            if not already then
                if staged < started and e and e.state == "running" then
                    installed[p.name] = { version = e.version, record = p.record, display = p.display,
                                          state = "live", at = now() }
                    changed = true
                    log("%s %s is live (%s)", p.name, tostring(e.version), tostring(p.record))
                elseif staged < started then
                    -- staged before this start and still not running: the restart did not bring it up
                    installed[p.name] = { version = p.version, record = p.record, display = p.display,
                                          state = "failed", at = now(),
                                          error = e and (e.error or e.state) or "not loaded (check resources.load)" }
                    changed = true
                    log("%s did not start after the restart: %s", p.name, tostring(installed[p.name].error))
                else
                    newWaiting[#newWaiting + 1] = p
                end
            end
        end
    end
    waiting = newWaiting
    if changed then writeJson("installed.json", installed) end
end

-- ------------------------------------------------------------------ when to restart
local function parseHHMM(s)
    local h, m = tostring(s or ""):match("^(%d%d?):(%d%d)$")
    h, m = tonumber(h), tonumber(m)
    if h and m and h < 24 and m < 60 then return h * 60 + m end
end

local function decide()
    if #waiting == 0 then
        emptySince = nil
        return nil
    end
    local t, players = now(), playerCount()
    if players == 0 then
        emptySince = emptySince or t
        if config.restart_when_empty and t - emptySince >= config.empty_grace_seconds then
            return "nobody is online", 10
        end
    else
        emptySince = nil
    end
    local target = parseHHMM(config.daily_restart_utc)
    if target then
        local day = math.floor(t / 86400)
        local minute = math.floor((t % 86400) / 60)
        if minute >= target and minute < target + 60 and lastDaily ~= day then
            lastDaily = day
            return "daily restart", players > 0 and config.warn_seconds or 10
        end
    end
    if config.max_wait_minutes > 0 then
        local oldest = t
        for _, p in ipairs(waiting) do oldest = math.min(oldest, tonumber(p.staged_at) or t) end
        if t - oldest >= config.max_wait_minutes * 60 then
            return ("cars waited %d min"):format(math.floor((t - oldest) / 60)), players > 0 and config.warn_seconds or 10
        end
    end
end

local function arm(reason, seconds)
    local st = Open77.runtime.restartStatus()
    if st and st.scheduled then return end      -- an operator (or we) already armed one
    local why = ("Adding new cars: %s (%s)"):format(namesOf(waiting), reason)
    if #why > 190 then why = why:sub(1, 187) .. "..." end
    local ok, err = Open77.runtime.scheduleRestart(seconds, why)
    if ok then
        armedBy = reason
        log("restart in %d s: %s", seconds, why)
        if playerCount() > 0 then
            say(-1, ("Server restarts in %d s to add: %s. Relaunch through the launcher to get them."):format(seconds, namesOf(waiting)))
        end
    else
        log("cannot schedule the restart: %s", tostring(err))
    end
end

local CONFIG_KEYS = {}
for k in pairs(DEFAULTS) do CONFIG_KEYS[#CONFIG_KEYS + 1] = k end
table.sort(CONFIG_KEYS)

local function writeStatus(force)
    local st = Open77.runtime.restartStatus() or {}
    local names = {}
    for name in pairs(installed) do names[#names + 1] = name end
    table.sort(names)
    local inst, sig = {}, {}
    for _, name in ipairs(names) do
        local e = installed[name]
        inst[#inst + 1] = { name = name, version = e.version, record = e.record, display = e.display,
                            state = e.state, at = e.at, error = e.error }
        sig[#sig + 1] = ("%s=%s/%s/%s"):format(name, tostring(e.version), tostring(e.state), tostring(e.at))
    end
    for _, p in ipairs(waiting) do
        sig[#sig + 1] = ("w:%s@%s@%s"):format(tostring(p.name), tostring(p.version), tostring(p.staged_at))
    end
    local scheduled = st.scheduled == true
    sig[#sig + 1] = ("r:%s|%s|%s|%s"):format(tostring(scheduled), tostring(st.reason), tostring(armedBy), tostring(started))
    for _, k in ipairs(CONFIG_KEYS) do sig[#sig + 1] = ("c:%s=%s"):format(k, tostring(config[k])) end
    sig = table.concat(sig, ";")
    if sig == lastStatus and not force then return false end
    lastStatus = sig
    local t = now()
    local left = tonumber(st.secondsRemaining)
    writeJson("status.json", {
        hub = HUB_VERSION, time = t, utc = Open77.time.utc(), started = started,
        waiting = waiting, installed = inst,
        restart = { scheduled = scheduled, at = scheduled and left and (t + left) or nil, secondsRemaining = left,
                    reason = st.reason, armedBy = armedBy },
        config = config,
    })
    return true
end

local function check()
    config = loadConfig()
    refresh()
    local reason, seconds = decide()
    if reason then
        arm(reason, seconds)
    elseif armedBy == "nobody is online" and playerCount() > 0 then
        -- someone joined during the short empty-server countdown: wait for the next chance
        local st = Open77.runtime.restartStatus()
        if st and st.scheduled then
            Open77.runtime.cancelRestart()
            log("restart cancelled: a player joined")
        end
        armedBy = nil
    end
    checks = checks + 1
    writeStatus()
end

-- ------------------------------------------------------------------ neon kits
local cars = {}               -- record -> what the car resource announced

AddEventHandler("f2o77:car", function(info)
    if type(info) ~= "table" or type(info.record) ~= "string" or info.record == "" then return end
    local known = cars[info.record]
    cars[info.record] = info
    local n = type(info.neon) == "table" and type(info.neon.colors) == "table" and #info.neon.colors or 0
    if not known and n > 0 then log("neon kit: %s (%d colours)", info.record, n) end
end)

local function norm(s)
    return (tostring(s or ""):lower():gsub("[^a-z0-9]+", ""))
end

local function kitOf(record)
    local info = cars[record]
    if info and type(info.neon) == "table" and type(info.neon.colors) == "table" and #info.neon.colors > 0 then
        return info
    end
end

-- A kit's lights are the car's own lights, lit with its headlights (and the headlight mode is the
-- light state Open77 keeps in step for every player).  So picking a colour, or driving off in a car
-- whose neon is on, switches the headlights on; and once the new appearance is in place on every
-- client the light mode is sent again, so the car lights the new colour's lights.
local RELIGHT_MS = 1500

local function canLight()
    return type(Open77.vehicles.getLights) == "function" and type(Open77.vehicles.setLights) == "function"
end

local function neonShowing(info, appearance)
    for _, c in ipairs(info.neon.colors) do if c.appearance == appearance then return true end end
    return false
end

local function lightsOn(vid)
    if not canLight() then return end
    if Open77.vehicles.getLights(vid) == "off" then Open77.vehicles.setLights(vid, "on") end
end

local function relight(vid)
    if not canLight() then return end
    CreateThread(function()
        Wait(RELIGHT_MS)
        local mode = Open77.vehicles.getLights(vid)
        if mode and mode ~= "off" then Open77.vehicles.setLights(vid, mode) end
    end)
end

local function neonCommand(source, args)
    if source == 0 then return log("/neon is for players sitting in a car") end
    local tell = function(text) Open77.chat.send(source, "[neon] " .. text) end
    local seat = Open77.vehicles.getPlayerSeat(source)
    if not seat then return tell("Get in a car first.") end
    local car = Open77.vehicles.get(seat.vehicleId)
    if not car then return tell("That car is gone.") end
    local info = kitOf(car.record)
    if not info then return tell("This car has no neon kit.") end
    local colors = info.neon.colors
    local current = car.appearance
    local function labelOf(app)
        for _, c in ipairs(colors) do if c.appearance == app then return c.label end end
        return "off"
    end
    local want = table.concat(args or {}, " ")
    local w = norm(want)
    if w == "" or w == "list" or w == "help" then
        local names = {}
        for _, c in ipairs(colors) do names[#names + 1] = c.label end
        return tell(("%s: %s. Now: %s. /neon <colour>, /neon next, /neon off"):format(
            tostring(info.display or car.record), table.concat(names, ", "), labelOf(current)))
    end
    if seat.seat ~= "seat_front_left" then return tell("Only the driver can switch the neon.") end
    local target, label
    if w == "off" or w == "none" then
        target, label = info.appearance, "off"
    elseif w == "next" or w == "prev" or w == "previous" then
        local idx = 0
        for i, c in ipairs(colors) do if c.appearance == current then idx = i end end
        if w == "next" then idx = idx % #colors + 1
        else idx = idx <= 1 and #colors or idx - 1 end
        target, label = colors[idx].appearance, colors[idx].label
    else
        for _, c in ipairs(colors) do
            if norm(c.key) == w or norm(c.label) == w then target, label = c.appearance, c.label break end
        end
        if not target then
            for _, c in ipairs(colors) do
                if norm(c.label):sub(1, #w) == w then target, label = c.appearance, c.label break end
            end
        end
    end
    if not target then return tell(("No colour called '%s'. /neon lists them."):format(want)) end
    local ok, reason = Open77.vehicles.setAppearance(seat.vehicleId, target)
    if not ok then return tell("Could not switch: " .. tostring(reason)) end
    if label == "off" then return tell("Neon off.") end
    lightsOn(seat.vehicleId)
    relight(seat.vehicleId)
    tell("Neon: " .. label .. " (it shines with the car's lights).")
end
if not pcall(RegisterCommand, "neon", neonCommand, false) then log("/neon is taken; use /f2o77neon") end
pcall(RegisterCommand, "f2o77neon", neonCommand, false)

-- ------------------------------------------------------------------ in a converted car
-- A driver who gets into a car with a neon kit is told about /neon, once per car and session.
--
-- A player the game will not let out never gets out: when it finds no room beside the car it
-- slides the player to another seat to try there, and back, seat after seat.  Three seats taken
-- inside LOOP_SECONDS in one converted car is that loop, not a person: the hub takes the player
-- out (the converter now gives wide cars the room, this is the safety net).
local LOOP_SECONDS = 8
local tipped = {}         -- playerId -> { [record] = true }
local seatTimes = {}      -- playerId -> { vehicle = id, times = { monotonic seconds } }
local rescued = {}        -- playerId -> monotonic seconds of the last forced exit

local function exitLoop(id, vid, car)
    local t = Open77.time.monotonic()
    local s = seatTimes[id]
    if not s or s.vehicle ~= vid then
        s = { vehicle = vid, times = {} }
        seatTimes[id] = s
    end
    local keep = {}
    for _, x in ipairs(s.times) do if t - x <= LOOP_SECONDS then keep[#keep + 1] = x end end
    keep[#keep + 1] = t
    s.times = keep
    if #keep < 3 or (rescued[id] and t - rescued[id] < 30) then return end
    rescued[id] = t
    s.times = {}
    local ok, reason = Open77.vehicles.forcePlayerOutOfVehicle(id, vid)
    log("exit loop in %s: player %d %s", tostring(car.record), id,
        ok and "taken out of the car" or ("could not be taken out (" .. tostring(reason) .. ")"))
    if ok then
        Open77.chat.send(id, "[cars] The game found no room to let you out and kept moving you between seats, "
                             .. "so you were taken out of the car.")
    end
end

AddEventHandler("onPlayerEnteredVehicle", function(playerId, vehicleId, seat)
    local id, vid = tonumber(playerId), tonumber(vehicleId)
    if not id or not vid then return end
    local car = Open77.vehicles.get(vid)
    local info = car and cars[car.record]
    if not info then return end                      -- not a converted car
    exitLoop(id, vid, car)
    if seat == "seat_front_left" and kitOf(car.record) and neonShowing(info, car.appearance) then
        lightsOn(vid)                                -- the neon shines with the car's lights
    end
    if seat == "seat_front_left" and kitOf(car.record) then
        tipped[id] = tipped[id] or {}
        if not tipped[id][car.record] then
            tipped[id][car.record] = true
            Open77.chat.send(id, ("[neon] %s has a neon kit: /neon <colour> (/neon list), /neon next, /neon off.")
                :format(tostring(info.display or car.record)))
        end
    end
end)

AddEventHandler("onPlayerDisconnected", function(playerId)
    local id = tonumber(playerId)
    if id then tipped[id], seatTimes[id], rescued[id] = nil, nil, nil end
end)

-- ------------------------------------------------------------------ players
AddEventHandler("onPlayerReady", function(playerId)
    local id = tonumber(playerId)
    if not id then return end
    if #waiting > 0 then
        say(id, ("New cars arrive at the next server restart: %s."):format(namesOf(waiting)))
    end
    local fresh = {}
    for name, e in pairs(installed) do
        if e.state == "live" and isCar(e) and now() - (e.at or 0) < 86400 then fresh[#fresh + 1] = e end
    end
    for _, e in ipairs(fresh) do
        say(id, ("New car: %s -- spawn it with /car %s"):format(carName(e), tostring(e.record)))
    end
end)

local function listCars(source)
    local lines = {}
    for name, e in pairs(installed) do
        if e.state == "live" and isCar(e) then
            lines[#lines + 1] = ("%s: /car %s%s"):format(carName(e), tostring(e.record),
                                                     kitOf(e.record) and " (neon: /neon)" or "")
        end
    end
    for _, p in ipairs(waiting) do
        if isCar(p) then lines[#lines + 1] = ("%s (after the next restart)"):format(carName(p)) end
    end
    if #lines == 0 then lines[1] = "no converted cars yet" end
    for _, l in ipairs(lines) do
        if source == 0 then log("%s", l) else Open77.chat.send(source, "[cars] " .. l) end
    end
end

-- /cars may already exist in a gamemode: then only /f2o77cars is registered
if not pcall(RegisterCommand, "cars", listCars, false) then log("/cars is taken; use /f2o77cars") end
pcall(RegisterCommand, "f2o77cars", listCars, false)

local function carhub(source, args)
    local sub = args[1] or "status"
    local reply = function(text)
        if source == 0 then log("%s", text) else Open77.chat.send(source, "[carhub] " .. text) end
    end
    if sub == "restart" then
        local secs = math.floor(tonumber(args[2]) or config.warn_seconds)
        if #waiting == 0 then return reply("no cars are waiting for a restart") end
        arm("requested", math.max(5, secs))
        reply("restart armed")
    elseif sub == "cancel" then
        Open77.runtime.cancelRestart()
        armedBy = nil
        reply("restart cancelled")
    else
        local st = Open77.runtime.restartStatus() or {}
        reply(("waiting: %s; players: %d; restart: %s"):format(#waiting > 0 and namesOf(waiting) or "none",
              playerCount(), st.scheduled and (tostring(st.secondsRemaining) .. " s") or "none"))
    end
end
if not pcall(RegisterCommand, "carhub", carhub, true) then log("cannot register /carhub") end

AddEventHandler("onResourceStart", function(name)
    if name ~= GetCurrentResourceName() then return end
    started = now() - Open77.time.monotonic()
    installed = readJson("installed.json", {})
    if type(installed) ~= "table" then installed = {} end
    config = loadConfig()
    log("car hub %s ready (server up since %s)", HUB_VERSION, tostring(math.floor(started)))
    TriggerEvent("f2o77:hubReady")          -- car resources answer with f2o77:car
end)

CreateThread(function()
    if started == 0 then
        started = now() - Open77.time.monotonic()
        installed = readJson("installed.json", {})
        if type(installed) ~= "table" then installed = {} end
    end
    while true do
        local ok, err = pcall(check)
        if not ok then log("check failed: %s", tostring(err)) end
        Wait(CHECK_MS)
    end
end)
