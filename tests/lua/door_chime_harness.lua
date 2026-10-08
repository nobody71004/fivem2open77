-- Runs a rendered door_chime.lua against a fake Open77 server and checks when the chime plays.
-- usage: lua5.4 door_chime_harness.lua <rendered door_chime.lua>
local script = assert(arg[1], "rendered script path")

local clock = 0.0
local cars = {}            -- id -> server snapshot
local handlers = {}        -- event -> { fn... }
local threads = {}
local sounds = {}          -- handle -> { playing, pos, opts }
local nextHandle = 0
local log = {}

Open77 = {
    time = { monotonic = function() return clock end },
    vehicles = {
        get = function(id) return cars[id] end,
        all = function()
            local out = {}
            for _, c in pairs(cars) do out[#out + 1] = c end
            return out
        end,
    },
}
function GetCurrentResourceName() return "car_chime" end
function print(...) log[#log + 1] = table.concat({ ... }, " ") end
function AddEventHandler(name, fn)
    handlers[name] = handlers[name] or {}
    table.insert(handlers[name], fn)
end
function CreateThread(fn) threads[#threads + 1] = coroutine.create(fn) end
function Wait(ms) coroutine.yield(ms) end
function Play3DSound(asset, pos, opts)
    assert(asset == "audio/door_chime.mp3", asset)
    assert(opts.loop and opts.duration and opts.duration > 0, "network loops need a duration")
    nextHandle = nextHandle + 1
    sounds[nextHandle] = { playing = true, pos = pos, opts = opts }
    return nextHandle
end
function StopSound(h) sounds[h].playing = false return true end
function DestroySound(h) sounds[h] = nil return true end
function SetSoundPosition(h, p) sounds[h].pos = p return true end

local function fire(name, ...)
    for _, fn in ipairs(handlers[name] or {}) do fn(...) end
end
local function step(dt)          -- advance time and run the poll thread once
    clock = clock + dt
    for _, co in ipairs(threads) do
        if coroutine.status(co) ~= "dead" then assert(coroutine.resume(co)) end
    end
end
local function playing()
    local n = 0
    for _, s in pairs(sounds) do if s.playing then n = n + 1 end end
    return n
end
local function check(cond, msg)
    if not cond then
        io.stderr:write("FAIL: " .. msg .. "\n" .. table.concat(log, "\n") .. "\n")
        os.exit(1)
    end
end

assert(loadfile(script))()
local function car(id, record)
    cars[id] = { id = id, record = record, bucket = 0, doors = 0, occupants = {},
                 position = { x = 1, y = 2, z = 3 } }
    fire("onVehicleCreated", tostring(id), "freeroam", record)
    return cars[id]
end

local c = car(7, "Vehicle.car")
local other = car(8, "Vehicle.other")
step(0)                                   -- thread starts, scans all()
check(playing() == 0, "silent while closed")

-- player gets in: the ledger's entering flag only blips -> no chime from it alone
c.occupants = { { playerId = 1, seat = "seat_front_left", entering = true } }
fire("onPlayerEnteringVehicle", "1", "7", "seat_front_left")
check(playing() == 0, "the entering blip does not start the chime")
c.occupants = { { playerId = 1, seat = "seat_front_left" } }
fire("onVehicleOccupancyChanged", "7", "11")
-- the entering client reports its door: chime starts on that update, no poll needed
c.doors = 1
fire("onVehicleUpdated", "7", "12")
check(playing() == 1, "chime starts when the door opens")
step(0.1)
check(playing() == 1, "keeps playing while the door is open")
-- door shut -> stops immediately on the update, no tail
c.doors = 0
fire("onVehicleUpdated", "7", "13")
check(playing() == 0, "chime stops the instant the door closes")

-- a script opens the passenger door (canonical mask bit 1)
c.doors = 2
fire("onVehicleUpdated", "7", "13")
check(playing() == 1, "canonical door open starts the chime")
c.position = { x = 5, y = 5, z = 5 }
step(0.1)
local h = next(sounds)
check(sounds[h].pos.x == 5, "chime follows the car")
c.doors = 16                               -- trunk only: not a cabin door
fire("onVehicleUpdated", "7", "14")
check(playing() == 0, "trunk does not chime")

-- getting out: exiting window
c.occupants = { { playerId = 1, seat = "seat_front_left", exiting = true } }
step(0.1)                                 -- even without an event the poll catches it
check(playing() == 1, "exit chimes")
cars[7] = nil
fire("onVehicleRemoved", "7", "deleted")
check(playing() == 0, "removed car stops")

-- other records never chime
other.doors = 1
fire("onVehicleUpdated", "8", "2")
step(0.1)
check(playing() == 0, "other models are ignored")

-- a car that existed before the resource started is picked up by the first scan
check(#log > 0 and log[1]:find("door chime ready"), "ready line logged")
print("ok")
io.stdout:write("door chime harness: ok (" .. #log .. " log lines)\n")
