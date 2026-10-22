-- Runs f2o77_maps' client side (shared/config.lua, shared/maps/*.lua, client/main.lua) against a
-- fake Open77 client: key mappings (rebindable, hold), the WebUI page, debugDraw with the engine's
-- limits, camera, character, ground ray, HUD; frames driven by a virtual clock.  Every call into the
-- resource (a frame of its loop, a key, a page or server event) is metered: Open77 stops a Lua call
-- that runs past its execution budget, so none may cost more than CALL_BUDGET VM instructions.
-- usage: lua5.4 maps_manager_client_harness.lua <f2o77_maps dir>
local DIR = assert(arg[1], "f2o77_maps dir")

local LOG = {}
local realprint = print
local function fail(msg)
    io.stderr:write("FAIL: " .. msg .. "\n--- log\n" .. table.concat(LOG, "\n") .. "\n")
    os.exit(1)
end
local function check(cond, msg) if not cond then fail(msg) end end
local function near(a, b, eps) return math.abs(a - b) <= (eps or 1e-6) end
local function deepcopy(v)
    if type(v) ~= "table" then return v end
    local t = {}
    for k, x in pairs(v) do t[k] = deepcopy(x) end
    return t
end

-- ------------------------------------------------------------------ metering
local CALL_BUDGET = 60000             -- VM instructions per call (the old whole-map draw took ~300k)
QUOTA_LINES = tonumber(os.getenv("QUOTA_LINES") or "") or 30   -- what another resource leaves us, in lines
local cost = { max = 0, where = "-", calls = 0 }
-- STOP.at: the host stops the next metered call after that many instructions (its frame budget
-- ran out), at whatever instruction that is -- as Open77 does
STOP = { at = nil, fired = 0 }
local function meter(where, co)
    local n = 0
    cost.cur = 0
    local hook = function()
        n = n + 100; cost.cur = n
        if STOP.at and n >= STOP.at then
            STOP.at = nil
            STOP.fired = STOP.fired + 1
            error("Open77 script execution budget exceeded")
        end
    end
    if co then debug.sethook(co, hook, "", 100) else debug.sethook(hook, "", 100) end
    return function()
        if co then debug.sethook(co) else debug.sethook() end
        cost.cur = 0
        cost.calls = cost.calls + 1
        if n > cost.max then cost.max, cost.where = n, where end
        return n
    end
end
local function metered(where, fn, ...)
    local stop = meter(where)
    local r = table.pack(pcall(fn, ...))
    stop()
    if not r[1] then error(r[2], 0) end
    return table.unpack(r, 2, r.n)
end

-- ------------------------------------------------------------------ fake client
local T = { ms = 5000, frame = 0 }
local mappings, mapOrder = {}, {}
local rebinds = {}
local page = nil
local pageSends = {}
local focus = { keyboard = false, cursor = false }
local serverEvents = {}
local net = {}
local handlers = {}
local threads = {}
local draws = { sets = 0, tags = {}, clears = 0, quota = nil, refused = 0 }
local notifies = {}
local camForward = { x = 0.0, y = 1.0, z = 0.0 }
local player = { x = 100.0, y = 200.0, z = 30.0, yaw = 0.0 }
local captured = false

function print(...)
    local t = {}
    for i = 1, select("#", ...) do t[#t + 1] = tostring((select(i, ...))) end
    LOG[#LOG + 1] = table.concat(t, " ")
end
function GetCurrentResourceName() return "f2o77_maps" end
function GetGameTimer() return T.ms end
function CreateThread(fn) threads[#threads + 1] = { co = coroutine.create(fn), wake = 0 } end
function Wait(ms) return coroutine.yield(ms or 0) end
function RegisterNetEvent(name, fn) net[name] = fn end
function AddEventHandler(name, fn) handlers[name] = handlers[name] or {}; table.insert(handlers[name], fn) end
function TriggerServerEvent(name, payload) serverEvents[#serverEvents + 1] = { name = name, payload = deepcopy(payload) } end

local KEYS_OK = {}
for _, k in ipairs({ "UP", "DOWN", "LEFT", "RIGHT", "PAGEUP", "PAGEDOWN", "HOME", "END", "SHIFT", "CTRL", "INSERT",
                     "DELETE", "ENTER", "BACKSPACE", "F9", "K", "F8" }) do KEYS_OK[k] = true end

function RegisterKeyMapping(id, name, key, onPressed, onReleased)
    check(type(id) == "string" and #id <= 64 and id:match("^[%w_.:-]+$"), "mapping id " .. tostring(id))
    check(type(name) == "string" and #name <= 96, "mapping name")
    check(KEYS_OK[key], "default key in the vocabulary: " .. tostring(key))
    if not mappings[id] then mapOrder[#mapOrder + 1] = id end
    mappings[id] = { id = id, name = name, key = rebinds[id] or key, defaultKey = key, onPressed = onPressed,
                     onReleased = onReleased, hold = onReleased ~= nil }
    return true, mappings[id].key
end

local function press(id)
    local m = mappings[id]
    check(m, "no mapping " .. id)
    if captured or focus.keyboard then return end      -- mappings never fire while a page owns the keyboard
    if m.onPressed then metered("key " .. id, m.onPressed, id) end
end
local function release(id)
    local m = mappings[id]
    if captured or focus.keyboard then return end
    if m.hold and m.onReleased then metered("key up " .. id, m.onReleased, id) end
end

local function fire(name, ...) for _, fn in ipairs(handlers[name] or {}) do metered("event " .. name, fn, ...) end end

Open77 = {
    input = {
        isCaptured = function() return captured end,
        keyFor = function(id) return mappings[id] and mappings[id].key end,
        mappings = function()
            local out = {}
            for _, id in ipairs(mapOrder) do
                local m = mappings[id]
                out[#out + 1] = { resource = "f2o77_maps", id = id, name = m.name, key = m.key, defaultKey = m.defaultKey,
                                  hold = m.hold, rebound = m.key ~= m.defaultKey }
            end
            out[#out + 1] = { resource = "other_res", id = "x", name = "Other", key = "X", defaultKey = "X" }
            return out
        end,
        rebind = function(res, id, key)
            if not KEYS_OK[key] then return false, "invalid_key" end
            rebinds[id] = key
            mappings[id].key = key
            fire("open77:keybinds:changed")
            return true, key
        end,
        reset = function(res, id)
            rebinds[id] = nil
            mappings[id].key = mappings[id].defaultKey
            fire("open77:keybinds:changed")
            return true, mappings[id].key
        end,
    },
    webui = {
        create = function(opts)
            check(opts.entry == "web/index.html" and opts.transparent == true and opts.visible == true, "page options")
            page = { handlers = {} }
            function page:on(name, fn) self.handlers[name] = fn end
            function page:send(name, payload)
                if self.refuse and self.refuse(name, payload) then return false, "invalid_webui_event_payload" end
                pageSends[#pageSends + 1] = { name = name, payload = deepcopy(payload) }
                return true
            end
            function page:setFocus(k, c) focus.keyboard, focus.cursor = k, c end
            return page
        end,
    },
    debugDraw = {
        set = function(tag, g)
            -- the engine's own work is not the resource's: no metering while the fake checks the batch
            local hk, hm, hc = debug.gethook()
            debug.sethook()
            local r1, r2 = draws.setImpl(tag, g)
            debug.sethook(hk, hm, hc)
            return r1, r2
        end,
        setImpl = function(tag, g)
            check(#tag >= 1 and #tag <= 96, "tag length")
            check(#g.lines <= 512, "at most 512 lines per batch, got " .. #g.lines)
            check(g.ttl >= 0.05 and g.ttl <= 10, "ttl range")
            check(g.maxDistance >= 1 and g.maxDistance <= 5000 and g.thickness >= 0.5 and g.thickness <= 8, "options")
            for _, ln in ipairs(g.lines) do
                for _, p in ipairs({ ln.a, ln.b }) do
                    check(type(p) == "table" and type(p.x) == "number" and type(p.y) == "number" and type(p.z) == "number",
                          "a whole point (invalid_debug_point in the game)")
                    check(p.x == p.x and math.abs(p.x) < 1e6 and math.abs(p.y) < 1e6 and math.abs(p.z) < 1e6, "finite point")
                end
                check(type(ln.color) == "string" and ln.color:match("^#%x%x%x%x%x%x%x?%x?$"), "color " .. tostring(ln.color))
            end
            if draws.quota then              -- another resource holds most of the budget: `quota` lines are left
                local have = 0
                for t, n in pairs(draws.tags) do if t ~= tag then have = have + n end end
                if have + #g.lines > draws.quota then
                    draws.refused = draws.refused + 1
                    return nil, "debug_geometry_quota"
                end
            end
            if draws.explodeOnce then
                draws.explodeOnce = false
                error("engine hiccup")
            end
            if draws.budgetOnce then         -- the host's frame budget ran out (other resources had it)
                draws.budgetOnce = false
                error("Open77 script execution budget exceeded")
            end
            draws.tags[tag] = #g.lines
            draws.thick = draws.thick or {}
            draws.thick[tag] = g.thickness
            draws.last = draws.last or {}
            draws.last[tag] = g.lines
            draws.sets = draws.sets + 1
            local tags, verts = 0, 0
            for _, n in pairs(draws.tags) do tags, verts = tags + 1, verts + 2 * n end
            check(tags <= 32, "at most 32 tags per resource")
            check(verts <= 8192, "at most 8192 vertices, got " .. verts)
            return true
        end,
        clear = function(tag)
            draws.clears = draws.clears + 1
            if tag then draws.tags[tag] = nil else draws.tags = {} end
            return true
        end,
    },
    camera = {
        view = function() return { position = { x = player.x, y = player.y, z = player.z + 1.7 }, forward = camForward, fov = 70 } end,
        aimRay = function(dist, opts) return { hit = true, position = { x = 150.0, y = 260.0, z = 31.0 }, distance = 50 } end,
    },
    character = {
        position = function() return player.x, player.y, player.z end,
        yaw = function() return player.yaw end,
    },
    world = { groundZ = function(x, y, fromZ)
        draws.rays = (draws.rays or 0) + 1
        if draws.terrainFn then return draws.terrainFn(x, y, fromZ) end
        if draws.terrain then return draws.terrain end
        return 12.25
    end },
    hud = { notify = function(text, opts) notifies[#notifies + 1] = text return true end },
    -- real time inside a frame: 20 ns per VM instruction of the call under way
    time = { monotonic = function() return T.ms / 1000 + (cost.cur or 0) * 2e-8 end },
    session = {
        launcherContext = function() return LCTX end,
        relaunchViaLauncher = function(id, ep) RELAUNCHED = { id = id, ep = ep } return true end,
        quitGame = function() QUIT = true end,
    },
}
LCTX = { launched = false, serverId = "", connectEndpoint = "", modsDigest = "" }

-- JSON (the agent's answers are printed as JSON lines)
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
-- bounded like the game's: the representation events use (48 KiB); more answers nil, reason
Open77.json = { encode = function(v)
    local text = enc(v)
    if #text > 48 * 1024 then return nil, "value_not_json_serializable" end
    return text
end }
Open77.resource = { version = function() return "1.3.0" end }

-- scripted cameras: one view at a time, within 250 m of the player
CAMS = { list = {}, active = nil, n = 0, activations = 0 }
Open77.camera.create = function(o)
    check(type(o) == "table" and o.position and o.lookAt, "camera options")
    check(o.fov == nil or (o.fov >= 5 and o.fov <= 170), "fov range")
    CAMS.n = CAMS.n + 1
    CAMS.list[CAMS.n] = { position = deepcopy(o.position), lookAt = deepcopy(o.lookAt), fov = o.fov }
    return CAMS.n
end
Open77.camera.activate = function(id, opts)
    local c = CAMS.list[id]
    if not c then return false, "camera_not_found" end
    local d = math.sqrt((c.position.x - player.x) ^ 2 + (c.position.y - player.y) ^ 2 + (c.position.z - player.z) ^ 2)
    if d > 250 then return false, "invalid_argument" end
    CAMS.active = id
    CAMS.activations = CAMS.activations + 1
    return true
end
Open77.camera.deactivate = function() CAMS.active = nil return true end
Open77.camera.destroy = function(id) CAMS.list[id] = nil if CAMS.active == id then CAMS.active = nil end return true end
-- screen rays from the active camera: a 200 x 125 m patch around what it looks at, the ground at
-- z = 100 + x / 100; the left tenth of the screen sees the sky (a miss)
SCRAY = { calls = 0 }
Open77.camera.screenRaycast = function(u, v, dist, opts)
    SCRAY.calls = SCRAY.calls + 1
    check(type(u) == "number" and u >= 0 and u <= 1 and type(v) == "number" and v >= 0 and v <= 1, "normalized screen point")
    check(type(dist) == "number" and dist >= 0.1 and dist <= 1000, "screen ray distance in 0.1..1000")
    check(type(opts) == "table" and opts.static == true and opts.dynamic == false, "the world's static parts only")
    local c = CAMS.active and CAMS.list[CAMS.active]
    if not c then return nil, "camera_unavailable" end
    if SCRAY.failFrom and v >= SCRAY.failFrom then return nil, "camera_projection_mismatch" end
    if u < 0.1 then return { hit = false, position = { x = 0, y = 0, z = 0 } } end
    local x, y = c.lookAt.x + (u - 0.5) * 200, c.lookAt.y + (0.5 - v) * 125
    return { hit = true, position = { x = x, y = y, z = 100 + x / 100 }, normal = { x = 0, y = 0, z = 1 },
             material = "terrain", distance = 200 }
end
Open77.camera.screenRay = function(u, v, dist)
    local c = CAMS.active and CAMS.list[CAMS.active]
    if not c then return nil, "camera_unavailable" end
    return { origin = deepcopy(c.position), direction = { x = 0, y = 0, z = -1 }, position = deepcopy(c.lookAt), maxDistance = dist }
end

draws.setImpl = Open77.debugDraw.setImpl
Open77.debugDraw.setImpl = nil

-- ------------------------------------------------------------------ load
dofile(DIR .. "/shared/config.lua")
local p = io.popen("ls '" .. DIR .. "/shared/maps'")
for fn in p:lines() do if fn:match("%.lua$") then dofile(DIR .. "/shared/maps/" .. fn) end end
p:close()
dofile(DIR .. "/client/main.lua")
DEFS_HAS_FOOT = F2O77_MAPS.cedar_crest and (F2O77_MAPS.cedar_crest.ghost or ""):find(" 4\n") ~= nil

local function frames(n, dtms)
    for _ = 1, n do
        T.ms = T.ms + (dtms or 16)
        T.frame = T.frame + 1
        for _, th in ipairs(threads) do
            if coroutine.status(th.co) ~= "dead" and th.wake <= T.ms then
                local stop = meter("frame " .. T.frame, th.co)
                local ok, ms = coroutine.resume(th.co)
                stop()
                if not ok then fail("client thread: " .. tostring(ms)) end
                th.wake = T.ms + (tonumber(ms) or 0)
            end
        end
        for _, th in ipairs(threads) do check(coroutine.status(th.co) ~= "dead", "the client's loop is still running") end
    end
end
local function framesUntil(cond, max)
    for _ = 1, max or 120 do
        if cond() then return true end
        frames(1)
    end
    return cond()
end
local function lastSend(name)
    for i = #pageSends, 1, -1 do if pageSends[i].name == name then return pageSends[i].payload end end
end
local function lastServer(name)
    for i = #serverEvents, 1, -1 do if serverEvents[i].name == name then return serverEvents[i].payload or true end end
end
local function fromServer(name, payload) metered("net " .. name, net["f2o77_maps:" .. name], deepcopy(payload)) end
local function pageEmits(name, payload) metered("page " .. name, page.handlers[name], deepcopy(payload)) end
local function totalLines() local n = 0 for _, c in pairs(draws.tags) do n = n + c end return n end
-- the colours of the lines on screen now (live tags only)
local function liveColors()
    local seen = {}
    for tag in pairs(draws.tags) do
        for _, ln in ipairs(draws.last[tag] or {}) do seen[ln.color] = (seen[ln.color] or 0) + 1 end
    end
    return seen
end
local DETAIL_COLORS = { "#19D5E0FF", "#FFB000B3", "#FFFFFF59", "#B0B0B0E6", "#FF7AE0FF" }   -- buildings, walls, box, now, pending

frames(2)
check(page, "the page is created")
check(lastServer("f2o77_maps:hello"), "hello sent to the server")
local function countServer(name)
    local n = 0
    for _, e in ipairs(serverEvents) do if e.name == name then n = n + 1 end end
    return n
end
local h0 = countServer("f2o77_maps:hello")
frames(200)                                         -- 3.2 s without an answer
check(countServer("f2o77_maps:hello") == h0 + 1, "no answer: hello asked again 3 s later, got " .. countServer("f2o77_maps:hello"))
local function logHas(pat) for _, l in ipairs(LOG) do if l:find(pat) then return l end end end
check(logHas("client 1.3.0 starting") and logHas("panel page created"), "the client tells its story in the game's log")
-- the page's own ready messages came before this script listened (a reload, a cached page): the
-- client knocks until the page answers
local pings = 0
for _, sent in ipairs(pageSends) do if sent.name == "maps:ping" then pings = pings + 1 end end
check(pings >= 1, "the client knocks on a page that has not answered")
frames(70)
local pings2 = 0
for _, sent in ipairs(pageSends) do if sent.name == "maps:ping" then pings2 = pings2 + 1 end end
check(pings2 > pings and pings2 <= pings + 3, "about once a second: " .. pings2)

-- every key is a rebindable mapping
local want = { "open_manager", "forward", "back", "left", "right", "raise", "lower", "rotate_left", "rotate_right",
               "fast", "fine", "aim", "ground", "save", "cancel" }
check(#mapOrder == #want, "15 mappings, got " .. #mapOrder)
for _, id in ipairs(want) do
    check(mappings[id] and mappings[id].name:match("^Maps: "), "mapping " .. id)
end
check(mappings.open_manager.key == "F9" and mappings.forward.key == "UP" and mappings.save.key == "ENTER", "default keys")
for _, id in ipairs({ "forward", "back", "left", "right", "raise", "lower", "rotate_left", "rotate_right", "fast", "fine" }) do
    check(mappings[id].hold, id .. " is a hold mapping")
end
check(not mappings.save.hold and not mappings.open_manager.hold, "taps")

-- the server's picture, the page ready
local MAPS = {
    { name = "cedar_crest", display = "Cedar Crest Estate", running = { x = 480.0, y = 1800.0, z = 174.0, yaw = 0.0 },
      presets = {}, history = {} },
    { name = "harbor_dock", display = "Harbor Dock", running = nil, presets = {}, history = {} },
}
fromServer("init", { maps = MAPS, rights = { cedar_crest = true, harbor_dock = false }, config = { defaultCountdown = 60 },
                     baker = { known = true, hook = true, version = "1.0.0" } })
check(page.handlers["ui:ready"] and page.handlers["maps:ready"], "waits for the page's ready events")
pageEmits("maps:ready", { maps = 0 })        -- the page answers a knock
local init = lastSend("maps:init")
check(init and #init.maps == 2 and init.maps[1].name == "cedar_crest", "page gets the maps")
local knocks = #pageSends
frames(70)
local more = 0
for i = knocks + 1, #pageSends do if pageSends[i].name == "maps:ping" then more = more + 1 end end
check(more == 0, "no more knocking once the page answered")
pageEmits("ui:ready", {})
check(lastSend("maps:init") and #lastSend("maps:init").maps == 2, "the runtime's ready event also sends the maps")
check(init.menuKey == "F9" and #init.keys == 15, "and this resource's keys only")
check(init.keys[1].id == "open_manager", "the manager's own key first")
check(init.baker.known == true and init.baker.hook == true, "and the baker")

-- a panel opened while this client knows no maps (their message came before it listened) asks again
local saved = deepcopy(MAPS)
fromServer("init", { maps = {}, rights = {}, config = {}, baker = { known = true } })
local hellos = 0
for _, e in ipairs(serverEvents) do if e.name == "f2o77_maps:hello" then hellos = hellos + 1 end end
fromServer("open", {})
local hellos2 = 0
for _, e in ipairs(serverEvents) do if e.name == "f2o77_maps:hello" then hellos2 = hellos2 + 1 end end
check(hellos2 == hellos + 1, "an empty panel asks the server for the maps again")
pageEmits("maps:ready", { again = true })
check(#serverEvents > 0, "and so does the page when it opens empty")
fromServer("init", { maps = saved, rights = { cedar_crest = true, harbor_dock = false }, config = { defaultCountdown = 60 },
                     baker = { known = true, hook = true, version = "1.0.0" } })
check(#lastSend("maps:init").maps == 2 and lastSend("maps:open"), "the maps arrive in the open panel")
pageEmits("maps:close", {})

-- the server's picture as a short header and then one message per map (one big message with every
-- map and all its history was dropped by the game once the moves piled up: "not responding")
do
    local acks0 = countServer("f2o77_maps:ack")
    local head = { maps = {}, names = { "cedar_crest", "harbor_dock" }, rights = { cedar_crest = true, harbor_dock = false },
                   config = { defaultCountdown = 60 }, baker = { known = true, hook = true, version = "1.0.0" }, wire = 2 }
    fromServer("init", head)
    check(countServer("f2o77_maps:ack") == acks0 + 1, "the header is acknowledged (the server learns the answer arrived)")
    fromServer("state", saved[1])
    fromServer("state", saved[2])
    local st = lastSend("maps:state")
    check(st and st.name == "harbor_dock", "each map goes to the page as it arrives")
    -- a map named in the header whose own message never comes is asked for again, a few times
    local h1 = countServer("f2o77_maps:hello")
    local head2 = deepcopy(head)
    head2.names[3] = "lost_map"
    fromServer("init", head2)
    fromServer("state", saved[1])
    fromServer("state", saved[2])
    frames(400)                                    -- 6.4 s
    local ask = lastServer("f2o77_maps:hello")
    check(countServer("f2o77_maps:hello") == h1 + 1 and type(ask) == "table" and type(ask.missing) == "table"
          and ask.missing[1] == "lost_map" and #ask.missing == 1, "the missing map is asked for, and only that one")
    check(logHas("did not arrive"), "and the game's log says so")
    frames(1200)                                   -- 19 s: three asks at most
    check(countServer("f2o77_maps:hello") == h1 + 3, "at most three times: " .. (countServer("f2o77_maps:hello") - h1))
    -- the page bridge refuses the maps in one message: header, keys, then one map at a time
    page.refuse = function(name, payload) return name == "maps:init" and #(payload.maps or {}) > 0 end
    local sends0 = #pageSends
    fromServer("init", head)
    fromServer("state", saved[1])
    fromServer("state", saved[2])
    local inits, states = 0, 0
    for i = sends0 + 1, #pageSends do
        if pageSends[i].name == "maps:init" then inits = inits + 1 end
        if pageSends[i].name == "maps:state" then states = states + 1 end
    end
    check(inits >= 1 and states >= 2, ("one map at a time when the bridge refuses them all at once (%d, %d)"):format(inits, states))
    page.refuse = nil
    fromServer("init", { maps = saved, rights = { cedar_crest = true, harbor_dock = false }, config = { defaultCountdown = 60 },
                         baker = { known = true, hook = true, version = "1.0.0" } })
end

-- open the panel with the key; the server answers
press("open_manager")
check(lastServer("f2o77_maps:open"), "the key asks the server")
fromServer("open", {})
check(focus.keyboard and focus.cursor and lastSend("maps:open"), "panel open with keyboard and cursor")
press("open_manager")
check(focus.keyboard, "mapped keys do not fire while the panel owns the keyboard")

-- an op from the page goes to the server and its answer back to the page
pageEmits("maps:op", { map = "cedar_crest", op = "place", args = { x = 1, y = 2, z = 3 }, request = 7 })
local opreq = lastServer("f2o77_maps:op")
check(opreq.map == "cedar_crest" and opreq.op == "place" and opreq.args.x == 1 and opreq.token, "op forwarded")
fromServer("result", { token = opreq.token, map = "cedar_crest", op = "place", ok = true, result = { x = 1, y = 2, z = 3, yaw = 0 } })
local res = lastSend("maps:result")
check(res and res.request == 7 and res.ok == true, "result back to the page with its request id")

-- state updates are relayed
fromServer("state", { name = "cedar_crest", display = "Cedar Crest Estate", running = MAPS[1].running,
                      pending = { x = 490.0, y = 1800.0, z = 174.0, yaw = 0.0 }, presets = {}, history = {} })
check(lastSend("maps:state").pending.x == 490.0, "state relayed")

-- preview: the wireframe at the new spot while the panel is open
pageEmits("maps:preview", { map = "cedar_crest", on = true })
check(totalLines() == 0, "nothing is drawn inside the page's call itself")
frames(2)
check(totalLines() > 0, "something is on screen within two frames (the box while the ghost is read)")
check(framesUntil(function() return totalLines() > 30 end, 60), "wireframe drawn for the preview over the next frames: " .. totalLines())
pageEmits("maps:preview", { map = "cedar_crest", on = false })
check(totalLines() == 0, "preview off clears it")

-- ------------------------------------------------------------------ the in-world editor
pageEmits("maps:edit", { map = "cedar_crest" })
check(not focus.keyboard and lastSend("maps:close"), "editing closes the panel and frees the keyboard")
local ed = lastSend("editor:state")
check(ed.active and ed.map == "cedar_crest" and near(ed.anchor.x, 490.0), "editor starts at the new spot")
check(ed.keys.save == "ENTER" and ed.keys.move == "UP/DOWN/LEFT/RIGHT", "editor shows the effective keys")
framesUntil(function() return lastSend("editor:state").wire == "full" end, 60)
frames(12)
local n = 0
for tag, c in pairs(draws.tags) do n = n + 1; check(tag:match("^f2o77_maps_%d+$"), "tag name") end
check(n >= 1 and totalLines() > 30 and totalLines() <= 3600 + 40, "wireframe within the line budget")
check(lastSend("editor:state").wire == "full", "editor state: the wireframe is drawn")

-- hold forward for one second (camera faces +y): about moveSpeed metres
press("forward")
frames(62, 16)
release("forward")
frames(5)
ed = lastSend("editor:state")
check(near(ed.anchor.y, 1800 + 4.0, 0.35) and near(ed.anchor.x, 490.0, 0.01), "forward moved ~4 m along the view: " .. ed.anchor.y)
local y0 = ed.anchor.y
press("fast"); press("right")
frames(31, 16)
release("right"); release("fast")
frames(3)
ed = lastSend("editor:state")
check(near(ed.anchor.x, 490.0 + 12.0, 1.2) and near(ed.anchor.y, y0, 0.01), "fast x6 to the right: " .. ed.anchor.x)
press("rotate_left")
frames(31, 16)
release("rotate_left")
frames(3)
ed = lastSend("editor:state")
check(near(ed.anchor.yaw, 17.5, 1.5), "rotate ~35 deg/s: " .. ed.anchor.yaw)
press("raise")
frames(31, 16)
release("raise")
frames(3)
check(lastSend("editor:state").anchor.z > 174.5, "raise")
press("ground")
frames(8)
check(near(lastSend("editor:state").anchor.z, 12.25), "ground: drops to the static ground")
press("aim")
frames(8)
ed = lastSend("editor:state")
check(ed.aim and near(ed.anchor.x, 150.0) and near(ed.anchor.y, 260.0), "aim: follows the crosshair")
press("aim")
captured = true                                  -- typing in chat: the editor must not move
local before = deepcopy(lastSend("editor:state").anchor)
press("forward")
frames(20)
captured = false
release("forward")
check(near(lastSend("editor:state").anchor.y, before.y), "no movement while another page owns the keyboard")

-- save: the spot goes to the server; on success the panel reopens on that map
press("save")
local saveReq = lastServer("f2o77_maps:op")
check(saveReq.op == "place" and near(saveReq.args.x, 150.0), "save sends the spot")
fromServer("result", { token = saveReq.token, map = "cedar_crest", op = "place", ok = true,
                       result = { x = 150.0, y = 260.0, z = 31.0, yaw = 17.5 } })
check(not lastSend("editor:state").active, "editor closed after the save")
check(focus.keyboard and lastSend("maps:select").map == "cedar_crest", "panel back on the map")
check(totalLines() == 0, "wireframe cleared")

-- cancel from the editor; the menu key also leaves the editor
pageEmits("maps:edit", { map = "cedar_crest" })
press("cancel")
check(not lastSend("editor:state").active and focus.keyboard, "cancel: back to the panel")
pageEmits("maps:close", {})
check(not focus.keyboard, "the page's close")

-- a map the player may not move
pageEmits("maps:edit", { map = "harbor_dock" })
check(not lastSend("editor:state").active and notifies[#notifies]:find("may not move"), "no editor without the right")

-- Escape (pause key) closes the panel
fromServer("open", {})
fire("open77:pauseKey")
check(not focus.keyboard and lastSend("maps:close"), "pause key closes the panel")

-- ------------------------------------------------------------------ keys
fromServer("open", {})
pageEmits("maps:keys:rebind", { id = "open_manager", key = "F8" })
local keys = lastSend("maps:keys")
check(mappings.open_manager.key == "F8" and keys.menuKey == "F8", "rebind from the page")
pageEmits("maps:keys:rebind", { id = "save", key = "NOT_A_KEY" })
check(lastSend("maps:keys").error == "invalid_key", "a refused key is reported")
pageEmits("maps:keys:reset", { id = "open_manager" })
check(mappings.open_manager.key == "F9" and lastSend("maps:keys").menuKey == "F9", "reset to the default")
pageEmits("maps:close", {})

-- ------------------------------------------------------------------ debug geometry quota: less detail, never nothing
-- (debugDraw's 8192 vertices are shared by every resource; another one may hold most of them)
local function noDetail()
    local seen = liveColors()
    for _, c in ipairs(DETAIL_COLORS) do if seen[c] then return false end end
    return true
end
draws.quota = QUOTA_LINES
pageEmits("maps:edit", { map = "cedar_crest" })
frames(15)
check(lastSend("editor:state").active and totalLines() > 0 and totalLines() <= QUOTA_LINES, "quota: less detail, still drawn: " .. totalLines())
check(lastSend("editor:state").wire == "reduced", "and the editor says so")
check(noDetail(), "only the outline (or box), gates and cross")
check(notifies[#notifies]:find("Another resource"), "the player is told once why")
local told = #notifies
press("forward")
frames(20)
release("forward")
frames(10)
check(totalLines() > 0 and totalLines() <= QUOTA_LINES and noDetail() and #notifies == told,
      "moving under the quota: still drawn with less detail, no new message: " .. totalLines())
-- tighter still: 17 lines, what the box and the cross take (and the outline, gates and cross, when they fit)
local GK = {}
for line in (F2O77_MAPS.cedar_crest.ghost or ""):gmatch("[^\n]+") do
    local k = tonumber(line:match("(%-?%d+)%s*$"))
    if k then GK[k] = (GK[k] or 0) + 1 end
end
local LEVEL1 = ((GK[4] or 0) > 0 and GK[4] or 12) + (GK[5] or 0) + 5
draws.quota = 17
press("back")
frames(5)
release("back")
check(framesUntil(function() return totalLines() > 0 and liveColors()["#FFE14DFF"] end, 60) and totalLines() <= 17 and noDetail(),
      "a tighter quota: still the outline (or the box) and the cross: " .. totalLines())
if LEVEL1 > 17 then
    check(liveColors()["#FFE14DFF"] == 12 and totalLines() == 17, "the box and the cross: " .. totalLines())
end
-- no room at all: nothing drawn, the editor says why, and it does not retry every frame
draws.quota = 4
press("back")
frames(5)
release("back")
framesUntil(function() return lastSend("editor:state").wire == "off" end, 60)
check(lastSend("editor:state").wire == "off" and lastSend("editor:state").wireWhy == "debug_geometry_quota" and totalLines() == 0,
      "no room: the wireframe is off and the editor says why")
local refusedBefore = draws.refused
frames(120)                                       -- two seconds standing still
check(draws.refused - refusedBefore <= 2, "no retry every frame: " .. (draws.refused - refusedBefore))
-- the other resource is done: half a minute later full detail is back
draws.quota = nil
frames(35, 1000)
check(framesUntil(function() return lastSend("editor:state").wire == "full" end, 120) and totalLines() > QUOTA_LINES and not noDetail(),
      "full detail again after a while: " .. tostring(lastSend("editor:state").wire) .. " " .. totalLines())
press("cancel")
check(focus.keyboard, "cancel reopens the panel")
fromServer("toast", { text = "in the panel" })
check(lastSend("maps:toast").text == "in the panel", "toasts go to the panel while it is open")
pageEmits("maps:close", {})

-- baker info relayed
fromServer("baker", { known = true, hook = false })
check(lastSend("maps:baker").hook == false, "baker info relayed")

-- server messages
fromServer("denied", { right = "command.maps" })
check(notifies[#notifies]:find("command.maps"), "denied explained")
fromServer("toast", { text = "hello" })
check(notifies[#notifies] == "hello", "toast on the HUD while the panel is closed")
fromServer("result", { token = 9999, map = "cedar_crest", op = "apply", ok = false, reason = "already_baking" })
check(notifies[#notifies]:find("already_baking"), "a failed op is shown when the panel is closed")

-- ------------------------------------------------------------------ a failing frame does not stop the editor
fromServer("open", {})
pageEmits("maps:edit", { map = "cedar_crest" })
draws.explodeOnce = true
press("forward")
frames(10)
release("forward")
check(framesUntil(function() return lastSend("editor:state").wire == "full" and totalLines() > 30 end, 120) and
      lastSend("editor:state").active, "the loop survives an error and draws again")
check(lastSend("editor:state").outline == (DEFS_HAS_FOOT and "outline" or "box"), "the HUD knows what stands for the ground")
press("cancel")
pageEmits("maps:close", {})

-- ------------------------------------------------------------------ the ground check (fit)
if DEFS_HAS_FOOT then
    -- the player is far from the spot: the world there may not be streamed, so nothing is checked
    fromServer("open", {})
    pageEmits("maps:edit", { map = "cedar_crest" })
    draws.rays = 0
    frames(40)
    local e = lastSend("editor:state")
    check(draws.rays == 0 and e.fit and e.fit.far and e.fit.far > 200, "too far from the player: no rays, and the HUD says so")
    press("cancel")
    pageEmits("maps:close", {})
    -- close by: 2 m of air under the map's ground; points over where the map is now are left out
    player.x, player.y, player.z = 470.0, 1790.0, 174.0
    fromServer("open", {})
    pageEmits("maps:edit", { map = "cedar_crest" })
    draws.terrain = 174.0 - 2.0                  -- the world's ground 2 m under the map's
    draws.rays = 0
    framesUntil(function() local f = lastSend("editor:state").fit return f and not f.far and f.samples > 0 end, 200)
    e = lastSend("editor:state")
    check(draws.rays > 0 and draws.rays <= 40, "a few rays, once the map stands still: " .. draws.rays)
    check(e.fit and e.fit.float >= 1.5 and e.fit.buried == 0 and not e.fit.far, "it floats about 2 m")
    check(e.fit.skipped > 0 and e.fit.skipped < e.fit.samples and draws.rays == e.fit.samples - e.fit.skipped,
          ("points over where the map is now are left out: %d of %d"):format(e.fit.skipped, e.fit.samples))
    if GK[5] == 4 then                           -- the test estate: its one gate is clear of where the map is now
        check(e.fit.gateFloat >= 1.5, "and the gate's threshold floats too")
    end
    check(framesUntil(function() return (liveColors()["#FF3B30FF"] or 0) > 0 end, 120), "red lines where it floats")
    -- buried: the world 1.5 m above (a new spot, a new check)
    press("raise")
    frames(3)
    release("raise")
    draws.terrain = 174.0 + 1.5
    frames(5)
    framesUntil(function() local f = lastSend("editor:state").fit return f and f.buried > 0 end, 200)
    e = lastSend("editor:state")
    check(e.fit and e.fit.buried >= 1.0 and e.fit.float == 0, "buried about 1.5 m: " .. tostring(e.fit and e.fit.buried))
    draws.terrain = nil
    press("cancel")
    pageEmits("maps:close", {})
    -- at the spot where the map is now there is nothing to check (the rays would find its own floors)
    fromServer("state", { name = "cedar_crest", display = "Cedar Crest Estate", running = MAPS[1].running, presets = {}, history = {} })
    fromServer("open", {})
    pageEmits("maps:edit", { map = "cedar_crest" })
    draws.rays = 0
    frames(40)
    e = lastSend("editor:state")
    check(draws.rays == 0 and e.fit and e.fit.here, "where it is now: no rays, and the HUD says so")
    press("cancel")
    pageEmits("maps:close", {})
    player.x, player.y, player.z = 100.0, 200.0, 30.0
end

-- ------------------------------------------------------------------ the host's frame budget runs out
fromServer("open", {})
pageEmits("maps:edit", { map = "cedar_crest" })
frames(20)
draws.budgetOnce = true
press("forward")
frames(20)
release("forward")
check(framesUntil(function() return lastSend("editor:state").wire == "full" and totalLines() > 30 end, 200),
      "after a budget refusal the wireframe comes back")
local refusals = 0
for _, l in ipairs(LOG) do if l:find("budget exceeded", 1, true) then refusals = refusals + 1 end end
check(refusals == 1, "the refusal is reported once: " .. refusals)
press("cancel")
pageEmits("maps:close", {})

-- ------------------------------------------------------------------ is the map really there? (old files)
if DEFS_HAS_FOOT then
    -- the map runs at 480 1800 174 (its ground at 174), yaw 0: its outline in world metres
    local edges = {}
    for line in (F2O77_MAPS.cedar_crest.ghost):gmatch("[^\n]+") do
        local v = {}
        for w in line:gmatch("%S+") do v[#v + 1] = tonumber(w) end
        if #v == 7 and v[7] == 4 then edges[#edges + 1] = { v[1] + 480, v[2] + 1800, v[4] + 480, v[5] + 1800 } end
    end
    local function nearEdge(x, y, r)
        for _, e in ipairs(edges) do
            local dx, dy = e[3] - e[1], e[4] - e[2]
            local L2 = dx * dx + dy * dy
            local t = L2 > 0 and math.max(0, math.min(1, ((x - e[1]) * dx + (y - e[2]) * dy) / L2)) or 0
            if (e[1] + dx * t - x) ^ 2 + (e[2] + dy * t - y) ^ 2 <= r * r then return true end
        end
        return false
    end
    local function presenceEvents(from)
        local out = {}
        for i = from + 1, #serverEvents do
            if serverEvents[i].name == "f2o77_maps:presence" then out[#out + 1] = serverEvents[i].payload end
        end
        return out
    end
    player.x, player.y, player.z = 470.0, 1790.0, 174.0
    -- the map is there, with hedges and low walls along its outline (1.2 m): rays that start just above
    -- the outline land on them and once called the map missing; from above its roofs it is plainly there
    draws.terrainFn = function(x, y, fromZ)
        if nearEdge(x, y, 2.0) then return math.min(fromZ, 175.2) end
        return math.min(fromZ, 174.0)
    end
    local before, said = #serverEvents, #notifies
    frames(60, 1000)
    check(#presenceEvents(before) == 0 and #notifies == said, "a map that is there, hedges and all: nothing to say")
    -- the old files: the world's ground (4 m lower) is all the rays find
    fromServer("state", { name = "cedar_crest", display = "Cedar Crest Estate", running = { x = 480.0, y = 1800.02, z = 174.0, yaw = 0.0 },
                          presets = {}, history = {} })
    draws.terrainFn = function(x, y, fromZ) return 170.0 end
    before = #serverEvents
    frames(40, 1000)
    local reported = presenceEvents(before)
    check(#reported == 1 and reported[1].map == "cedar_crest" and reported[1].state == "missing",
          "a game with the map's old files notices and tells the server (once)")
    check(notifies[#notifies]:find("not at its new spot"), "and the player")
    check(lastSend("maps:stale") and lastSend("maps:stale").map == "cedar_crest" and lastSend("maps:stale").relaunch == false,
          "the panel offers the fix (manual: not started by the launcher)")
    pageEmits("maps:relaunch", {})
    check(not RELAUNCHED and notifies[#notifies]:find("not started by the Open77 launcher"), "without the launcher: what to do")
    LCTX = { launched = true, serverId = "0b6f1d1e-1111-2222-3333-444455556666", connectEndpoint = "127.0.0.1:11796" }
    pageEmits("maps:relaunch", {})
    check(RELAUNCHED and RELAUNCHED.ep == "127.0.0.1:11796" and QUIT, "with it: the launcher restarts the game for this server")
    -- far away the look stops; it never counts looks from afar
    player.x, player.y = 900.0, 1800.0
    before = #serverEvents
    frames(30, 1000)
    check(#presenceEvents(before) == 0, "no looks from afar")
    -- the map turns up (its sector streamed in late): the warning is taken back
    player.x, player.y = 470.0, 1790.0
    draws.terrainFn = function(x, y, fromZ) return math.min(fromZ, 174.0) end
    before = #serverEvents
    frames(60, 1000)
    local back = presenceEvents(before)
    check(#back == 1 and back[1].state == "present", "a later look finds it: the server is told")
    check(notifies[#notifies]:find("is at its spot in your game"), "and the player")
    check(lastSend("maps:stale") and lastSend("maps:stale").present == true, "and the panel drops the warning")
    -- terrain under the map's spot is invisible to these rays: with the old files they find nothing
    fromServer("state", { name = "cedar_crest", display = "Cedar Crest Estate", running = { x = 480.0, y = 1800.03, z = 174.0, yaw = 0.0 },
                          presets = {}, history = {} })
    draws.terrainFn = function(x, y, fromZ) return nil end
    before = #serverEvents
    frames(40, 1000)
    reported = presenceEvents(before)
    check(#reported == 1 and reported[1].state == "missing", "old files over terrain: missing")
    draws.terrainFn = nil
    player.x, player.y, player.z = 100.0, 200.0, 30.0
end

-- ------------------------------------------------------------------ moved at the last restart
fromServer("init", { maps = { { name = "cedar_crest", display = "Cedar Crest Estate", running = MAPS[1].running, presets = {},
                                history = {}, apply = { state = "applied", appliedAt = 1791515154 } } },
                     rights = { cedar_crest = true }, config = {}, baker = { known = true }, server = { boot = 1791515150 } })
check(notifies[#notifies]:find("moved at the server's last restart"), "the people who manage a moved map are told how to see it")
local n0 = #notifies
fromServer("init", { maps = { { name = "cedar_crest", display = "Cedar Crest Estate", running = MAPS[1].running, presets = {},
                                history = {}, apply = { state = "applied", appliedAt = 1791515154 } } },
                     rights = { cedar_crest = true }, config = {}, baker = { known = true }, server = { boot = 1791515150 } })
check(#notifies == n0, "once")
fromServer("init", { maps = MAPS, rights = { cedar_crest = true, harbor_dock = false }, config = { defaultCountdown = 60 },
                     baker = { known = true, hook = true, version = "1.0.0" } })

-- ------------------------------------------------------------------ a big map: never one long call
if F2O77_MAPS.big_map then
    cost.max, cost.where = 0, "-"
    fromServer("state", { name = "big_map", display = "Big Map", running = { x = 0.0, y = 0.0, z = 50.0, yaw = 0.0 },
                          presets = {}, history = {} })
    fromServer("init", { maps = { MAPS[1], { name = "big_map", display = "Big Map",
                                              running = { x = 0.0, y = 0.0, z = 50.0, yaw = 0.0 }, presets = {}, history = {} } },
                         rights = { cedar_crest = true, big_map = true }, config = {}, baker = { known = true } })
    fromServer("open", {})
    pageEmits("maps:edit", { map = "big_map" })
    frames(60)
    local want = 0
    for _ in (F2O77_MAPS.big_map.ghost):gmatch("[^\n]+") do want = want + 1 end
    check(totalLines() >= want, ("the whole big wireframe is drawn: %d of %d lines"):format(totalLines(), want))
    press("forward")
    frames(30)
    release("forward")
    frames(20)
    check(totalLines() >= want, "and again after moving")
    press("cancel")
    pageEmits("maps:close", {})
end
check(cost.max <= CALL_BUDGET, ("every call within %d instructions; the costliest was %s at %d"):format(
    CALL_BUDGET, cost.where, cost.max))

-- ------------------------------------------------------------------ the agent (maps @<id> ... through the server)
local function agentLine(id)
    for i = #LOG, 1, -1 do
        local l = LOG[i]
        local j = l:find("@agent ", 1, true)
        if j and l:find('"id":"' .. id .. '"', 1, true) then return l:sub(j + 7) end
    end
end
local hellosNow = countServer("f2o77_maps:hello")
frames(400)
check(countServer("f2o77_maps:hello") == hellosNow, "the server answered: no more hellos")
fromServer("agent:reply", { id = "r1", ok = true, lines = { "f2o77_maps 1.3.0" }, data = { version = "1.3.0" } })
check(agentLine("r1") and agentLine("r1"):find('"version":"1.3.0"', 1, true), "a server answer goes to the log as @agent JSON")

-- a camera over the map: within 250 m of the player, given back on `off`, on the panel and on time
player.x, player.y, player.z = 480.0, 1800.0, 175.0
fromServer("agent:do", { id = "v1", op = "view", map = "cedar_crest", args = { mode = "top" } })
check(CAMS.active and agentLine("v1"):find('"view":"top"', 1, true), "top view taken: " .. tostring((agentLine("v1"))))
local cam = CAMS.list[CAMS.active]
check(cam.position.z > 175 + 50 and math.abs(cam.lookAt.z - 174) < 0.01, "high above the map, looking down at it")
check(lastSend("maps:agent") and lastSend("maps:agent").view == true, "the page shows the agent holds the camera")
fromServer("agent:do", { id = "v2", op = "view", map = "cedar_crest", args = { mode = "oblique", azimuth = 90 } })
check(CAMS.active and agentLine("v2"):find('"view":"oblique"', 1, true), "oblique view")
fromServer("agent:do", { id = "v3", op = "view", map = "cedar_crest", args = { mode = "off" } })
check(not CAMS.active and agentLine("v3"):find('"ok":true', 1, true), "view off gives the camera back")
player.x, player.y = 0.0, 0.0
fromServer("agent:do", { id = "v4", op = "view", map = "cedar_crest", args = { mode = "top" } })
check(not CAMS.active and agentLine("v4"):find("camera_too_far_from_player", 1, true), "too far from the player: refused")
player.x, player.y = 480.0, 1800.0
fromServer("agent:do", { id = "v5", op = "view", map = "cedar_crest", args = { mode = "top", seconds = 5 } })
check(CAMS.active, "view again")
frames(400)                                         -- 6.4 s
check(not CAMS.active, "the camera is given back when its time is up")
fromServer("agent:do", { id = "v6", op = "view", map = "cedar_crest", args = { mode = "top" } })
fromServer("open", {})
check(not CAMS.active, "opening the panel takes the view back")
pageEmits("maps:close", {})
fromServer("agent:do", { id = "v7", op = "view", map = "nope", args = { mode = "top" } })
check(agentLine("v7"):find("unknown_map", 1, true), "unknown map")

-- the wireframe without the panel
draws.tags = {}
fromServer("agent:do", { id = "w1", op = "wire", map = "cedar_crest", args = { on = true } })
check(agentLine("w1"):find('"wire":true', 1, true), "wire on")
framesUntil(function() return totalLines() > 0 end, 200)
check(totalLines() > 0, "the agent's wireframe is drawn")

-- the roads around it: asked from the server, received in parts, drawn, answered
fromServer("agent:do", { id = "d1", op = "roads", map = "cedar_crest", args = { on = true } })
local ask = lastServer("f2o77_maps:roads")
check(type(ask) == "table" and ask.key and ask.r >= 60 and near(ask.x, 480, 60), "the client asks the server for the lanes around the map")
fromServer("agent:roads", { key = ask.key, part = 2, parts = 2, lanes = { "8 p 2 470 1790 174 490 1790 174" } })
check(not agentLine("d1"), "waits for every part")
fromServer("agent:roads", { key = ask.key, part = 1, parts = 2,
                            lanes = { "7 r 5 400 1750 173 560 1750 173", "9 i 5 460 1760 173 470 1770 173 480 1780 173" } })
framesUntil(function() return agentLine("d1") ~= nil end, 30)
check(agentLine("d1") and agentLine("d1"):find('"lanes":3', 1, true), "answered once the lanes are in: " .. tostring((agentLine("d1"))))
framesUntil(function() return draws.tags["f2o77_maps_road1"] ~= nil end, 120)
check(draws.tags["f2o77_maps_road1"] and draws.tags["f2o77_maps_road1"] >= 4, "the roads are drawn under their own tag")
local roadColors = {}
for _, ln in ipairs(draws.last["f2o77_maps_road1"]) do roadColors[ln.color] = true end
check(roadColors["#F4F4F4E6"] and roadColors["#FFF27ACC"] and roadColors["#9AA5B190"], "car lanes, junctions and pavement")
-- a city block: 160 lanes of 12 points, drawn without any long call and within the line budget
local before = cost.max
fromServer("agent:do", { id = "d3", op = "roads", map = "cedar_crest", args = { on = true } })
local ask3 = lastServer("f2o77_maps:roads")
local big = {}
for i = 1, 160 do
    local parts = { tostring(1000 + i), ({ "r", "i", "p", "h" })[i % 4 + 1], "5" }
    for k = 0, 11 do
        parts[#parts + 1] = tostring(400 + k * 12); parts[#parts + 1] = tostring(1700 + i * 1.5); parts[#parts + 1] = "173"
    end
    big[#big + 1] = table.concat(parts, " ")
end
frames(70)                                          -- a second later: a new request is allowed
fromServer("agent:do", { id = "d3", op = "roads", map = "cedar_crest", args = { on = true } })
ask3 = lastServer("f2o77_maps:roads")
fromServer("agent:roads", { key = ask3.key, part = 1, parts = 1, lanes = big })
framesUntil(function() return draws.tags["f2o77_maps_road2"] ~= nil end, 200)
local roadLinesNow = (draws.tags["f2o77_maps_road1"] or 0) + (draws.tags["f2o77_maps_road2"] or 0)
check(roadLinesNow == 900, "the roads take their share of lines and no more: " .. roadLinesNow)
check(cost.max <= CALL_BUDGET, "building the road lines never makes one long call: " .. cost.max .. " at " .. cost.where)
fromServer("agent:roads", { key = "stale", part = 1, parts = 1, lanes = {} })
check(draws.tags["f2o77_maps_road1"], "an answer to an older request is ignored")
fromServer("agent:do", { id = "d2", op = "roads", map = "cedar_crest", args = { on = false } })
framesUntil(function() return draws.tags["f2o77_maps_road1"] == nil end, 30)
check(not draws.tags["f2o77_maps_road1"], "roads off: cleared")
fromServer("agent:do", { id = "w2", op = "wire", map = "cedar_crest", args = { on = false } })

-- the host stops the drawing at any instruction, again and again: every line it hands to debugDraw
-- is still whole (the fake checks each point), and the drawing still completes
if F2O77_MAPS.big_map then
    draws.tags = {}
    fromServer("init", { maps = { MAPS[1], { name = "big_map", display = "Big Map",
                                              running = { x = 0.0, y = 0.0, z = 50.0, yaw = 0.0 }, presets = {}, history = {} } },
                         rights = { cedar_crest = true, big_map = true }, config = {}, baker = { known = true } })
    player.x, player.y, player.z = 0.0, 0.0, 50.0
    fromServer("agent:do", { id = "w9", op = "wire", map = "big_map", args = { on = true } })
    local stops = 0
    for k = 1, 160 do
        STOP.at = 400 + (k * 977) % 9000
        frames(1)
        if STOP.at == nil then stops = stops + 1 end
    end
    STOP.at = nil
    local want = 0
    for _ in (F2O77_MAPS.big_map.ghost):gmatch("[^\n]+") do want = want + 1 end
    framesUntil(function() return totalLines() >= want end, 600)
    check(stops >= 8, "the host stopped the drawing often: " .. stops)
    check(totalLines() >= want, ("drawn whole through %d stops: %d of %d lines"):format(stops, totalLines(), want))
    fromServer("agent:do", { id = "w10", op = "wire", map = "big_map", args = { on = false } })
    player.x, player.y, player.z = 480.0, 1800.0, 175.0
end

-- the ground check and probes for the agent
draws.terrain = 174.0
fromServer("agent:do", { id = "f1", op = "fit", map = "cedar_crest", args = {} })
framesUntil(function() return agentLine("f1") ~= nil end, 200)
check(agentLine("f1") and agentLine("f1"):find('"samples":', 1, true), "the ground check answers: " .. tostring((agentLine("f1"))))
draws.terrain = nil
fromServer("agent:do", { id = "p1", op = "probe", args = { points = { { 480, 1800 }, { 481, 1801, 200 } } } })
check(agentLine("p1"):find('"ground":12.25', 1, true), "probes answer the ground height")
-- a height map from above (screen rays see terrain, which groundZ does not): the scan's own camera,
-- stopped by the host anywhere and resumed, every cell once, the parts of a long answer whole
local function agentText(id)
    local parts, n = {}, nil
    for _, l in ipairs(LOG) do
        local j = l:find("@agent+ " .. id .. " ", 1, true)
        if j then
            local i, cnt, piece = l:sub(j):match("^@agent%+ %S+ (%d+)/(%d+) |(.*)|$")
            if i then parts[tonumber(i)], n = piece, tonumber(cnt) end
        end
    end
    if n then
        for i = 1, n do if not parts[i] then return nil end end
        return table.concat(parts, "", 1, n)
    end
    return agentLine(id)
end
local function cells(text)
    local body = text:match('"cells":"([^"]*)"')
    if not body then return nil end
    local hits, misses = 0, 0
    for item in (body .. ";"):gmatch("([^;]*);") do
        if item == "" then
            misses = misses + 1
        else
            check(item:match("^%-?%d+%.%d,%-?%d+%.%d,%-?%d+%.%d,%d+$"), "a cell is x,y,z,material: " .. item)
            hits = hits + 1
        end
    end
    return hits, misses
end
fromServer("agent:do", { id = "vx", op = "view", map = "cedar_crest", args = { mode = "off" } })
fromServer("agent:do", { id = "sc0", op = "scan", args = {} })
check(agentLine("sc0") and agentLine("sc0"):find("no_agent_view", 1, true), "a scan needs a camera or a point")
fromServer("agent:do", { id = "sc1", op = "scan", args = { x = 0, y = 0, z = 0 } })
check(agentLine("sc1") and agentLine("sc1"):find("camera_too_far_from_player", 1, true), "a scan's camera stays near the player")
fromServer("agent:do", { id = "sc2", op = "scan", args = { x = 500, y = 1800, z = 174, cols = 10, rows = 6, settle = 0 } })
check(CAMS.active and CAMS.list[CAMS.active].position.z > 174 + 100, "the scan's own camera, high above the point")
local scanStops, scanStopsInside = 0, 0
for k = 1, 600 do
    if agentText("sc2") then break end
    if k % 2 == 1 then STOP.at = 100 + (k * 211) % 2600 end   -- every other frame stopped somewhere
    local castsBefore = SCRAY.calls
    frames(1)
    if STOP.at == nil and k % 2 == 1 then
        scanStops = scanStops + 1
        if SCRAY.calls > castsBefore then scanStopsInside = scanStopsInside + 1 end
    end
    STOP.at = nil
end
STOP.at = nil
local text2 = agentText("sc2")
check(text2, "the scan answers")
local h2, m2 = cells(text2)
check(h2 == 54 and m2 == 6 and text2:find('"misses":6', 1, true), ("every cell once: %s hits, %s misses"):format(tostring(h2), tostring(m2)))
check(text2:find('"materials":["terrain"]', 1, true), "the materials, each once")
check(scanStops >= 3 and scanStopsInside >= 1, ("the host stopped the scan: %d stops, %d while it cast rays"):format(
    scanStops, scanStopsInside))
check(SCRAY.calls >= 60, "every cell cast")
check(not CAMS.active, "the scan gives its camera back")
local costBefore, whereBefore = cost.max, cost.where
fromServer("agent:do", { id = "sc3", op = "scan", args = { x = 470, y = 1790, z = 174, keep = true } })
framesUntil(function() return agentText("sc3") ~= nil end, 300)
local text3 = agentText("sc3")
check(text3 and #text3 > 3000, "a long answer in parts, put together")
local h3, m3 = cells(text3 or "")
check(h3 and h3 + m3 == 1000 and m3 == 75, ("40 x 25 cells: %s hits, %s misses"):format(tostring(h3), tostring(m3)))
check(CAMS.active, "keep: the camera stays for the next look")
cost.max, cost.where = costBefore, whereBefore      -- the fake JSON encoder is Lua; the game's is native
-- one ray through the agent's camera, everything the engine says
fromServer("agent:do", { id = "ry1", op = "ray", args = { u = 0.5, v = 0.5 } })
check(agentLine("ry1") and agentLine("ry1"):find('"material":"terrain"', 1, true) and agentLine("ry1"):find('"origin"', 1, true),
      "a ray through the agent's camera: " .. tostring(agentLine("ry1")))
-- rays the engine refuses (its camera state): tried again, then marked; a long run of them ends the scan
SCRAY.failFrom = 0.9
fromServer("agent:do", { id = "sc4", op = "scan", args = { x = 480, y = 1800, z = 174, cols = 10, rows = 10, settle = 0 } })
framesUntil(function() return agentText("sc4") ~= nil end, 300)
local t4 = agentText("sc4") or ""
check(t4:find('"failed":10', 1, true) and t4:find('"camera_projection_mismatch":', 1, true),
      "the refused rays are counted with their reason: " .. t4:sub(1, 300))
local h4, m4 = 0, 0
for item in ((t4:match('"cells":"([^"]*)"') or "") .. ";"):gmatch("([^;]*);") do
    if item == "!" then m4 = m4 + 1 elseif item ~= "" then h4 = h4 + 1 end
end
check(m4 == 10 and h4 == 81, ("refused cells marked: %d hits, %d refused"):format(h4, m4))
SCRAY.failFrom = 0.0
fromServer("agent:do", { id = "sc5", op = "scan", args = { x = 480, y = 1800, z = 174, cols = 10, rows = 10, settle = 0 } })
framesUntil(function() return agentLine("sc5") ~= nil end, 600)
check(agentLine("sc5") and agentLine("sc5"):find("screen_rays_failed", 1, true) and agentLine("sc5"):find("camera_projection_mismatch", 1, true),
      "every ray refused: the scan stops and says why: " .. tostring(agentLine("sc5")))
check(not CAMS.active, "and gives its camera back")
SCRAY.failFrom = nil
fromServer("agent:do", { id = "vz", op = "view", map = "cedar_crest", args = { mode = "top" } })
fromServer("agent:do", { id = "vy", op = "view", map = "cedar_crest", args = { mode = "off" } })
fromServer("agent:do", { id = "s1", op = "state", map = "cedar_crest", args = {} })
check(agentLine("s1"):find('"pageReady":true', 1, true) and agentLine("s1"):find('"answered":true', 1, true),
      "the client's own state for the agent")
fromServer("agent:say", { text = "moving the estate 4 m east", by = "Claude" })
check(lastSend("maps:agent") and lastSend("maps:agent").text == "moving the estate 4 m east", "the agent's words on screen")
fromServer("agent:do", { id = "x1", op = "dance" })
check(agentLine("x1"):find("unknown_agent_op", 1, true), "unknown op")

-- stop
fire("onClientResourceStop", "f2o77_maps")
check(not focus.keyboard, "focus released on stop")

realprint(("OK maps manager client (costliest call %s: %d instructions, %d calls)"):format(cost.where, cost.max, cost.calls))
