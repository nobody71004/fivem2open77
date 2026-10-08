-- Runs f2o77_hub's server/main.lua against a fake Open77 server.
-- usage: lua5.4 hub_harness.lua <path to server/main.lua>
local script = assert(arg[1], "main.lua path")

local function new_world(t0, uptime)
    local W = { clock = t0, mono = uptime or 0, files = {}, res = {}, players = {}, chat = {}, logs = {},
                restart = nil, handlers = {}, threads = {}, commands = {}, writes = {}, seats = {}, vehicles = {},
                events = {} }
    return W
end

local W
local function install_env()
    Open77 = {
        time = { unix = function() return W.clock end, monotonic = function() return W.mono end,
                 utc = function() return "2026-10-07T12:00:00Z" end },
        io = {
            exists = function(p) return W.files[p] ~= nil end,
            readJson = function(p) return W.files[p] end,
            writeJson = function(p, v)
                W.files[p] = v
                W.writes[p] = (W.writes[p] or 0) + 1
                return true
            end,
        },
        resource = { list = function()
            local out = {}
            for name, e in pairs(W.res) do out[#out + 1] = { name = name, version = e.version, state = e.state, error = e.error } end
            return out
        end },
        players = { all = function() local t = {} for id in pairs(W.players) do t[#t + 1] = id end return t end },
        vehicles = {
            getPlayerSeat = function(id) return W.seats[tonumber(id)] end,
            get = function(id) return W.vehicles[id] end,
            setAppearance = function(id, name)
                local v = W.vehicles[id]
                if not v then return false, "vehicle_not_found" end
                v.appearance = name
                return true
            end,
        },
        runtime = {
            restartStatus = function()
                if W.restart then return { scheduled = true, secondsRemaining = W.restart.seconds, reason = W.restart.reason } end
                return { scheduled = false, secondsRemaining = 0 }
            end,
            scheduleRestart = function(s, reason)
                assert(s >= 1 and #reason <= 200, "invalid restart request")
                W.restart = { seconds = s, reason = reason }
                return { scheduled = true, secondsRemaining = s, reason = reason }
            end,
            cancelRestart = function() W.restart = nil return { scheduled = false, secondsRemaining = 0 } end,
        },
        chat = {
            send = function(id, m) W.chat[#W.chat + 1] = { to = id, text = m } end,
            broadcast = function(m) W.chat[#W.chat + 1] = { to = -1, text = m } end,
        },
    }
    function GetCurrentResourceName() return "f2o77_hub" end
    function print(...) W.logs[#W.logs + 1] = table.concat({ ... }, " ") end
    function AddEventHandler(n, fn) W.handlers[n] = W.handlers[n] or {} table.insert(W.handlers[n], fn) end
    function RegisterCommand(n, fn, restricted)
        if W.commands[n] then error("duplicate command") end
        W.commands[n] = fn
        return true
    end
    function CreateThread(fn) W.threads[#W.threads + 1] = coroutine.create(fn) end
    function TriggerEvent(name, ...) W.events[#W.events + 1] = { name = name, args = { ... } } return true end
    function Wait(ms) coroutine.yield(ms) end
end

local function fire(n, ...) for _, fn in ipairs(W.handlers[n] or {}) do fn(...) end end
local function tick(dt)          -- advance the clocks and run the hub's loop once
    W.clock = W.clock + (dt or 5)
    W.mono = W.mono + (dt or 5)
    for _, co in ipairs(W.threads) do
        if coroutine.status(co) ~= "dead" then assert(coroutine.resume(co)) end
    end
end
local function fail(msg)
    io.stderr:write("FAIL: " .. msg .. "\n" .. table.concat(W.logs, "\n") .. "\n")
    os.exit(1)
end
local function check(c, msg) if not c then fail(msg) end end
local function boot()
    install_env()
    assert(loadfile(script))()
    fire("onResourceStart", "f2o77_hub")
    tick(0)
end
local function chatHas(pat)
    for _, m in ipairs(W.chat) do if m.text:find(pat) then return true end end
    return false
end

-- 1: server up for an hour, nothing pending
W = new_world(1000000, 3600)
W.files["config.json"] = { empty_grace_seconds = 60 }
W.players[1] = true
boot()
check(W.restart == nil, "no restart with nothing pending")
check(W.files["status.json"] and W.files["status.json"].hub == "1.1.1", "status written")
check(W.logs[1]:find("car hub 1.1.1 ready"), "ready line")

-- 1a: nothing changes: nothing is written (a write into the resource folder stalls the server tick)
local before = W.writes["status.json"]
tick(5) tick(5) tick(5)
W.players[2] = true tick(5) W.players[2] = nil tick(5)
check(W.writes["status.json"] == before, "status.json is not rewritten while nothing changes")
check(W.writes["installed.json"] == nil, "installed.json is not rewritten while nothing changes")

-- 1b: a resource reload already shows the staged version: still waits (packages need a start)
W.res["early"] = { version = "2.0.0", state = "running" }
W.files["pending.json"] = { { name = "early", version = "2.0.0", record = "Vehicle.early", display = "Early",
                              staged_at = W.clock } }
tick(5)
check(not (W.files["installed.json"] or {})["early"], "a reloaded version is not a started package")
check(#W.files["status.json"].waiting == 1, "it waits for the restart")
local w1 = W.writes["status.json"]
tick(5) tick(5)
check(W.writes["status.json"] == w1, "a waiting car alone does not rewrite the status")
W.res["early"] = nil

-- 2: the Studio stages a car while a player is online: no restart, the player is told on join
W.files["pending.json"] = { { name = "i8rpncs", version = "1.0.7", record = "Vehicle.i8rpncs",
                              display = "BMW i8 Roadster", staged_at = W.clock } }
tick(5)
check(W.restart == nil, "players online: wait")
fire("onPlayerReady", "2")
check(chatHas("next server restart: BMW i8 Roadster"), "joining player hears about the waiting car")

-- 3: everybody leaves: restart after the grace period, not before
W.players = {}
tick(5)
check(W.restart == nil, "grace period running")
tick(30) tick(30)
check(W.restart and W.restart.seconds == 10, "restart once empty for the grace period")
check(W.restart.reason:find("BMW i8 Roadster") and W.restart.reason:find("nobody is online"), "restart reason names the car")

-- 4: someone joins during the countdown: restart withdrawn
W.players[3] = true
tick(5)
check(W.restart == nil, "joining player cancels the empty-server restart")

-- 5: an operator's own restart is never cancelled or re-armed by the hub
W.restart = { seconds = 600, reason = "operator" }
tick(5)
check(W.restart and W.restart.reason == "operator", "operator restart untouched")
W.restart = nil

-- 6: max wait with players online: armed with the warning countdown
W.files["config.json"] = { max_wait_minutes = 30, warn_seconds = 300 }
W.clock = W.clock + 31 * 60
tick(5)
check(W.restart and W.restart.seconds == 300, "max wait arms a warned restart")
check(chatHas("Server restarts in 300 s"), "players warned")
local status = W.files["status.json"]
check(status.restart.scheduled == true and #status.waiting == 1, "status shows the waiting car and the restart")
check(status.restart.at and status.restart.at > W.clock, "status says when the restart happens")
local w2 = W.writes["status.json"]
W.restart.seconds = 250 tick(5)
check(W.writes["status.json"] == w2, "a running countdown does not rewrite the status")

-- 7: after the restart the car resource runs with the staged version: live, no further restart
local files = W.files
W = new_world(files["pending.json"][1].staged_at + 4000, 20)
W.files = files
W.res["i8rpncs"] = { version = "1.0.7", state = "running" }
W.players[1] = true
boot()
check(W.restart == nil, "nothing left to restart for")
check(W.files["installed.json"]["i8rpncs"].state == "live", "car marked live")
fire("onPlayerReady", "1")
check(chatHas("spawn it with /car Vehicle.i8rpncs"), "new car announced to joining players")
W.commands["cars"](1, { n = 0 })
check(chatHas("BMW i8 Roadster: /car Vehicle.i8rpncs"), "/cars lists it")

-- 8: a car that fails to start after the restart is reported, never restarted for again
W.files["pending.json"] = { { name = "badcar", version = "1.0.1", record = "Vehicle.badcar", display = "Bad",
                              staged_at = W.clock - 100 } }
W.res["badcar"] = { version = "1.0.1", state = "failed", error = "missing_dependency:archivexl" }
W.players = {}
tick(5) tick(60) tick(60)
check(W.restart == nil, "failed car does not loop restarts")
check(W.files["installed.json"]["badcar"].state == "failed", "failure recorded")
check(W.files["installed.json"]["badcar"].error == "missing_dependency:archivexl", "failure reason kept")

-- 9: daily restart time with players online
W.files["pending.json"] = { { name = "car2", version = "1.0.2", record = "Vehicle.car2", display = "Car 2",
                              staged_at = W.clock + 1 } }
W.files["config.json"] = { daily_restart_utc = "05:00", restart_when_empty = false, warn_seconds = 120 }
W.players[5] = true
local day = math.floor(W.clock / 86400)
W.clock = day * 86400 + 86400 + 5 * 3600 + 30      -- tomorrow 05:00:30 UTC
tick(5)
check(W.restart and W.restart.seconds == 120 and W.restart.reason:find("daily restart"), "daily restart armed")

-- 10: /carhub (restricted) from the console
W.restart = nil
W.commands["carhub"](0, { "status", n = 1 })
check(W.logs[#W.logs]:find("waiting: Car 2"), "carhub status")
W.commands["carhub"](0, { "restart", "30", n = 2 })
check(W.restart and W.restart.seconds == 30, "carhub restart")
W.commands["carhub"](0, { "cancel", n = 1 })
check(W.restart == nil, "carhub cancel")

-- 11: /cars taken by a gamemode: the hub still loads and offers /f2o77cars
W = new_world(5000000, 100)
install_env()
W.commands["cars"] = function() end
assert(loadfile(script))()
check(W.commands["f2o77cars"] ~= nil, "fallback command registered")

-- 12: neon kits: cars announce themselves, /neon switches the driver's car appearance
W = new_world(6000000, 100)
boot()
check(W.events[#W.events] and W.events[#W.events].name == "f2o77:hubReady", "hub asks the cars to announce")
local CAR = { resource = "biome", record = "Vehicle.biome", display = "Biome", appearance = "biome",
              neon = { default = "off", colors = {
                  { key = "white", label = "White", appearance = "biome_neon_white" },
                  { key = "hot_pink", label = "Hot Pink", appearance = "biome_neon_hot_pink" },
                  { key = "blue", label = "Blue", appearance = "biome_neon_blue" } } } }
fire("f2o77:car", CAR)
fire("f2o77:car", CAR)
fire("f2o77:car", { record = "Vehicle.plain", appearance = "plain" })
local seen = 0
for _, l in ipairs(W.logs) do if l:find("neon kit: Vehicle.biome %(3 colours%)") then seen = seen + 1 end end
check(seen == 1, "a kit is logged once")
W.vehicles[42] = { id = 42, record = "Vehicle.biome", appearance = "biome" }
W.vehicles[43] = { id = 43, record = "Vehicle.plain", appearance = "plain" }
W.seats[7] = { playerId = 7, vehicleId = 42, seat = "seat_front_left" }
W.seats[8] = { playerId = 8, vehicleId = 42, seat = "seat_front_right" }
W.seats[9] = { playerId = 9, vehicleId = 43, seat = "seat_front_left" }
local function last(id) for i = #W.chat, 1, -1 do if W.chat[i].to == id then return W.chat[i].text end end end
W.commands["neon"](7, { n = 0 })
check(last(7):find("White, Hot Pink, Blue") and last(7):find("Now: off"), "/neon lists the kit")
W.commands["neon"](7, { "hot", "pink", n = 2 })
check(W.vehicles[42].appearance == "biome_neon_hot_pink" and last(7):find("Neon: Hot Pink"), "/neon hot pink")
W.commands["neon"](7, { "next", n = 1 })
check(W.vehicles[42].appearance == "biome_neon_blue", "/neon next")
W.commands["neon"](7, { "next", n = 1 })
check(W.vehicles[42].appearance == "biome_neon_white", "/neon next wraps around")
W.commands["neon"](7, { "prev", n = 1 })
check(W.vehicles[42].appearance == "biome_neon_blue", "/neon prev")
W.commands["neon"](7, { "bl", n = 1 })
check(W.vehicles[42].appearance == "biome_neon_blue", "/neon accepts the start of a name")
W.commands["neon"](8, { "white", n = 1 })
check(W.vehicles[42].appearance == "biome_neon_blue" and last(8):find("Only the driver"), "passengers cannot switch")
W.commands["neon"](7, { "teal", n = 1 })
check(last(7):find("No colour called 'teal'"), "unknown colour")
W.commands["neon"](7, { "off", n = 1 })
check(W.vehicles[42].appearance == "biome" and last(7):find("Neon off"), "/neon off")
W.commands["neon"](9, { "white", n = 1 })
check(last(9):find("no neon kit"), "a car without a kit")
W.commands["neon"](10, { "white", n = 1 })
check(last(10):find("Get in a car"), "not in a car")

io.stdout:write("hub harness: ok\n")
