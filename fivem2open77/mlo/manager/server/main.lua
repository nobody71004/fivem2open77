-- f2o77_maps: the in-game manager of the FiveM maps fivem2open77 converted (server side).
--
-- A converted map is baked into Night City as a streaming sector (its own resource, e.g.
-- cedar_crest, with the preload packages).  Where it stands is one ANCHOR: a point and a yaw.
-- This resource owns, per map:
--   running   the anchor of the package the server started with: where every player sees it now;
--   pending   a new spot someone saved (in game, from the console or through the agent API);
--   previous  where it stood before the last move went live (one click to go back);
--   presets   named spots to come back to;
--   apply     the move in progress.
--
-- Moving a map = save a pending spot, then Apply.  The map baker (a small service next to the
-- server: `python3 -m fivem2open77.mlo.baker run`, installed with the map) builds the map's sector
-- package for that spot into a staging folder OUTSIDE the server's resources, never touching the
-- live map; this resource then restarts the server -- after a countdown, or once nobody is online
-- -- and the baker's start hook (ExecStartPre of the server unit) installs the staged build before
-- the server comes back.  Players pick the new package up when they rejoin.  This resource and
-- the baker talk through files in data/bake/: requests/<map>.json (written here: the intent),
-- status/<map>.json and baker.json (written by the baker).
--
-- Every file written under a resource folder makes the server re-scan it (a short tick stall), so
-- the state in data/maps/ is written at most every two seconds and only when it changed.
--
-- Rights: command.maps (every map) or command.mlo.<map> (one map).
local RES = GetCurrentResourceName()
local DEFAULTS = F2O77_CONFIG or {}
local MAPS = F2O77_MAPS or {}
local EV = "f2o77_maps:"
local HISTORY_MAX = 40
local PRESETS_MAX = 32

local CFG = {}
for k, v in pairs(DEFAULTS) do CFG[k] = v end   -- data/config.json is merged over these at start

-- the states of an Apply
local BUSY = { requested = true, queued = true, baking = true }                -- the baker has it
local STAGED = { built = true, restart_scheduled = true, waiting_empty = true } -- built, waits for a restart
local function isOpen(a)
    return a ~= nil and (BUSY[a.state] or STAGED[a.state] or a.state == "unstaging") and true or false
end

-- ------------------------------------------------------------------ time
local function unix()
    if Open77.time and Open77.time.unix then return math.floor(Open77.time.unix()) end
    return os.time()
end

local function uptime()
    if Open77.time and Open77.time.monotonic then return Open77.time.monotonic() end
    return (GetGameTimer and GetGameTimer() or 0) / 1000.0
end

-- the wall-clock second the server process started: the same for every (re)load in one run
local BOOT = math.floor(unix() - uptime() + 0.5)

-- every line this resource logs is also kept (the last few hundred) for `maps log`: an agent or
-- a manager in game reads what the server did without access to the server host
local LOG_KEEP = 300
local logRing, logAt = {}, 0
local function log(fmt, ...)
    local line = ("[%s] " .. fmt):format(RES, ...)
    print(line)
    logAt = logAt % LOG_KEEP + 1
    logRing[logAt] = { at = unix(), text = line:sub(#RES + 4, #RES + 403) }
end
local function recentLog(n)
    local out = {}
    for i = 0, LOG_KEEP - 1 do
        local e = logRing[(logAt - i - 1) % LOG_KEEP + 1]
        if not e or #out >= n then break end
        table.insert(out, 1, e)
    end
    return out
end

local VERSION = "?"
do
    local meta = Open77.resource and Open77.resource.metadata
    if type(meta) == "function" then
        local ok, v = pcall(meta, RES, "version")
        if ok and type(v) == "string" then VERSION = v end
    end
end

-- ------------------------------------------------------------------ anchors
local function wrapYaw(y)
    y = (tonumber(y) or 0) % 360
    if y > 180 then y = y - 360 end
    return y
end

local function round(v, d)
    local m = 10 ^ d
    return math.floor(v * m + 0.5) / m
end

local function validAnchor(a)
    if type(a) ~= "table" then return false, "anchor_must_be_a_table" end
    for _, k in ipairs({ "x", "y", "z" }) do
        local v = tonumber(a[k])
        if not v or v ~= v or math.abs(v) > 100000 then return false, "invalid_" .. k end
    end
    if a.yaw ~= nil then
        local y = tonumber(a.yaw)
        if not y or y ~= y or math.abs(y) > 1e6 then return false, "invalid_yaw" end
    end
    return true
end

local function normAnchor(a, base)
    base = base or {}
    return {
        x = round(tonumber(a.x), 3), y = round(tonumber(a.y), 3), z = round(tonumber(a.z), 3),
        yaw = round(wrapYaw(tonumber(a.yaw) or base.yaw or 0.0), 2),
    }
end

local function sameAnchor(a, b)
    if not a or not b then return a == b end
    return math.abs(a.x - b.x) < 0.01 and math.abs(a.y - b.y) < 0.01 and math.abs(a.z - b.z) < 0.01
        and math.abs(wrapYaw(a.yaw - b.yaw)) < 0.05
end

local function rotate(x, y, deg)
    local r = math.rad(deg)
    local c, s = math.cos(r), math.sin(r)
    return x * c - y * s, x * s + y * c
end

local function toWorld(p, a)
    local dx, dy = rotate(p.x or 0, p.y or 0, a.yaw or 0)
    return { x = a.x + dx, y = a.y + dy, z = a.z + (p.z or 0) }
end

local function fmtAnchor(a)
    if not a then return "-" end
    return ("%.2f %.2f %.2f yaw %.1f"):format(a.x, a.y, a.z, a.yaw)
end

-- ------------------------------------------------------------------ data files (this resource's data/)
local function ensureDir(path)
    if Open77.io.exists(path) then return true end
    return Open77.io.makeDirectory(path)
end

local function readJson(path)
    if not Open77.io.exists(path) then return nil end
    local v, err = Open77.io.readJson(path)
    if v == nil and err then log("cannot read %s: %s", path, tostring(err)) end
    return v
end

local function writeJson(path, value)
    local dir = path:match("^(.*)/[^/]+$")
    if dir then ensureDir(dir) end
    local ok, err = Open77.io.writeJson(path, value)
    if not ok then log("cannot write %s: %s", path, tostring(err)) end
    return ok, err
end

local function loadConfig()
    CFG = {}
    for k, v in pairs(DEFAULTS) do CFG[k] = v end
    local over = readJson("config.json")
    if type(over) == "table" then
        for k, v in pairs(over) do
            if DEFAULTS[k] ~= nil and type(v) == type(DEFAULTS[k]) then CFG[k] = v end
        end
    end
end

-- ------------------------------------------------------------------ state
local maps = {}          -- name -> { name, def, running, runningId, previous, pending, presets, history, apply, bake, boot }
local order = {}         -- map names, sorted by display name
local dirty = {}
local bakerInfo = nil    -- data/bake/baker.json: the baker says who it is when it starts
local loaded = false     -- the maps are read (a moment after the start): until then players wait
local waiting = {}       -- player id -> "hello" | "open": asked before the maps were read
local bakerSeen = 0      -- the last time the baker answered a request (unix)

local function stateFile(name) return "maps/" .. name .. ".json" end
local function requestFile(name) return "bake/requests/" .. name .. ".json" end
local function statusFile(name) return "bake/status/" .. name .. ".json" end

local function fileBaked(def)
    if def.baked and validAnchor(def.baked) then return normAnchor(def.baked) end
    return nil
end

local function flushOne(m)
    dirty[m.name] = nil
    writeJson(stateFile(m.name), {
        running = m.running, runningId = m.runningId, runningSource = m.runningSource, previous = m.previous,
        pending = m.pending, presets = m.presets, history = m.history, apply = m.apply, boot = m.boot,
    })
end

local function flush()
    for name in pairs(dirty) do
        if maps[name] then flushOne(maps[name]) else dirty[name] = nil end
    end
end

-- mark for the next flush; `now` writes at once (an Apply must survive a crash a second later)
local function persist(m, now)
    dirty[m.name] = true
    if now then flushOne(m) end
end

-- moves in quick succession by the same hand (nudges, a held key) are one entry, so the history
-- keeps what matters (the applies, the presets) instead of forty one-metre steps
local MERGE_SECONDS = 20
local function remember(m, who, what, anchor)
    local last, t = m.history[1], unix()
    if last and last.by == who and (what == "move" or what == "place") and (last.what == "move" or last.what == "place")
        and t - (tonumber(last.at) or 0) <= MERGE_SECONDS then
        last.at, last.anchor, last.what = t, anchor, what
        last.count = (tonumber(last.count) or 1) + 1
        return
    end
    table.insert(m.history, 1, { at = t, by = who, what = what, anchor = anchor })
    while #m.history > HISTORY_MAX do table.remove(m.history) end
end

local function summary(m)
    local d = m.def
    return {
        name = m.name, display = d.display or m.name, version = d.version, resource = d.resource or m.name,
        running = m.running, runningId = m.runningId, runningSource = m.runningSource, previous = m.previous,
        pending = m.pending, presets = m.presets, history = m.history, apply = m.apply, bake = m.bake,
        bounds = d.bounds, spawn = d.spawn,
        runningSpawn = (d.spawn and m.running) and toWorld(d.spawn, m.running) or nil,
        pendingSpawn = (d.spawn and m.pending) and toWorld(d.spawn, m.pending) or nil,
        parts = d.partCount, placements = d.placementCount, doors = d.doors,
        moved = m.pending ~= nil and not sameAnchor(m.pending, m.running),
        right = "command.mlo." .. m.name,
    }
end

-- ------------------------------------------------------------------ what goes over the wire
-- A net event may carry 48 KiB of JSON, but the game's client also counts every table, key and
-- value of it against its own budget, and a payload over that budget is dropped on the client
-- ("dropped malformed network event payload" in the game's log) without a word to the server: the
-- player's panel then waits for an answer for ever.  One message with every map and all their
-- history did exactly that once the moves piled up.  So a client gets a short header and then each
-- map in a message of its own, a map's history is cut to the last moves (the panel shows a dozen;
-- `maps state` and the agent API have it all), every message is measured before it goes out and
-- cut further when it is still big, and nothing JSON cannot carry (a non-finite number, broken
-- UTF-8, a function) goes out at all.
local WIRE_BYTES = 12 * 1024           -- per message: a quarter of the envelope
local WIRE_NODES = 700                 -- tables + keys + values per message
local WIRE_HISTORY = 12                -- moves per map in a client's picture
local mathType = math.type or function(v) return v == math.floor(v) and "integer" or "float" end

-- the approximate JSON size and node count of a value (every table, key and value is a node)
local function wireCost(v, depth)
    local t = type(v)
    if t == "table" then
        depth = depth or 0
        if depth > 16 then return 4, 1 end
        local bytes, nodes = 2, 1
        for k, x in pairs(v) do
            local b, n = wireCost(x, depth + 1)
            if type(k) == "string" then bytes, nodes = bytes + #k + 4 + b, nodes + n + 1
            else bytes, nodes = bytes + 1 + b, nodes + n end
        end
        return bytes, nodes
    elseif t == "string" then
        return #v + 2, 1
    elseif t == "number" then
        return #tostring(v), 1
    end
    return 5, 1
end

local function cleanString(s)
    if #s > 2000 then s = s:sub(1, 2000) end
    if utf8 and utf8.len and not utf8.len(s) then
        s = s:gsub("[\192-\255][\128-\191]*$", "")              -- a character cut in two at the end
        if not utf8.len(s) then s = s:gsub("[\128-\255]", "?") end -- broken inside: ASCII only
    end
    return s
end

-- a copy JSON can carry: no functions, userdata or cycles, no NaN or infinity, valid UTF-8,
-- bounded strings and depth, string or integer keys only
local function wireClean(v, depth)
    local t = type(v)
    if t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return nil end
        return v
    elseif t == "string" then
        return cleanString(v)
    elseif t == "boolean" then
        return v
    elseif t == "table" then
        depth = depth or 0
        if depth >= 12 then return nil end
        local out = {}
        for k, x in pairs(v) do
            if type(k) == "string" then out[cleanString(k)] = wireClean(x, depth + 1)
            elseif type(k) == "number" and mathType(k) == "integer" then out[k] = wireClean(x, depth + 1) end
        end
        return out
    end
    return nil
end

local function firstN(list, n)
    if type(list) ~= "table" or #list <= n then return list end
    local out = {}
    for i = 1, n do out[i] = list[i] end
    return out
end

local wireWarned = {}

-- one map as a client gets it.  `slim`: the least that still works (a client that keeps asking
-- because the full picture never reached it).
local function wireSummary(m, slim)
    local s = summary(m)
    local total = type(s.history) == "table" and #s.history or 0
    s.history = firstN(s.history, slim and 3 or WIRE_HISTORY)
    s.historyCount = total
    if type(s.bake) == "table" then
        local b = s.bake
        s.bake = { id = b.id, state = b.state, message = b.message, startedAt = b.startedAt,
                   finishedAt = b.finishedAt, promotedAt = b.promotedAt, anchor = b.anchor,
                   log = (not slim and type(b.log) == "string") and b.log:sub(-400) or nil }
    end
    if type(s.apply) == "table" and s.apply.package ~= nil then
        local a = {}
        for k, v in pairs(s.apply) do if k ~= "package" then a[k] = v end end
        s.apply = a
    end
    -- a saved spot needs its name and anchor on the wire, nothing else
    if type(s.presets) == "table" then
        local ps = {}
        for i, p in ipairs(firstN(s.presets, slim and 8 or #s.presets)) do
            if type(p) == "table" then ps[i] = { name = p.name, anchor = p.anchor } end
        end
        s.presets = ps
    end
    s = wireClean(s) or { name = m.name }
    -- still too big: older moves and later spots go first, a little at a time
    local bytes, nodes = wireCost(s)
    local CUTS = {
        function() s.history = firstN(s.history, 6) end,
        function() s.presets = firstN(s.presets, 16) end,
        function() s.history = firstN(s.history, 3) end,
        function() s.presets = firstN(s.presets, 8) end,
        function() s.history = {}; if type(s.bake) == "table" then s.bake.log = nil end end,
        function() s.presets, s.bounds = {}, nil end,
    }
    local cuts = 0
    while (bytes > WIRE_BYTES or nodes > WIRE_NODES) and cuts < #CUTS do
        cuts = cuts + 1
        CUTS[cuts]()
        bytes, nodes = wireCost(s)
    end
    if cuts > 0 and not wireWarned[m.name] then
        wireWarned[m.name] = true
        log("%s: the picture players get of it was cut %d time(s) to fit one message (%d bytes, %d values)",
            m.name, cuts, bytes, nodes)
    end
    return s, bytes, nodes
end

-- ------------------------------------------------------------------ rights
local function allowed(source, name)
    source = tonumber(source) or 0
    if source < 1 then return true end            -- the console and the agent API
    if Open77.acl.isAllowed(source, CFG.allMapsRight or "command.maps") == true then return true end
    return name ~= nil and Open77.acl.isAllowed(source, "command.mlo." .. name) == true
end

local function rightsOf(source)
    local out = {}
    for _, name in ipairs(order) do out[name] = allowed(source, name) end
    return out
end

local function anyRight(source)
    for _, ok in pairs(rightsOf(source)) do if ok then return true end end
    return false
end

-- ------------------------------------------------------------------ messages to players
-- every player's client gets a map's state when it changes -- at most a few times a second per map
-- (a flood of nudges must not become a flood of network messages and log lines)
local BROADCAST_INTERVAL = 0.2
local lastBroadcast, broadcastDue = {}, {}
local function sendState(m)
    lastBroadcast[m.name] = uptime()
    broadcastDue[m.name] = nil
    TriggerClientEvent(EV .. "state", -1, (wireSummary(m)))
end
local function broadcastMap(m)
    if uptime() - (lastBroadcast[m.name] or -1e9) >= BROADCAST_INTERVAL then return sendState(m) end
    if broadcastDue[m.name] then return end
    broadcastDue[m.name] = true
    CreateThread(function()
        Wait(math.floor(BROADCAST_INTERVAL * 1000))
        if broadcastDue[m.name] then sendState(m) end
    end)
end

-- what an agent gets of a map: the summary with the last few moves only (an answer travels as one
-- line of the game's log, which keeps about 4 KiB of a line)
local function agentSummary(m)
    local s = summary(m)
    if type(s.history) == "table" and #s.history > 5 then
        local h = {}
        for i = 1, 5 do h[i] = s.history[i] end
        s.history = h
    end
    if type(s.bake) == "table" and type(s.bake.log) == "string" and #s.bake.log > 400 then
        local b = {}
        for k, v in pairs(s.bake) do b[k] = v end
        b.log = b.log:sub(-400)
        s.bake = b
    end
    return s
end

local function bakerSummary()
    local b = bakerInfo
    if type(b) ~= "table" then return { known = false } end
    return { known = true, version = b.version, startedAt = b.startedAt, hook = b.hook == true,
             wolvenkit = b.wolvenkit ~= nil and b.wolvenkit ~= false, lastAnswer = bakerSeen }
end

local function announce(text)
    log("%s", text)
    Open77.chat.send(-1, { author = "Maps", text = text, color = { 34, 216, 226 } })
end

-- ------------------------------------------------------------------ operations (every entry point uses these)
local ops = {}

local function getMap(name)
    local m = maps[tostring(name or "")]
    if not m then return nil, "unknown_map" end
    return m
end

local function setPending(m, a, who, what)
    local ok, why = validAnchor(a)
    if not ok then return false, why end
    a = normAnchor(a, m.pending or m.running)
    if sameAnchor(a, m.running) then
        m.pending = nil
    else
        m.pending = a
    end
    remember(m, who, what or "move", a)
    persist(m)
    broadcastMap(m)
    -- one log line per burst of moves (the last spot is logged when the burst is over)
    local t = uptime()
    if t - (m.loggedAt or -1e9) >= 3 then
        m.loggedAt, m.logDue = t, nil
        log("%s: new spot %s (%s)", m.name, fmtAnchor(m.pending), tostring(who))
    else
        m.logDue = who
    end
    return true, nil, m.pending or m.running
end

function ops.place(m, args, who)
    return setPending(m, args, who, "place")
end

function ops.move(m, args, who)
    local a = m.pending or m.running
    if not a then return false, "not_placed" end
    local dx, dy = tonumber(args.dx) or 0, tonumber(args.dy) or 0
    if args.frame == "map" then dx, dy = rotate(dx, dy, a.yaw) end
    return setPending(m, { x = a.x + dx, y = a.y + dy, z = a.z + (tonumber(args.dz) or 0),
                           yaw = a.yaw + (tonumber(args.dyaw) or 0) }, who, "move")
end

function ops.here(m, args, who)
    local pid = tonumber(args.playerId)
    if not pid or pid < 1 then return false, "needs_an_in_game_player" end
    local p = Open77.players.get(pid)
    local pos = (p and p.position) or Open77.players.position(pid)
    if not pos then return false, "player_position_unknown" end
    local base = m.pending or m.running or { yaw = 0 }
    local yaw = tonumber(args.yaw) or (args.keepYaw and base.yaw) or (p and (p.heading or p.yaw)) or base.yaw or 0
    return setPending(m, { x = pos.x, y = pos.y, z = pos.z + (tonumber(args.dz) or 0), yaw = yaw }, who, "here")
end

function ops.discard(m, _, who)
    if not m.pending then return true end
    m.pending = nil
    remember(m, who, "discard", nil)
    persist(m)
    broadcastMap(m)
    return true
end

-- back to where the map stood before the last move went live
function ops.revert(m, _, who)
    if not m.previous then return false, "no_previous_spot" end
    return setPending(m, m.previous, who, "back to the previous spot")
end

-- ground height under (x, y), seen by a nearby client: call from a scheduler coroutine
function ops.ground(m, args)
    local a = m.pending or m.running
    local x, y = tonumber(args.x) or (a and a.x), tonumber(args.y) or (a and a.y)
    if not x or not y then return false, "needs_x_and_y" end
    if type(Open77.world) ~= "table" or type(Open77.world.groundZ) ~= "function" then
        return false, "ground_query_unavailable"
    end
    local opts = {}
    if args.playerId then opts.playerId = tonumber(args.playerId) end
    local z, detail = Open77.world.groundZ({ x = x, y = y }, opts)
    if z == nil then return false, tostring(detail) end
    return true, nil, { x = x, y = y, z = z, observer = type(detail) == "table" and detail.playerId or nil }
end

function ops.drop(m, args, who)
    local ok, why, g = ops.ground(m, args)
    if not ok then return false, why end
    local a = m.pending or m.running or { yaw = 0 }
    return setPending(m, { x = g.x, y = g.y, z = g.z + (tonumber(args.dz) or 0), yaw = a.yaw }, who, "ground")
end

function ops.target(m, args)
    local which = args.which or "running"
    local a = (which == "pending" and m.pending) or m.running or m.pending
    if not a then return nil, "not_placed" end
    if args.offset then
        return toWorld({ x = tonumber(args.offset.x) or 0, y = tonumber(args.offset.y) or 0,
                         z = tonumber(args.offset.z) or 0 }, a), a
    end
    if m.def.spawn then return toWorld(m.def.spawn, a), a end
    return { x = a.x, y = a.y, z = a.z + 1.0 }, a
end

function ops.goto_(m, args)
    local pid = tonumber(args.playerId)
    if not pid or pid < 1 then return false, "needs_an_in_game_player" end
    local target, a = ops.target(m, args)
    if not target then return false, a end
    local pending, reason = Open77.players.teleport(pid, target, { heading = a.yaw, dismount = true })
    if not pending then return false, reason end
    return true, nil, target
end

-- presets ---------------------------------------------------------------
local function presetIndex(m, name)
    for i, p in ipairs(m.presets) do
        if p.name:lower() == tostring(name):lower() then return i end
    end
end

function ops.presetSave(m, args, who)
    local name = tostring(args.name or ""):gsub("^%s+", ""):gsub("%s+$", "")
    if name == "" or #name > 48 or name:find("[%c]") then return false, "invalid_preset_name" end
    local a = (args.anchor and validAnchor(args.anchor)) and normAnchor(args.anchor) or m.pending or m.running
    if not a then return false, "not_placed" end
    local i = presetIndex(m, name)
    local entry = { name = name, anchor = a, by = who, at = unix() }
    if i then
        m.presets[i] = entry
    else
        if #m.presets >= PRESETS_MAX then return false, "too_many_presets" end
        m.presets[#m.presets + 1] = entry
    end
    remember(m, who, "preset " .. name, a)
    persist(m)
    broadcastMap(m)
    return true, nil, entry
end

function ops.presetUse(m, args, who)
    local i = presetIndex(m, args.name)
    if not i then return false, "unknown_preset" end
    return setPending(m, m.presets[i].anchor, who, "preset " .. m.presets[i].name)
end

function ops.presetDelete(m, args, who)
    local i = presetIndex(m, args.name)
    if not i then return false, "unknown_preset" end
    local p = table.remove(m.presets, i)
    remember(m, who, "delete preset " .. p.name, nil)
    persist(m)
    broadcastMap(m)
    return true
end

-- apply -------------------------------------------------------------------
local nextId
do
    local counter = 0
    nextId = function(name)
        counter = counter + 1
        return ("%s-%d-%d"):format(name, unix(), counter)
    end
end

local function restartStatus()
    if type(Open77.runtime) ~= "table" or type(Open77.runtime.restartStatus) ~= "function" then return nil end
    return Open77.runtime.restartStatus()
end

local function armRestart(m, seconds, reason)
    if type(Open77.runtime) ~= "table" or type(Open77.runtime.scheduleRestart) ~= "function" then
        return false, "restart_control_unavailable"
    end
    seconds = math.max(5, math.floor(seconds))
    local st = restartStatus()
    if st and st.scheduled and (tonumber(st.secondsRemaining) or 1e9) <= seconds then
        return true, nil, st      -- an earlier restart is already armed: it applies this too
    end
    local r, why = Open77.runtime.scheduleRestart(seconds,
        reason or ("Moving %s to its new spot: rejoin after the restart"):format(m.def.display or m.name))
    if not r then return false, why end
    return true, nil, r
end

local function writeRequest(m, req)
    return writeJson(requestFile(m.name), req)
end

local function requestFor(m, a, action, extra)
    local req = { id = a.id, action = action, map = m.name, resource = m.def.resource or m.name, anchor = a.anchor,
                  requestedBy = a.by, requestedAt = a.at, mode = a.mode }
    for k, v in pairs(extra or {}) do req[k] = v end
    return req
end

function ops.apply(m, args, who)
    local target = m.pending
    if args.anchor then
        local ok, why = validAnchor(args.anchor)
        if not ok then return false, why end
        target = normAnchor(args.anchor)
    end
    if not target then
        if args.force and m.running then target = m.running
        else return false, "nothing_to_apply" end
    end
    if sameAnchor(target, m.running) and not args.force then return false, "already_there" end
    local cur = m.apply
    if cur and (BUSY[cur.state] or cur.state == "unstaging") then return false, "already_baking" end
    if cur and cur.state == "restart_scheduled" then return false, "restart_pending_cancel_first" end
    local mode = args.mode == "empty" and "empty" or "now"
    local countdown = math.floor(tonumber(args.countdown) or CFG.defaultCountdown or 60)
    if countdown < 5 or countdown > 3600 then return false, "invalid_countdown" end
    local a = { id = nextId(m.name), mode = mode, countdown = countdown, by = who, at = unix(), state = "requested",
                anchor = target, message = "waiting for the map baker" }
    local ok, err = writeRequest(m, requestFor(m, a, "bake"))
    if not ok then return false, "cannot_write_request:" .. tostring(err) end
    m.apply = a
    remember(m, who, "apply (" .. mode .. ")", target)
    persist(m, true)
    broadcastMap(m)
    log("%s: bake requested for %s by %s (%s)", m.name, fmtAnchor(target), tostring(who), mode)
    return true, nil, a
end

local function otherRestartNeeded(m)
    for _, name in ipairs(order) do
        local a = maps[name].apply
        if name ~= m.name and a and a.state == "restart_scheduled" then return true end
    end
    return false
end

function ops.cancel(m, _, who)
    local a = m.apply
    if not isOpen(a) then return false, "nothing_to_cancel" end
    if a.state == "baking" then return false, "baking_now_try_again_in_a_moment" end
    if a.state == "unstaging" then return false, "already_cancelling" end
    if a.state == "requested" or a.state == "queued" then
        -- the baker skips a request that is gone (and drops a result whose request vanished)
        if Open77.io.exists(requestFile(m.name)) then Open77.io.remove(requestFile(m.name)) end
        a.state, a.message = "cancelled", "cancelled before the baker started"
    else
        -- built and staged: stop the restart we armed, and have the baker throw the build away
        if a.state == "restart_scheduled" and not otherRestartNeeded(m) and Open77.runtime and Open77.runtime.cancelRestart then
            Open77.runtime.cancelRestart()
            announce(("The restart for %s was cancelled."):format(m.def.display or m.name))
        end
        local ok, err = writeRequest(m, requestFor(m, a, "unstage", { requestedBy = who, requestedAt = unix() }))
        if not ok then return false, "cannot_write_request:" .. tostring(err) end
        a.state, a.message, a.unstageAt = "unstaging", "taking the new build back", unix()
    end
    remember(m, who, "cancel", nil)
    persist(m, true)
    broadcastMap(m)
    return true
end

-- ------------------------------------------------------------------ baker answers
local function onStatus(m, st)
    local a = m.apply
    m.bake = st
    if not a or type(st) ~= "table" or st.id ~= a.id then return end
    local changed = false
    if a.state == "unstaging" then
        if st.state == "unstaged" then
            a.state, a.message = "cancelled", "cancelled: the map stays where it is"
            changed = true
        end
    elseif BUSY[a.state] then
        if st.state == "queued" or st.state == "baking" then
            if a.state ~= st.state then
                a.state = st.state
                a.message = st.message or (st.state == "baking" and "building the new placement" or "queued")
                changed = true
            end
        elseif st.state == "done" then
            a.builtAt = st.finishedAt or unix()
            a.package = st.package
            if a.mode == "now" then
                local ok, why = armRestart(m, a.countdown)
                if ok then
                    a.state, a.restartAt = "restart_scheduled", unix() + a.countdown
                    a.message = ("built: the server restarts in %d s"):format(a.countdown)
                    announce(("%s moves: the server restarts in %d seconds. Rejoin to see it at its new spot.")
                        :format(m.def.display or m.name, a.countdown))
                else
                    a.state = "built"
                    a.message = "built, but the restart could not be scheduled (" .. tostring(why)
                        .. "): it goes live at the next server restart"
                end
            else
                a.state = "waiting_empty"
                a.message = "built: the server restarts once nobody is online"
            end
            changed = true
        elseif st.state == "failed" or st.state == "cancelled" then
            a.state = st.state
            a.message = st.state == "failed" and ("the bake failed: " .. tostring(st.message or "see the baker log"))
                or tostring(st.message or "cancelled")
            changed = true
        end
    end
    if changed then
        bakerSeen = unix()
        persist(m, true)
        broadcastMap(m)
        log("%s: %s (%s)", m.name, tostring(a.state), tostring(a.message))
    end
end

local function pollBaker()
    for _, name in ipairs(order) do
        local m = maps[name]
        local a = m.apply
        if a and (BUSY[a.state] or a.state == "unstaging") then
            local st = readJson(statusFile(name))
            if type(st) == "table" and st.id == a.id then
                onStatus(m, st)
            elseif a.state == "requested" and not a.offline
                and unix() - (a.at or 0) > (CFG.bakerTimeoutSeconds or 45) then
                a.offline = true
                a.message = "no answer from the map baker: is the f2o77-mapbaker service running on the server host?"
                persist(m)
                broadcastMap(m)
            elseif a.state == "unstaging" and not a.offline
                and unix() - (a.unstageAt or 0) > (CFG.bakerTimeoutSeconds or 45) then
                a.offline = true
                a.message = "no answer from the map baker: the new build is thrown away when it is back, "
                    .. "or at the next server start"
                persist(m)
                broadcastMap(m)
            end
        end
    end
end

local function readBakerInfo()
    local b = readJson("bake/baker.json")
    if type(b) ~= "table" then b = nil end
    local before = bakerInfo and (tostring(bakerInfo.startedAt) .. tostring(bakerInfo.hook)) or ""
    local after = b and (tostring(b.startedAt) .. tostring(b.hook)) or ""
    bakerInfo = b
    if before ~= after then TriggerClientEvent(EV .. "baker", -1, bakerSummary()) end
end

-- Without the baker's start hook the staged build must be installed before the server stops: ask
-- the baker to do it in the last seconds of a restart countdown (whoever scheduled the restart).
local function promoteBeforeRestart()
    if bakerInfo and bakerInfo.hook == true then return end
    local st = restartStatus()
    if not (st and st.scheduled) or (tonumber(st.secondsRemaining) or 99) > 8 then return end
    for _, name in ipairs(order) do
        local m = maps[name]
        local a = m.apply
        if a and STAGED[a.state] and not a.promoteRequested then
            a.promoteRequested = true
            writeRequest(m, requestFor(m, a, "bake", { promoteNow = true }))
            persist(m, true)
            log("%s: asked the baker to install the new build before the restart", name)
        end
    end
end

local emptySince = nil

local function pollEmptyRestarts()
    local waiting = nil
    for _, name in ipairs(order) do
        local a = maps[name].apply
        if a and a.state == "waiting_empty" then waiting = maps[name] break end
    end
    if not waiting then emptySince = nil return end
    if #(Open77.players.all() or {}) > 0 then emptySince = nil return end
    emptySince = emptySince or unix()
    if unix() - emptySince < (CFG.emptyGraceSeconds or 30) then return end
    emptySince = nil
    local seconds = (bakerInfo and bakerInfo.hook == true) and 5 or 12
    local ok, why = armRestart(waiting, seconds, "Applying map moves while the server is empty")
    if not ok then return log("could not schedule the restart: %s", tostring(why)) end
    for _, name in ipairs(order) do
        local a = maps[name].apply
        if a and a.state == "waiting_empty" then
            a.state, a.message, a.restartAt = "restart_scheduled", ("restart in %d s (server empty)"):format(seconds),
                unix() + seconds
            persist(maps[name], true)
            broadcastMap(maps[name])
        end
    end
    log("server empty: restarting to apply map moves")
end

-- ------------------------------------------------------------------ loading
local function mapResourceBaked(m)
    -- the map resource knows the anchor (and bake id) of the package it ships: server export `baked`
    local res = m.def.resource or m.name
    if type(Open77.exports) ~= "table" or type(Open77.exports.call) ~= "function" then return nil end
    for attempt = 1, 6 do
        local pending, why = Open77.exports.call(res, "baked")
        if pending then
            local v, err = pending:await()
            if type(v) == "table" then
                if validAnchor(v) then return normAnchor(v), v.id end
                return nil
            end
            if err == nil then return nil end        -- answered nothing: that map is not baked yet
            why = err
        end
        -- the map resource may start after this one: wait for it a little (not for an old map
        -- resource without the export, nor for a broken one)
        local r = tostring(why)
        local later = r:find("unavailable", 1, true) or r:find("preparing", 1, true) or r:find("stopped", 1, true)
        if not later or attempt == 6 then
            log("%s: no `baked` answer from resource %s (%s)", m.name, res, r)
            return nil
        end
        Wait(2000)
    end
end

local function establishRunning(m, saved)
    if saved.running and validAnchor(saved.running) and tonumber(saved.boot) and math.abs(saved.boot - BOOT) <= 2 then
        -- the same server run: this resource was reloaded; keep what we knew (a freshly staged
        -- build is not live before the restart)
        m.running = normAnchor(saved.running)
        m.runningId, m.runningSource, m.boot = saved.runningId, saved.runningSource or "saved", saved.boot
        return
    end
    m.boot = BOOT
    local before = (saved.running and validAnchor(saved.running)) and normAnchor(saved.running) or nil
    local fromRes, resId = mapResourceBaked(m)
    if fromRes then
        m.running, m.runningId, m.runningSource = fromRes, resId, "map resource"
    elseif fileBaked(m.def) then
        m.running, m.runningId, m.runningSource = fileBaked(m.def), m.def.bakeId, "map data"
    elseif before then
        m.running, m.runningId, m.runningSource = before, saved.runningId, "saved"
    end
    local a = m.apply
    if not a or not a.id then return end
    local live = (m.runningId ~= nil and m.runningId == a.id)
        or (m.runningId == nil and STAGED[a.state] and a.anchor ~= nil and sameAnchor(a.anchor, m.running))
    if live and a.state ~= "applied" then
        a.state, a.message, a.appliedAt = "applied", "live since the last restart", unix()
        if before and not sameAnchor(before, m.running) then m.previous = before end
        if m.pending and sameAnchor(m.pending, m.running) then m.pending = nil end
        remember(m, "server", "applied", m.running)
    elseif STAGED[a.state] then
        a.state, a.promoteRequested = "built", nil
        a.message = "the server restarted but the new build was not installed (is the map baker's start hook in "
            .. "place?): it goes live at the next restart"
    end
end

local function loadMaps()
    ensureDir("maps")
    ensureDir("bake/requests")
    ensureDir("bake/status")
    local names = {}
    for name, def in pairs(MAPS) do
        if type(def) == "table" and type(name) == "string" and name:match("^[%w_]+$") then names[#names + 1] = name end
    end
    table.sort(names, function(a, b)
        return tostring(MAPS[a].display or a):lower() < tostring(MAPS[b].display or b):lower()
    end)
    order = names
    for _, name in ipairs(names) do
        local def = MAPS[name]
        local saved = readJson(stateFile(name)) or {}
        local m = { name = name, def = def, presets = saved.presets or {}, history = saved.history or {},
                    pending = (saved.pending and validAnchor(saved.pending)) and normAnchor(saved.pending) or nil,
                    previous = (saved.previous and validAnchor(saved.previous)) and normAnchor(saved.previous) or nil,
                    apply = type(saved.apply) == "table" and saved.apply or nil }
        maps[name] = m
        establishRunning(m, saved)
        if not m.running and def.default and validAnchor(def.default) then
            m.running = normAnchor(def.default)
            m.runningSource = "default"
        end
        if m.pending and sameAnchor(m.pending, m.running) then m.pending = nil end
        m.bake = readJson(statusFile(name))
        if m.apply and m.apply.state == "unstaging" and type(m.bake) == "table" then onStatus(m, m.bake) end
        persist(m, true)
        log("%s: running %s (%s)%s", name, fmtAnchor(m.running), tostring(m.runningSource),
            m.pending and (", new spot " .. fmtAnchor(m.pending)) or "")
    end
end

-- ------------------------------------------------------------------ net events (the in-game manager)
-- What a player's client gets: a short header (the map names, the player's rights, the baker, the
-- settings), then every map in a message of its own (see "what goes over the wire").  The client
-- acknowledges the header; a client that keeps saying hello without ever acknowledging one is not
-- getting the answers (its game drops them): it is told in the log and gets the slim picture.
local function headerFor(source, slim)
    local names = {}
    for _, name in ipairs(order) do if maps[name] then names[#names + 1] = name end end
    return wireClean({ maps = {}, names = names, rights = rightsOf(source), baker = bakerSummary(), config = {
        restartCountdowns = CFG.restartCountdowns, defaultCountdown = CFG.defaultCountdown,
        emptyGraceSeconds = CFG.emptyGraceSeconds }, server = { boot = BOOT }, loading = not loaded, wire = 2,
        slim = slim or nil })
end

local function sendPicture(pid, slim, only)
    pid = tonumber(pid) or 0
    if pid < 1 then return end
    if not only then TriggerClientEvent(EV .. "init", pid, headerFor(pid, slim)) end
    for _, name in ipairs(order) do
        if maps[name] and (not only or only[name]) then
            TriggerClientEvent(EV .. "state", pid, (wireSummary(maps[name], slim)))
        end
    end
end

-- hellos without an acknowledged header, per player (reset by an ack and when they leave)
local unanswered = {}
local function helloFrom(src, p)
    p = type(p) == "table" and p or {}
    local u = unanswered[src] or { n = 0 }
    unanswered[src] = u
    u.n = u.n + 1
    local asked = math.max(u.n, tonumber(p.asked) or 0)
    local slim = p.slim == true or asked >= 3
    if slim and not u.told then
        u.told = true
        log("player %s (#%d) asked %d times without getting the maps (its game drops the answers?): "
            .. "sending the slim picture", tostring(Open77.players.name(src)), src, asked)
    end
    -- a client that got the header but misses some maps names them
    local only = nil
    if type(p.missing) == "table" and next(p.missing) ~= nil and u.acked then
        only = {}
        for _, name in ipairs(p.missing) do if type(name) == "string" then only[name] = true end end
        log("player %s (#%d) misses %d map(s) of its picture: sending them again%s", tostring(Open77.players.name(src)),
            src, #p.missing, slim and " (slim)" or "")
    end
    return slim, only
end

-- a player who asks before the maps are read is told so at once ("loading", never an empty list
-- that looks final) and gets the whole picture once they are
RegisterNetEvent(EV .. "hello", function(p)
    local src = source
    local slim, only = helloFrom(src, p)
    if not loaded then
        waiting[src] = waiting[src] or "hello"
        return sendPicture(src, slim)
    end
    sendPicture(src, slim, only)
end)

RegisterNetEvent(EV .. "ack", function(p)
    local src = source
    local u = unanswered[src]
    if u and u.told then
        log("player %s (#%d) got the picture (%s map(s))", tostring(Open77.players.name(src)), src,
            tostring(type(p) == "table" and p.maps or "?"))
    end
    unanswered[src] = { n = 0, acked = true }
end)

RegisterNetEvent(EV .. "open", function(p)
    local src = source
    local slim = (unanswered[src] and not unanswered[src].acked and unanswered[src].n >= 2) or
        (type(p) == "table" and p.slim == true)
    if not loaded then
        waiting[src] = "open"
        return sendPicture(src, slim)
    end
    if not anyRight(src) then
        return TriggerClientEvent(EV .. "denied", src, { right = CFG.allMapsRight or "command.maps" })
    end
    sendPicture(src, slim)
    TriggerClientEvent(EV .. "open", src, {})
end)

local CLIENT_OPS = {
    place = true, move = true, here = true, discard = true, drop = true, goto_ = true, revert = true,
    presetSave = true, presetUse = true, presetDelete = true, apply = true, cancel = true,
}

-- a client found that its game does not have a map's current package (it started before the
-- server installed it): logged once per player and map, shown in the players list for agents
local staleFiles = {}                  -- player id -> { map -> runningId }
RegisterNetEvent(EV .. "presence", function(p)
    local src = source
    if type(p) ~= "table" or not maps[tostring(p.map or "")] then return end
    local m = maps[p.map]
    staleFiles[src] = staleFiles[src] or {}
    if p.state == "present" then         -- a later look found it there after all
        if staleFiles[src][m.name] then
            staleFiles[src][m.name] = nil
            log("%s: player %s (#%d) sees the map at its spot after all", m.name, tostring(Open77.players.name(src)), src)
        end
        return
    end
    if staleFiles[src][m.name] ~= (m.runningId or true) then
        staleFiles[src][m.name] = m.runningId or true
        log("%s: player %s (#%d) does not see the map at its current spot (game started before the server "
            .. "installed it: a restart from the launcher fixes it)", m.name, tostring(Open77.players.name(src)), src)
    end
end)

AddEventHandler("playerDropped", function()
    staleFiles[source] = nil
    unanswered[source] = nil
end)

RegisterNetEvent(EV .. "op", function(req)
    local src = source
    if type(req) ~= "table" then return end
    local m = maps[tostring(req.map or "")]
    local name, args = tostring(req.op or ""), type(req.args) == "table" and req.args or {}
    local token = req.token
    local function done(ok, reason, result)
        TriggerClientEvent(EV .. "result", src, { token = token, map = req.map, op = name, ok = ok and true or false,
                                                  reason = reason, result = result })
    end
    if not m then return done(false, "unknown_map") end
    if not CLIENT_OPS[name] then return done(false, "unknown_op") end
    -- reading where a map is (going there) is open to anyone who may open the manager
    if name == "goto_" then
        if not anyRight(src) then return done(false, "not_allowed") end
    elseif not allowed(src, m.name) then
        return done(false, "not_allowed")
    end
    local who = ("%s (#%d)"):format(tostring(Open77.players.name(src) or "player"), src)
    if name == "here" or name == "goto_" or name == "drop" then args.playerId = src end
    if name == "drop" then
        CreateThread(function() done(ops.drop(m, args, who)) end)
        return
    end
    done(ops[name](m, args, who))
end)

-- the road lanes around a spot, for a client's overlay (the editor while a map is moved, an agent's
-- view): read from data/roads in a thread (tiles are parsed a slice at a time), sent in parts small
-- enough for one net event each.  A player asks at most once a second.
local roadAsked = {}
RegisterNetEvent(EV .. "roads", function(p)
    local src = source
    if type(p) ~= "table" or not anyRight(src) then return end
    local t = uptime()
    if t - (roadAsked[src] or -1e9) < 0.8 then return end
    roadAsked[src] = t
    local x, y, r = tonumber(p.x), tonumber(p.y), tonumber(p.r) or 200
    local key = tostring(p.key or ""):sub(1, 64)
    if not x or not y then return end
    if not (F2O77_ROADS and F2O77_ROADS.available()) then
        return TriggerClientEvent(EV .. "agent:roads", src, { key = key, missing = true })
    end
    CreateThread(function()
        local lanes = F2O77_ROADS.near(x, y, math.min(600, r), tostring(p.kinds or ""), 700)
        local parts, cur, size = {}, {}, 0
        for _, lane in ipairs(lanes) do
            local text = F2O77_ROADS.encode(lane)
            if size + #text > 30000 and #cur > 0 then
                parts[#parts + 1] = cur
                cur, size = {}, 0
            end
            cur[#cur + 1], size = text, size + #text + 3
        end
        if #cur > 0 or #parts == 0 then parts[#parts + 1] = cur end
        for i, part in ipairs(parts) do
            TriggerClientEvent(EV .. "agent:roads", src, { key = key, part = i, parts = #parts, lanes = part })
            Wait(0)
        end
    end)
end)

AddEventHandler("playerDropped", function()
    roadAsked[source] = nil
end)

-- ------------------------------------------------------------------ console / chat command
local USAGE = {
    "maps list | open | baker | version | log [n] | players | tp <x> <y> <z> [heading]",
    "maps status <map>                     maps state [map]",
    "maps set <map> <x> <y> <z> [yaw]      maps here <map> [yaw]",
    "maps move <map> <dx> <dy> [dz]        maps nudge <map> <forward> <right> [up]",
    "maps rotate <map> <degrees>           maps up|down <map> <metres>      maps ground <map>",
    "maps goto <map> [running|pending]     maps discard <map>               maps revert <map>",
    "maps preset <map> list | save <name> | use <name> | delete <name>",
    "maps apply <map> [now [seconds] | empty]     maps cancel <map>",
    "in game, for you: maps view <map> top|oblique|gate|off   maps wire <map> on|off   maps roads <map> on|off",
    "                  maps fit <map>   maps probe x,y [x,y ...]   maps shot <http://127.0.0.1:port/...>   maps say <text>",
    "                  maps scan [x y z] [h=200] [cols=40] [rows=25]   (heights seen from above, terrain too)",
    "an agent prefixes a request id: maps @<id> <command...> (the answer goes to its game's log as @agent JSON)",
}

-- Where an answer goes.  `to` is a player id (chat), 0 (the server console) or an agent's request
-- { pid, id }: its answer goes to that player's client as one event, which prints it into the game's
-- log as one `@agent {json}` line -- the map agent on that player's computer reads it there.  An
-- agent gets the data behind the lines too.  Several chat lines sent in one tick arrive in reverse
-- order: one line per tick.
local function reply(to, lines, data, ok)
    if type(lines) ~= "table" then lines = { lines } end
    if type(to) == "table" then
        print(("[%s] @%s %s"):format(RES, tostring(to.id), tostring(lines[1] or "")))
        if (tonumber(to.pid) or 0) >= 1 then
            -- one message: the data behind the lines only while it fits (the lines are what the agent reads)
            local msg = wireClean({ id = to.id, ok = ok ~= false, lines = lines, data = data }) or { id = to.id }
            local bytes, nodes = wireCost(msg)
            if bytes > 2 * WIRE_BYTES or nodes > 2 * WIRE_NODES then
                msg.data, msg.truncated = nil, true
                bytes, nodes = wireCost(msg)
                while (bytes > 2 * WIRE_BYTES or nodes > 2 * WIRE_NODES) and type(msg.lines) == "table" and #msg.lines > 1 do
                    msg.lines = firstN(msg.lines, math.floor(#msg.lines / 2))
                    bytes, nodes = wireCost(msg)
                end
            end
            TriggerClientEvent(EV .. "agent:reply", to.pid, msg)
        end
        return
    end
    local source = tonumber(to) or 0
    for _, text in ipairs(lines) do print(("[%s] %s"):format(RES, text)) end
    if source < 1 then return end
    CreateThread(function()
        for _, text in ipairs(lines) do
            Open77.chat.send(source, { author = "Maps", text = text, color = { 34, 216, 226 } })
            Wait(0)
        end
    end)
end

-- the forward declarations the commands need (defined with the agent API below)
local statusAll, playersList

local function fmtMap(m)
    local s = ("%s (%s): running %s"):format(m.def.display or m.name, m.name, fmtAnchor(m.running))
    if m.pending then s = s .. ", NEW SPOT " .. fmtAnchor(m.pending) end
    if isOpen(m.apply) or (m.apply and m.apply.state == "failed") then
        s = s .. (" [%s: %s]"):format(m.apply.state, tostring(m.apply.message or ""))
    end
    return s
end

-- a loopback address on the player's own computer (the map agent listens there): where a screenshot
-- may be sent from a `maps shot` request -- nowhere else, so this command can never post a player's
-- screen to some other host
local function loopbackUrl(url)
    url = tostring(url or "")
    if #url > 300 or url:find("[%s\"'<>\\]") then return nil end
    local host, port = url:match("^http://([%w%.%-]+):(%d+)/")
    if not host or not (host == "127.0.0.1" or host == "localhost" or host == "[::1]") then return nil end
    local p = tonumber(port)
    if not p or p < 1024 or p > 65535 then return nil end
    return url
end

-- what a client of this player does for an agent (its camera, wireframe, roads, ground check):
-- the client answers in its own game's log
local function toClient(to, op, map, args)
    local pid = type(to) == "table" and to.pid or to
    TriggerClientEvent(EV .. "agent:do", pid, { id = type(to) == "table" and to.id or nil, op = op, map = map,
                                                 args = args or {} })
end

local function command(source, args)
    -- an agent's request: `maps @<id> <command...>` (its answer goes to its game's log)
    local to = source
    if type(args[1]) == "string" and args[1]:sub(1, 1) == "@" then
        local id = args[1]:sub(2)
        if id == "" or #id > 40 or not id:match("^[%w_%-%.]+$") then
            return reply(source, "maps: an agent's request id is letters, digits, - _ . (40 at most)")
        end
        to = { pid = tonumber(source) or 0, id = id }
        table.remove(args, 1)
    end
    local pid = tonumber(source) or 0
    local sub = (args[1] or "list"):lower()
    local who = pid > 0 and ("%s (#%d)"):format(tostring(Open77.players.name(source)), source) or "console"
    if type(to) == "table" then who = who .. " [agent]" end
    local function needPlayer()
        if pid < 1 then reply(to, sub .. " needs an in-game player", nil, false) return true end
        return false
    end
    local function needRight()
        if pid >= 1 and not anyRight(source) then
            reply(to, "you need " .. (CFG.allMapsRight or "command.maps"), nil, false)
            return true
        end
        return false
    end
    if sub == "help" then return reply(to, USAGE) end
    if sub == "list" then
        if #order == 0 then return reply(to, loaded and "no maps installed" or "the maps are still being read", { maps = {} }) end
        local lines, names = {}, {}
        for _, name in ipairs(order) do lines[#lines + 1] = fmtMap(maps[name]); names[#names + 1] = name end
        return reply(to, lines, { maps = names })
    end
    if sub == "version" then
        local ok, rinfo = false, nil
        if F2O77_ROADS then ok, rinfo = F2O77_ROADS.available() end
        return reply(to, ("f2o77_maps %s: %d map(s)%s, road data %s"):format(VERSION, #order,
            loaded and "" or " (still reading them)", ok and "installed" or "missing"),
            { version = VERSION, boot = BOOT, loaded = loaded, maps = #order, roads = ok and rinfo or false,
              baker = bakerSummary() })
    end
    if sub == "open" then
        if needPlayer() or needRight() then return end
        sendPicture(source)
        TriggerClientEvent(EV .. "open", source, {})
        if type(to) == "table" then reply(to, "panel opened") end
        return
    end
    if sub == "baker" then
        local b = bakerSummary()
        local lines = { b.known and ("baker %s, started %s, start hook %s"):format(tostring(b.version),
            tostring(b.startedAt), b.hook and "installed" or "MISSING") or "the map baker has not reported yet" }
        for _, name in ipairs(order) do
            local st = maps[name].bake
            lines[#lines + 1] = ("%s: %s"):format(name, type(st) == "table"
                and (tostring(st.state) .. " " .. tostring(st.message or "")) or "no answer from the baker yet")
        end
        return reply(to, lines, b)
    end
    if sub == "log" then
        if needRight() then return end
        local n = math.max(1, math.min(80, tonumber(args[2]) or 30))
        local entries = recentLog(n)
        if type(to) == "table" then return reply(to, ("%d line(s)"):format(#entries), { log = entries }) end
        local lines = {}
        for i = math.max(1, #entries - 11), #entries do lines[#lines + 1] = entries[i].text end
        return reply(to, lines)
    end
    if sub == "players" then
        if needRight() then return end
        local list = playersList()
        local lines = {}
        for _, p in ipairs(list) do
            local pos = p.position
            lines[#lines + 1] = ("#%d %s%s"):format(p.playerId, tostring(p.name),
                pos and (" at %.1f %.1f %.1f"):format(pos.x, pos.y, pos.z) or "")
        end
        return reply(to, #lines > 0 and lines or "nobody online", { players = list })
    end
    if sub == "state" then
        if needRight() then return end
        if args[2] then
            local m = maps[args[2]]
            if not m then return reply(to, tostring(args[2]) .. ": unknown_map", nil, false) end
            return reply(to, fmtMap(m), { map = agentSummary(m) })
        end
        local all = statusAll()
        all.maps = {}
        for _, name in ipairs(order) do all.maps[#all.maps + 1] = agentSummary(maps[name]) end
        return reply(to, ("%d map(s)"):format(#order), all)
    end
    if sub == "say" then
        if needPlayer() or needRight() then return end
        local text = table.concat(args, " ", 2):sub(1, 200)
        if text == "" then return reply(to, "say what?", nil, false) end
        local by = type(to) == "table" and "Claude" or tostring(Open77.players.name(source) or "Maps")
        for _, p in ipairs(Open77.players.all() or {}) do
            if anyRight(p) then TriggerClientEvent(EV .. "agent:say", p, { text = text, by = by }) end
        end
        return reply(to, "said")
    end
    if sub == "shot" then
        if needPlayer() or needRight() then return end
        local url = loopbackUrl(args[2])
        if not url then
            return reply(to, "shot needs an http://127.0.0.1:<port>/... address on your own computer", nil, false)
        end
        if type(Open77.players.requestScreenshot) ~= "function" then
            return reply(to, "screenshots are not available on this server", nil, false)
        end
        local quality = math.max(30, math.min(95, tonumber(args[3]) or 85))
        local scale = math.max(0.25, math.min(1.0, tonumber(args[4]) or 1.0))
        local overlay = (args[5] or "overlay") ~= "nooverlay"
        local pending, why = Open77.players.requestScreenshot(pid, {
            url = url, format = "jpeg", quality = quality, scale = scale, includeOverlay = overlay, settleFrames = 2,
            encoding = "multipart", field = "file",
            filename = (type(to) == "table" and to.id or "shot") .. ".jpg",
            timeoutMs = 20000, timeout = 30000,
        })
        if not pending then return reply(to, "screenshot refused: " .. tostring(why), nil, false) end
        CreateThread(function()
            local receipt, err = pending:await()
            if not receipt then return reply(to, "screenshot failed: " .. tostring(err), nil, false) end
            reply(to, ("screenshot: HTTP %s, %sx%s, %s bytes"):format(tostring(receipt.status), tostring(receipt.width),
                tostring(receipt.height), tostring(receipt.sizeBytes)), receipt,
                (tonumber(receipt.status) or 0) >= 200 and (tonumber(receipt.status) or 0) < 300)
        end)
        return
    end
    if sub == "probe" then
        if needPlayer() or needRight() then return end
        local points = {}
        for i = 2, math.min(#args, 33) do
            local x, y, z = tostring(args[i]):match("^(%-?[%d%.]+),(%-?[%d%.]+),?(%-?[%d%.]*)$")
            if x then points[#points + 1] = { tonumber(x), tonumber(y), tonumber(z) } end
        end
        if #points == 0 then return reply(to, "probe x,y[,z] ...", nil, false) end
        return toClient(to, "probe", nil, { points = points })
    end
    if sub == "tp" then              -- an agent moves the player it works for (the world streams around the body)
        if needPlayer() or needRight() then return end
        local x, y, z = tonumber(args[2]), tonumber(args[3]), tonumber(args[4])
        if not (x and y and z) or math.abs(x) > 20000 or math.abs(y) > 20000 or math.abs(z) > 5000 then
            return reply(to, "tp x y z [heading]", nil, false)
        end
        local pending, why = Open77.players.teleport(pid, { x = x, y = y, z = z },
            { heading = tonumber(args[5]), dismount = true })
        if not pending then return reply(to, "teleport refused: " .. tostring(why), nil, false) end
        return reply(to, ("going to %.1f %.1f %.1f"):format(x, y, z), { x = x, y = y, z = z })
    end
    if sub == "scan" then            -- a height map of what this player's game sees from above (terrain too)
        if needPlayer() or needRight() then return end
        local opts, first = {}, 2
        local x, y, z = tonumber(args[2]), tonumber(args[3]), tonumber(args[4])
        if x and y and z then
            if math.abs(x) > 20000 or math.abs(y) > 20000 or math.abs(z) > 5000 then
                return reply(to, "scan [x y z] [h=200] [cols=40] [rows=25] [settle=1.5] [keep]", nil, false)
            end
            opts.x, opts.y, opts.z, first = x, y, z, 5
        end
        for i = first, #args do
            local k, v = tostring(args[i]):match("^(%w+)=(.+)$")
            if k then opts[k] = tonumber(v) or v
            elseif args[i] == "keep" then opts.keep = true end
        end
        return toClient(to, "scan", nil, opts)
    end
    if sub == "ray" then             -- one screen ray through the agent's camera: what the engine says
        if needPlayer() or needRight() then return end
        local opts = { u = tonumber(args[2]), v = tonumber(args[3]) }
        for i = 2, #args do
            local k, v = tostring(args[i]):match("^(%w+)=(.+)$")
            if k then opts[k] = tonumber(v) or v end
        end
        return toClient(to, "ray", nil, opts)
    end
    if sub == "client" then          -- what this player's client has (page, maps, camera, roads)
        if needPlayer() then return end
        return toClient(to, "state", args[2], {})
    end
    local m, why = getMap(args[2])
    if not m then return reply(to, ("%s: %s (maps list)"):format(tostring(args[2]), why), nil, false) end
    if not allowed(source, m.name) then
        return reply(to, "you need command.mlo." .. m.name .. " or " .. (CFG.allMapsRight or "command.maps"), nil, false)
    end
    local n = function(i) return tonumber(args[i]) end
    -- the parts that happen in this player's game
    if sub == "view" then
        if needPlayer() then return end
        local mode = (args[3] or "top"):lower()
        local opts = { mode = mode, which = nil }
        for i = 4, #args do
            local k, v = tostring(args[i]):match("^(%w+)=(.+)$")
            if k then opts[k] = tonumber(v) or v
            elseif args[i] == "running" or args[i] == "pending" then opts.which = args[i]
            elseif mode == "gate" and tonumber(args[i]) then opts.gate = tonumber(args[i]) end
        end
        return toClient(to, "view", m.name, opts)
    end
    if sub == "wire" or sub == "roads" then
        if needPlayer() then return end
        local on = (args[3] or "on"):lower() ~= "off"
        local opts = { on = on }
        for i = 4, #args do
            local k, v = tostring(args[i]):match("^(%w+)=(.+)$")
            if k then opts[k] = tonumber(v) or v
            elseif args[i] == "running" or args[i] == "pending" then opts.which = args[i] end
        end
        return toClient(to, sub, m.name, opts)
    end
    if sub == "fit" then
        if needPlayer() then return end
        return toClient(to, "fit", m.name, { which = args[3] })
    end
    local ok, reason
    if sub == "status" then return reply(to, fmtMap(m), { map = agentSummary(m) })
    elseif sub == "set" or sub == "place" then ok, reason = ops.place(m, { x = n(3), y = n(4), z = n(5), yaw = n(6) }, who)
    elseif sub == "here" then ok, reason = ops.here(m, { playerId = source, yaw = n(3) }, who)
    elseif sub == "move" then ok, reason = ops.move(m, { dx = n(3), dy = n(4), dz = n(5) }, who)
    elseif sub == "nudge" then ok, reason = ops.move(m, { dx = n(4) or 0, dy = n(3) or 0, dz = n(5), frame = "map" }, who)
    elseif sub == "rotate" then ok, reason = ops.move(m, { dyaw = n(3) or 90 }, who)
    elseif sub == "up" or sub == "down" then ok, reason = ops.move(m, { dz = (n(3) or 1) * (sub == "down" and -1 or 1) }, who)
    elseif sub == "ground" then
        CreateThread(function()
            local good, w = ops.drop(m, { playerId = pid > 0 and source or nil }, who)
            reply(to, (good and "ok: " or ("failed (" .. tostring(w) .. "): ")) .. fmtMap(m), { map = agentSummary(m) }, good)
        end)
        return
    elseif sub == "goto" then ok, reason = ops.goto_(m, { playerId = source, which = args[3] })
    elseif sub == "discard" or sub == "reset" then ok, reason = ops.discard(m, {}, who)
    elseif sub == "revert" or sub == "previous" then ok, reason = ops.revert(m, {}, who)
    elseif sub == "preset" then
        local act, pname = (args[3] or "list"):lower(), table.concat(args, " ", 4)
        if act == "list" then
            if #m.presets == 0 then return reply(to, m.name .. ": no saved positions", { presets = {} }) end
            local lines = {}
            for _, p in ipairs(m.presets) do lines[#lines + 1] = ("  %s: %s"):format(p.name, fmtAnchor(p.anchor)) end
            return reply(to, lines, { presets = m.presets })
        elseif act == "save" then ok, reason = ops.presetSave(m, { name = pname }, who)
        elseif act == "use" then ok, reason = ops.presetUse(m, { name = pname }, who)
        elseif act == "delete" then ok, reason = ops.presetDelete(m, { name = pname }, who)
        else return reply(to, USAGE) end
    elseif sub == "apply" then
        local mode = (args[3] or "now"):lower()
        ok, reason = ops.apply(m, { mode = mode == "empty" and "empty" or "now", countdown = n(4) }, who)
    elseif sub == "cancel" then ok, reason = ops.cancel(m, {}, who)
    else return reply(to, USAGE) end
    reply(to, (ok and "ok: " or ("failed (" .. tostring(reason) .. "): ")) .. fmtMap(m), { map = agentSummary(m), reason = reason }, ok)
end

RegisterCommand("maps", command, false)

-- the commands of the old per-map placer: mlo.<map> <sub> ... == maps <sub> <map> ...
local function legacyAliases()
    for _, name in ipairs(order) do
        RegisterCommand("mlo." .. name, function(source, args)
            local sub = (args[1] or "status"):lower()
            if sub == "edit" then return command(source, { "open" }) end
            local rest = { sub, name }
            for i = 2, #args do rest[#rest + 1] = args[i] end
            command(source, rest)
        end, false)
    end
end

-- ------------------------------------------------------------------ agent HTTP API
-- GET  /f2o77_maps/status                       every map, the baker, the restart (read only, no token)
-- GET  /f2o77_maps/maps/<map>                   one map (no token)
-- GET  /f2o77_maps/baker                        the baker and its last answer per map (no token)
-- GET  /f2o77_maps/players                      connected players and where they are
-- POST /f2o77_maps/maps/<map>/<op>              op: place move here discard revert ground drop goto apply
--                                               cancel presetSave presetUse presetDelete
-- POST /f2o77_maps/screenshot {playerId,url,options?}
-- Authorization: Bearer <data/agent_token.txt of this resource>.
local token = nil

local function loadToken()
    local f = LoadResourceFile(RES, "agent_token.txt")
    if type(f) == "string" then
        f = f:gsub("%s+", "")
        if #f >= 16 then return f, "data/agent_token.txt" end
    end
    local saved = Open77.kvp.get("agent_token")
    local fresh = (type(saved) == "string" and #saved >= 16) and saved or nil
    if not fresh then
        local chars, t = "abcdefghijklmnopqrstuvwxyz0123456789", {}
        math.randomseed(unix() + math.floor(uptime() * 1000))
        for i = 1, 32 do
            local k = math.random(1, #chars)
            t[i] = chars:sub(k, k)
        end
        fresh = table.concat(t)
        Open77.kvp.set("agent_token", fresh)
    end
    -- the operator (and an agent on the host) reads it from the file; never printed
    Open77.io.write("agent_token.txt", fresh .. "\n")
    return fresh, "generated"
end

local function authorized(req)
    local h = req.headers and (req.headers["authorization"] or req.headers["Authorization"]) or ""
    return token ~= nil and h == ("Bearer " .. token)
end

function playersList()
    local out = {}
    for _, pid in ipairs(Open77.players.all() or {}) do
        local old = {}
        for name in pairs(staleFiles[pid] or {}) do old[#old + 1] = name end
        out[#out + 1] = { playerId = pid, name = Open77.players.name(pid), position = Open77.players.position(pid),
                          rights = rightsOf(pid), staleMaps = old }
    end
    return out
end

local function bakerAll()
    local st = {}
    for _, name in ipairs(order) do st[name] = maps[name].bake end
    return { baker = bakerSummary(), info = bakerInfo, status = st }
end

function statusAll()
    local list = {}
    for _, name in ipairs(order) do list[#list + 1] = summary(maps[name]) end
    return { resource = RES, maps = list, boot = BOOT, restart = restartStatus(), baker = bakerSummary() }
end

local HTTP_OPS = {
    place = true, move = true, here = true, discard = true, revert = true, ground = true, drop = true,
    ["goto"] = true, apply = true, cancel = true, presetSave = true, presetUse = true, presetDelete = true,
}

local function http(req, res)
    local path = (req.path or ""):gsub("^/" .. RES, ""):gsub("/+$", "")
    if path == "" then path = "/status" end
    if req.method == "GET" and path == "/status" then return res.json(200, statusAll()) end
    if req.method == "GET" and path == "/baker" then return res.json(200, bakerAll()) end
    local mapName = path:match("^/maps/([%w_]+)$")
    if req.method == "GET" and mapName then
        local m = maps[mapName]
        if not m then return res.json(404, { error = "unknown_map" }) end
        return res.json(200, summary(m))
    end
    if not authorized(req) then return res.json(401, { error = "unauthorized" }) end
    if req.method == "GET" and path == "/players" then return res.json(200, { players = playersList() }) end
    if req.method ~= "POST" then return res.json(405, { error = "method_not_allowed" }) end
    local body = {}
    if req.body and req.body ~= "" then
        local ok, decoded = pcall(json.decode, req.body)
        if not ok or type(decoded) ~= "table" then return res.json(400, { error = "invalid_json" }) end
        body = decoded
    end
    if path == "/screenshot" then
        if type(Open77.players.requestScreenshot) ~= "function" then
            return res.json(501, { error = "screenshots_unavailable" })
        end
        local opts = type(body.options) == "table" and body.options or {}
        opts.url = body.url
        local pending, why = Open77.players.requestScreenshot(tonumber(body.playerId), opts)
        if not pending then return res.json(400, { error = why }) end
        CreateThread(function()
            local receipt, err = pending:await()
            log("screenshot for the agent: %s", receipt and ("HTTP " .. tostring(receipt.status)) or tostring(err))
        end)
        return res.json(202, { requested = true })
    end
    local name, op = path:match("^/maps/([%w_]+)/([%w_]+)$")
    if not name then return res.json(404, { error = "unknown_route" }) end
    local m = maps[name]
    if not m then return res.json(404, { error = "unknown_map" }) end
    if not HTTP_OPS[op] then return res.json(404, { error = "unknown_op" }) end
    local function answer(ok, reason, result)
        res.json(ok and 200 or 409, { ok = ok and true or false, reason = reason, result = result, map = summary(m) })
    end
    local who = "agent"
    if op == "ground" or op == "drop" then
        CreateThread(function()
            if op == "ground" then answer(ops.ground(m, body)) else answer(ops.drop(m, body, who)) end
        end)
        return
    end
    if op == "goto" then return answer(ops.goto_(m, body)) end
    return answer(ops[op](m, body, who))
end

-- ------------------------------------------------------------------ start
-- Starting happens once per server VM: on onResourceStart, and -- should that event not reach this
-- VM (a reload) -- a moment later anyway.  The maps are read first and players are answered as
-- soon as they are; the rest (aliases, the agent API) cannot hold that up, and the steps of the
-- loop below are each guarded so a failing one never stops the others.
local started = false
local function step(name, fn)
    local ok, err = pcall(fn)
    if not ok then log("%s failed: %s", name, tostring(err)) end
    return ok
end

local function startUp()
    if started then return end
    started = true
    CreateThread(function()
        Wait(1500)                   -- let the map resources start (their `baked` export)
        step("reading data/config.json", loadConfig)
        step("reading the baker's info", readBakerInfo)
        loadMaps()                   -- may Wait (the map resources' `baked` export): not under pcall
        -- players who said hello before the maps were loaded get the full picture now (and the
        -- panel, when they asked for it)
        loaded = true
        for _, pid in ipairs(Open77.players.all() or {}) do
            sendPicture(pid)
            if waiting[pid] == "open" and anyRight(pid) then TriggerClientEvent(EV .. "open", pid, {}) end
        end
        waiting = {}
        step("the old mlo.<map> commands", legacyAliases)
        step("the agent token", function() token = select(1, loadToken()) end)
        step("the agent API", function()
            if type(Open77.http) == "table" and type(Open77.http.listen) == "function" then
                local route, why = Open77.http.listen("/", http)
                if route then log("agent API on %s", tostring(route))
                else log("agent API off (%s); the `maps` command still works (agents: maps @<id> ...)", tostring(why)) end
            end
        end)
        local roadsOk = F2O77_ROADS and F2O77_ROADS.available()
        log("%s: %d map(s) managed; baker %s; road data %s; open the manager in game with %s (rebindable)", VERSION,
            #order, bakerInfo and ("v" .. tostring(bakerInfo.version) .. (bakerInfo.hook and " with start hook" or " WITHOUT start hook"))
                or "not reported yet", roadsOk and "installed" or "missing", tostring(CFG.menuKey or "F9"))
        local tick = 0
        while true do
            Wait(1000)
            tick = tick + 1
            step("promote before restart", promoteBeforeRestart)
            if tick % 2 == 0 then
                step("baker poll", pollBaker)
                step("empty-server restarts", pollEmptyRestarts)
                step("saving state", flush)
            end
            step("move log", function()
                for _, name in ipairs(order) do
                    local m = maps[name]
                    if m.logDue and uptime() - (m.loggedAt or 0) >= 3 then
                        log("%s: new spot %s (%s)", m.name, fmtAnchor(m.pending), tostring(m.logDue))
                        m.loggedAt, m.logDue = uptime(), nil
                    end
                end
            end)
            if tick % 10 == 0 then step("baker info", readBakerInfo) end
        end
    end)
    -- a start that died before the maps were read (a broken map definition, a data file) must not
    -- leave players waiting for ever: they are answered with what there is
    CreateThread(function()
        Wait(25000)
        if not loaded then
            log("the maps were not read within 25 s: answering players with what there is (see the errors above)")
            loaded = true
            for _, pid in ipairs(Open77.players.all() or {}) do sendPicture(pid) end
            waiting = {}
        end
    end)
end

AddEventHandler("onResourceStart", function(resource)
    if resource == RES then startUp() end
end)

log("%s loading (server VM generation %s)", VERSION, tostring(GetCurrentResourceGeneration and GetCurrentResourceGeneration() or "?"))
CreateThread(function()
    Wait(3000)
    if not started then
        log("no start event reached this VM: starting anyway")
        startUp()
    end
end)

AddEventHandler("onResourceStop", function(resource)
    if resource == RES then flush() end
end)

-- exposed for other server resources (the Studio's status, scripts)
exports("status", function() return statusAll() end)
