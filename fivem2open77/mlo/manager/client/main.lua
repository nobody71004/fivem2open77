-- f2o77_maps: the in-game manager of converted FiveM maps (client side).
--
-- Open it with the map-manager key (default F9; rebind it under Pause > Settings > KEY BINDINGS,
-- or in the manager's Keys tab).  The panel lists every map on the server: where it is now
-- (running), where it is going (pending), presets, and the Apply step that moves the baked map
-- (a rebuild on the server host, then a server restart).  "Move in the world" closes the panel
-- and puts a wireframe of the whole map on your controls:
--
--   arrows        move forward / back / left / right (relative to the camera)
--   PAGE UP/DOWN  raise / lower            HOME / END   rotate left / right
--   SHIFT (hold)  x6 speed                 CTRL (hold)  fine steps
--   INSERT        follow the crosshair on/off
--   DELETE        drop onto the ground under the anchor
--   ENTER         save as the pending spot BACKSPACE    cancel
-- (all of them rebindable the same way).  Saving never moves the geometry by itself: Apply does.
local RES = GetCurrentResourceName()
local CFG = F2O77_CONFIG or {}
local DEFS = F2O77_MAPS or {}
local EV = "f2o77_maps:"
local KEYS = CFG.editorKeys or {}

local TAG = "f2o77_maps_"
local BATCH = 500                      -- lines per debugDraw tag (engine cap 512)
local BATCH_MIN = 200                  -- the smallest batch (32 tags per resource: map + roads fit)
local batchCap = BATCH                 -- halves when handing a batch over runs out the host's script budget
local MAX_LINES = 3000                 -- engine cap: 8192 vertices across every resource's batches (leave others room)
local TTL = 9.0                        -- seconds a drawn batch lives without a refresh (engine cap 10)
local REFRESH = 5.0                    -- redrawn this often while nothing moves, well before the TTL
local MAX_DISTANCE = 3000
-- Open77 gives every client host 6 ms of Lua per frame for ALL resources together (2 ms on older
-- clients) and stops a call that runs past it ("script execution budget exceeded"): the heavy work
-- here -- reading a ghost, placing lines, ground rays -- stops after FRAME_BUDGET of real time and
-- carries on next frame; after a refusal it backs off and works in smaller slices for a while.
local FRAME_BUDGET = 0.0006            -- seconds per frame this resource allows itself
local PARSE_SLICE = 400                -- ghost lines read per frame, at most (the time budget stops it earlier)
local LINE_SLICE = 500                 -- wireframe lines placed per frame, at most
local FAST_INTERVAL = 0.05             -- the fast lane follows a moving map 20 times a second
local BACKGROUND_DELAY = 20.0          -- seconds after the start before other maps' ghosts are read
local FIT_SAMPLES = 32                 -- points along the outline checked against the ground
local FIT_GATES = 8                    -- and gate thresholds (where the drives meet the street)
local FIT_MAX_DISTANCE = 200.0         -- metres from the player: farther, the world's collision may not be streamed
local FIT_RAYS = 6                     -- ground rays per frame
local FIT_DELAY = 0.3                  -- seconds the map stands still before the ground check
local FIT_TOLERANCE = 0.35             -- metres: closer than this counts as sitting on the ground
local REDUCED_RETRY = 30.0             -- seconds after a quota refusal before more detail is tried again
local RUNNING_COLOR = "#B0B0B0E6"      -- outline of where the map is now
local PENDING_COLOR = "#FF7AE0FF"      -- and of its saved new spot
local FLOAT_COLOR = "#FF3B30FF"        -- the map's ground above the world's (it would float)
local BURIED_COLOR = "#3B82FFFF"       -- the world above the map's ground (buried, or a building in the way)
local STYLES = {
    [4] = { color = "#FFE14DFF", thickness = 4.0, lift = 0.2 },   -- the outline at ground level
    [5] = { color = "#4DFF6AFF", thickness = 4.0, lift = 0.0 },   -- gates
    [0] = { color = "#FFFFFF59", thickness = 1.0, lift = 0.0 },   -- the box around everything
    [1] = { color = "#19D5E0FF", thickness = 2.0, lift = 0.0 },   -- buildings
    [2] = { color = "#FFB000B3", thickness = 1.0, lift = 0.0 },   -- walls and floors
    [3] = { color = "#7CFF6BB0", thickness = 1.0, lift = 0.0 },   -- ground (ghosts from older builds)
    box = { color = "#FFE14DFF", thickness = 3.0, lift = 0.0 },   -- the box, when a ghost has no outline
    mark = { color = "#FFFFFFFF", thickness = 3.0, lift = 0.0 },  -- anchor cross, ground check
    outline = { color = RUNNING_COLOR, thickness = 2.0, lift = 0.0 },
}
local ORDER = { 4, 5, 0, 1, 2, 3 }     -- what a drawing places first

local server = { maps = {}, rights = {}, config = {}, baker = { known = false } }   -- what the server told us
local page = nil
local pageReady = false
local needInit = false                 -- the page still needs the server's picture (sent from the frame loop)
local lastPing = -1e9                  -- knocks on the page until it answers (see createPage)
local stale = {}                       -- map -> true: this game has its old files (see presenceStep)
local panelOpen = false
local edit = nil                       -- the in-world editor: { map, anchor, aim, dirty, ... }
local preview = nil                    -- map name whose pending spot is drawn while the panel is used
local held = {}
local pendingOps = {}                  -- token -> page request
local opSeq = 0
-- talking to the server: until it answers, hello is asked again (3 s apart for a minute, then 15 s)
local link = { answered = false, loading = false, helloAt = -1e9, hellos = 0, since = nil, told = {},
               names = nil, headerAt = nil, missingAsks = 0, missingAt = -1e9, slim = false }
-- what an agent asked of this client (the map agent on this computer, through the game's debug
-- bridge and the server's `maps @<id> ...` commands): its camera, its wireframe, the roads, its words
local agent = { view = nil, roads = nil, said = nil, wire = nil, fit = nil, scan = nil, errors = 0, release = nil }
-- the lanes around the map being moved (from the server's road data): see "roads" below
local ROAD_MAX = 900                   -- lines for the roads (the map drawing keeps MAX_LINES - this)
local roads = { lanes = nil, key = nil, want = nil, asked = -1e9, center = nil, drawnAt = -1e9, dirty = false,
                tags = 0, missing = false, lines = 0, shown = false, parts = {} }

local function now()
    if GetGameTimer then return GetGameTimer() / 1000.0 end
    return os.clock()
end

-- one line in the game's log (red4ext/logs/open77-*.log): the manager's own story, which the map
-- agent and a person reading the log both follow; `once` keys a line that would otherwise repeat
local function say(once, fmt, ...)
    if once then
        if link.told[once] then return end
        link.told[once] = true
    end
    print(("[%s] " .. fmt):format(RES, ...))
end

-- an answer for the map agent: one line, `@agent ` and JSON, which it finds in the game's log.  The
-- log keeps about 4 KiB of a line: a longer answer goes out in numbered parts,
-- `@agent+ <id> <i>/<n> |<piece>|` (between bars: a piece may end with a space), which the agent
-- puts back together.
local AGENT_LINE = 3000
local agentChunks
-- the log lines of one answer (several numbered parts when it is long)
local function agentLines(t)
    local enc = Open77.json and Open77.json.encode
    local ok, text = false, nil
    if enc then ok, text = pcall(enc, t) end
    if not ok and type(text) == "string" and text:find("budget", 1, true) then
        error(text, 0)                  -- the host stopped this frame: not a failure, the caller tries again
    end
    if not ok or type(text) ~= "string" then
        text = ('{"id":"%s","ok":false,"error":"encode_failed"}'):format(tostring(t and t.id or ""))
    end
    return agentChunks(t and t.id, text)
end

-- JSON text as log lines: one `@agent` line, or numbered `@agent+` parts when it is long
agentChunks = function(id, text)
    if #text <= AGENT_LINE then return { "@agent " .. text } end
    id = tostring(id or "?")
    local n = math.ceil(#text / AGENT_LINE)
    local lines = {}
    for i = 1, n do
        lines[i] = ("@agent+ %s %d/%d |%s|"):format(id, i, n, text:sub((i - 1) * AGENT_LINE + 1, i * AGENT_LINE))
    end
    return lines
end

local function agentPrint(t)
    for _, line in ipairs(agentLines(t)) do print(line) end
end

local function wrapYaw(y)
    y = y % 360
    if y > 180 then y = y - 360 end
    return y
end

local function describe(a)
    return ("x %.1f  y %.1f  z %.1f  yaw %.0f"):format(a.x, a.y, a.z, a.yaw)
end

local function display(name)
    local m = server.maps[name]
    return (m and m.display) or (DEFS[name] and DEFS[name].display) or name
end

local function notify(text)
    if panelOpen and page then
        page:send("maps:toast", { text = text, kind = "info" })
        return
    end
    local ok = Open77.hud and Open77.hud.notify and Open77.hud.notify(text, { replace = true })
    if not ok then print("[" .. RES .. "] " .. text) end
end

-- A map's new package reaches a player through the launcher, which installs a server's files before
-- it starts the game: a game started before the server installed them keeps the old ones.  The
-- launcher can restart this game for the same server with the new files.
local function canRelaunch()
    local ctx = Open77.session and Open77.session.launcherContext and Open77.session.launcherContext()
    return type(ctx) == "table" and ctx.launched == true and (ctx.serverId or "") ~= "" and
        (ctx.connectEndpoint or "") ~= "" and Open77.session.relaunchViaLauncher ~= nil, ctx
end

local function relaunch()
    local ok, ctx = canRelaunch()
    if ok then
        local done, why = Open77.session.relaunchViaLauncher(ctx.serverId, ctx.connectEndpoint)
        if done then
            if Open77.session.quitGame then Open77.session.quitGame() end
            return true
        end
        notify("The launcher could not take over (" .. tostring(why) .. "): quit the game and start it again from the Open77 launcher.")
        return false
    end
    notify("This game was not started by the Open77 launcher: quit it and start it again from the launcher.")
    return false
end


-- ------------------------------------------------------------------ wireframes
-- The wireframe is the map's ghost (shared/maps/<map>.lua, from ghost.py): lines in the map's own
-- frame, most useful first -- its outline at ground level (kind 4), the gates (5), the box around
-- everything (0), the buildings (1), their walls and floors (2).  Everything heavy runs in the frame
-- loop a slice per frame -- reading the ghost, placing it, handing it to debugDraw -- because Open77
-- stops any Lua call that runs past its execution budget, and a whole big map at once does.
-- While the map moves, its outline, gates and anchor cross are redrawn every frame (the "fast
-- lane", a few dozen lines); the rest follows a few times a second.
local ghosts = {}                      -- name -> { src, pos, kinds = { [k] = { x1,y1,z1,x2,y2,z2, ... } }, ready }
local job = nil                        -- the drawing under way (stepDraw)
local want = nil                       -- the newest drawing asked for: { name, a, full }
local drawnTags = 0                    -- tags the last finished drawing used
local fastTags = 0                     -- of those, the first ones the fast lane may redraw
local lastDrawAt = -1e9
local wire = { state = "off", why = nil, told = {} }   -- off | loading | full | reduced
local reduced = {}                     -- map -> { at, level } after quota refusals: less detail for a while
local fit = nil                        -- the ground under the outline: { key, points, float, buried, missing }
local fitJob = nil
-- a real-time clock for the slices (no `os` in the sandbox; GetGameTimer only moves between frames)
local clock = (Open77.time and Open77.time.monotonic) or function() return now() end
local budgetScale = 1.0                -- shrinks after a budget refusal, grows back slowly
local backoffUntil = -1e9              -- no heavy work until then (right after a refusal)
local deadline = 1e18                  -- the end of this frame's slice (frameStep sets it)
local function timeLeft() return clock() < deadline end

local function styleOf(k) return STYLES[k] or STYLES[1] end

-- how much of a map is drawn: 0 everything; after a quota refusal (debugDraw's vertex budget is
-- shared by every resource) 1 = the outline, gates, cross and ground check, and after another one
-- 2 = the box and the cross.  Full detail is tried again after a while (the other resource may be
-- done with its geometry), and at once when an editing session starts.
local function detailOf(name)
    local r = reduced[name]
    if r and now() - r.at < REDUCED_RETRY then return r.level end
    reduced[name] = nil
    return 0
end

local function ghostOf(name)
    local G = ghosts[name]
    if G then return G end
    local def = DEFS[name] or {}
    G = { src = type(def.ghost) == "string" and def.ghost or "", pos = 1, kinds = {}, ready = false, n = 0 }
    ghosts[name] = G
    return G
end

-- read up to `budget` lines of a ghost; true once all of it is read
local LINE_RE = "^%s*(%S+)%s+(%S+)%s+(%S+)%s+(%S+)%s+(%S+)%s+(%S+)%s+(%S+)"
local function parseStep(G, budget)
    if G.ready then return true end
    local s = G.src
    for n = 1, budget do
        if n % 16 == 0 and not timeLeft() then return false end
        local i, j, x1, y1, z1, x2, y2, z2, k = s:find(LINE_RE, G.pos)
        if not i then
            G.ready, G.src = true, nil
            return true
        end
        G.pos = j + 1
        x1, y1, z1, x2, y2, z2, k = tonumber(x1), tonumber(y1), tonumber(z1), tonumber(x2), tonumber(y2), tonumber(z2), tonumber(k)
        if x1 and y1 and z1 and x2 and y2 and z2 then
            if not STYLES[k] or k == "mark" then k = 1 end
            local list = G.kinds[k]
            if not list then list = {}; G.kinds[k] = list end
            local n = #list
            list[n + 1], list[n + 2], list[n + 3], list[n + 4], list[n + 5], list[n + 6] = x1, y1, z1, x2, y2, z2
            G.n = G.n + 1
        end
    end
    return false
end

local function boundsBox(name)
    local b = (DEFS[name] or {}).bounds
    local out = {}
    if not (b and b.min and b.max) then return out end
    local mn, mx = b.min, b.max
    local c = { { mn[1], mn[2] }, { mx[1], mn[2] }, { mx[1], mx[2] }, { mn[1], mx[2] } }
    for i = 1, 4 do
        local p, q = c[i], c[i % 4 + 1]
        for _, z in ipairs({ mn[3], mx[3] }) do
            out[#out + 1], out[#out + 2], out[#out + 3] = p[1], p[2], z
            out[#out + 1], out[#out + 2], out[#out + 3] = q[1], q[2], z
        end
        out[#out + 1], out[#out + 2], out[#out + 3] = p[1], p[2], mn[3]
        out[#out + 1], out[#out + 2], out[#out + 3] = p[1], p[2], mx[3]
    end
    return out
end

-- the outline that stands for the map at another spot (where it is now, the saved new spot)
local function outlineOf(name)
    local G = ghostOf(name)
    local foot = G.ready and G.kinds[4]
    if foot and #foot > 0 then
        if #foot > 6 * 300 then return boundsBox(name) end
        return foot
    end
    return boundsBox(name)
end

local function crossOf(name)
    local def = DEFS[name] or {}
    local size = 6.0
    if def.bounds then
        size = math.max(4.0, math.min(25.0, 0.08 * math.max(def.bounds.max[1] - def.bounds.min[1],
                                                           def.bounds.max[2] - def.bounds.min[2])))
    end
    -- x red, y green with an arrowhead (where the map's north points), z blue
    return { 0, 0, 0, size, 0, 0,  0, 0, 0, 0, size, 0,  0, 0, 0, 0, 0, size,
             0, size, 0, -size * 0.25, size * 0.75, 0,  0, size, 0, size * 0.25, size * 0.75, 0 },
           { "#FF3030FF", "#30FF30FF", "#3080FFFF", "#30FF30FF", "#30FF30FF" }
end

local function sameSpot(a, b)
    return a and b and math.abs(a.x - b.x) < 0.01 and math.abs(a.y - b.y) < 0.01 and math.abs(a.z - b.z) < 0.01 and
        math.abs(((a.yaw or 0) - (b.yaw or 0) + 180) % 360 - 180) < 0.05
end
local function spotKey(a)
    return ("%.2f %.2f %.2f %.1f"):format(a.x, a.y, a.z, a.yaw or 0)
end

-- the outline of `name` placed at spot b, in world coordinates (cached: the spots where a map is now
-- and where it is going change rarely, the drawings that show them often)
local outlineCache = {}
local function outlineWorld(name, b, color)
    local key = name .. "|" .. spotKey(b) .. "|" .. color .. "|" .. tostring(ghostOf(name).ready)
    local hit = outlineCache[key]
    if hit then return hit end
    local ol = outlineOf(name)
    local r = math.rad(b.yaw or 0)
    local c, s = math.cos(r), math.sin(r)
    local o, oc = {}, {}
    for i = 1, #ol, 3 do
        local x, y, z = ol[i], ol[i + 1], ol[i + 2]
        o[#o + 1], o[#o + 2], o[#o + 3] = b.x + x * c - y * s, b.y + x * s + y * c, b.z + z + 0.1
    end
    for _ = 1, #ol / 6 do oc[#oc + 1] = color end
    local n = 0
    for _ in pairs(outlineCache) do n = n + 1 end
    if n > 8 then outlineCache = {} end
    hit = { lines = o, colors = oc }
    outlineCache[key] = hit
    return hit
end

local FAST_MAX = 400                   -- the fast lane's lines per frame, at most (else the drawing does it)

-- what a drawing of `name` at `a` consists of, in order: segments of lines with one style each;
-- fastOnly: just the ones the fast lane redraws while the map moves
local function planFor(name, a, full, fastOnly)
    local G = ghostOf(name)
    local plan = {}
    local function add(list, style, colors, world, fast)
        if list and #list >= 6 then
            plan[#plan + 1] = { list = list, style = style, colors = colors, world = world, fast = fast }
        end
    end
    local level = detailOf(name)
    local function addCross()
        local cross, colors = crossOf(name)
        local marks, mcolors = {}, {}
        for i = 1, #cross do marks[i] = cross[i] end
        for i = 1, #colors do mcolors[i] = colors[i] end
        add(marks, STYLES.mark, mcolors, false, true)
    end
    if level >= 2 then
        add(boundsBox(name), STYLES.box, nil, false, true)
        addCross()
        return plan
    end
    local onlyBox = not G.ready or G.n == 0
    -- a ghost from an older build has no outline (kind 4): its box stands in for it, drawn as boldly
    local boxForOutline = not onlyBox and not (G.kinds[4] and #G.kinds[4] > 0)
    if onlyBox then
        add(boundsBox(name), styleOf(0), nil, false, true)
    else
        if boxForOutline then
            add((G.kinds[0] and #G.kinds[0] > 0) and G.kinds[0] or boundsBox(name), STYLES.box, nil, false, true)
        else
            add(G.kinds[4], styleOf(4), nil, false, true)
        end
        add(G.kinds[5], styleOf(5), nil, false, true)
    end
    addCross()
    if fastOnly then return plan end
    if fit and fit.key == spotKey(a) and fit.name == name and #fit.lines > 0 then
        add(fit.lines, STYLES.mark, fit.colors, true, false)
    end
    if level >= 1 then return plan end
    if not onlyBox and full ~= false then
        for _, k in ipairs(ORDER) do
            if k ~= 4 and k ~= 5 and not (k == 0 and boxForOutline) then add(G.kinds[k], styleOf(k)) end
        end
    end
    -- where it is now (grey) and the saved new spot (pink), when they are elsewhere
    local m = server.maps[name]
    if m and m.running and not sameSpot(m.running, a) then
        local w = outlineWorld(name, m.running, RUNNING_COLOR)
        add(w.lines, STYLES.outline, w.colors, true)
    end
    if m and m.pending and not sameSpot(m.pending, a) and not sameSpot(m.pending, m.running) then
        local w = outlineWorld(name, m.pending, PENDING_COLOR)
        add(w.lines, STYLES.outline, w.colors, true)
    end
    return plan
end

local function setTag(n, lines, style)
    return Open77.debugDraw.set(TAG .. n, { lines = lines, ttl = TTL, maxDistance = MAX_DISTANCE,
                                             thickness = style.thickness })
end

-- Batches reuse their line tables from one drawing to the next (a moving map redraws dozens of
-- times a second: thousands of new tables a second would be garbage the collector pays for inside
-- this resource's frame budget).  A batch is { n = lines in use, lines = { {a={}, b={}, color}, ... } }.
local batches = {}                     -- tag number -> batch
local function batchFor(tag)
    local b = batches[tag]
    if not b then b = { n = 0, lines = {} }; batches[tag] = b end
    return b
end
local function startBatch(b) b.n = 0 end
local function finishBatch(b)
    local lines = b.lines
    for k = #lines, b.n + 1, -1 do lines[k] = nil end
    return lines
end

-- place up to `budget` lines of segment `seg` at anchor a, into batch `cur`, from `prog.i` (the
-- list index) on.  Open77 can stop this call at any instruction (the frame budget): a line counts
-- in the batch, and in `prog`, only once it is whole -- a half-written line would reach debugDraw
-- (invalid_debug_point) and a lost step would place lines twice.
local function place(seg, prog, cur, budget, ax, ay, az, c, s)
    local list, style, colors, world = seg.list, seg.style, seg.colors, seg.world
    local lift, color = style.lift or 0, style.color
    local n = #list
    local done = 0
    local lines = cur.lines
    local i = prog.i
    while i + 6 <= n and cur.n < batchCap and done < budget do
        local x1, y1, z1, x2, y2, z2 = list[i + 1], list[i + 2], list[i + 3], list[i + 4], list[i + 5], list[i + 6]
        local col = colors and colors[i // 6 + 1] or color
        local k = cur.n + 1
        local ln = lines[k]
        if not ln then
            ln = { a = {}, b = {} }
            lines[k] = ln
        end
        local pa, pb = ln.a, ln.b
        if world then
            pa.x, pa.y, pa.z, pb.x, pb.y, pb.z = x1, y1, z1, x2, y2, z2
        else
            pa.x, pa.y, pa.z = ax + x1 * c - y1 * s, ay + x1 * s + y1 * c, az + z1 + lift
            pb.x, pb.y, pb.z = ax + x2 * c - y2 * s, ay + x2 * s + y2 * c, az + z2 + lift
        end
        ln.color = col
        cur.n = k                                  -- whole now: it counts
        i = i + 6
        prog.i, prog.lines = i, (prog.lines or 0) + 1
        done = done + 1
    end
    return i, done
end

local function clearDrawing()
    if Open77.debugDraw and Open77.debugDraw.clear then Open77.debugDraw.clear() end
    drawnTags, fastTags = 0, 0
    job, want = nil, nil
end

local function requestDraw(name, a, full)
    if not (name and a) then return end
    want = { name = name, a = { x = a.x, y = a.y, z = a.z, yaw = a.yaw or 0 }, full = full ~= false }
end

local function tellOnce(key, text)
    if wire.told[key] then return end
    wire.told[key] = true
    notify(text)
end

-- a refusal from debugDraw: less detail after a quota refusal, nothing (and say why) otherwise
local function drawRefused(name, why)
    local was = job
    job = nil
    local level = detailOf(name)
    if why == "debug_geometry_quota" and level < 2 then
        reduced[name] = { at = now(), level = level + 1 }
        -- this resource's own batches count against the budget too: all of them go, then less comes back
        Open77.debugDraw.clear()
        drawnTags, fastTags = 0, 0
        wire.state, wire.why = "reduced", "debug_geometry_quota"
        tellOnce("quota", "Another resource is drawing debug geometry: the map is drawn with less detail for now.")
        if was and not want then requestDraw(was.name, was.a, was.full) end
        return
    end
    clearDrawing()
    -- tried again after REFRESH (or as soon as the map moves), not every frame
    lastDrawAt = now()
    wire.state, wire.why = "off", tostring(why)
    tellOnce("refused:" .. tostring(why), "wireframe unavailable: " .. tostring(why))
end

-- one slice of the drawing under way; starts the newest one asked for when none is
local function stepDraw(budget)
    if not (Open77.debugDraw and Open77.debugDraw.set) then
        if want then wire.state, wire.why, want = "off", "debug_draw_unavailable", nil end
        return
    end
    if not job then
        if not want then return end
        local w = want
        want = nil
        job = { name = w.name, a = w.a, full = w.full, plan = planFor(w.name, w.a, w.full), step = 1, i = 0,
                tag = 0, lines = 0, cur = nil, fast = 0 }
    end
    local J = job
    local r = math.rad(J.a.yaw or 0)
    local c, s = math.cos(r), math.sin(r)
    local ax, ay, az = J.a.x, J.a.y, J.a.z
    local sets = 0                     -- handing a batch to the engine is the costly part: one per frame
    while budget > 0 and sets < 1 do
        local seg = J.plan[J.step]
        if not seg then break end
        local cur = J.cur
        if not cur then
            cur = batchFor(J.tag + 1)
            startBatch(cur)
            J.cur = cur
        end
        local cap = (roads.shown and roads.lanes) and (MAX_LINES - ROAD_MAX) or MAX_LINES
        local _, done = place(seg, J, cur, math.min(budget, 32, math.max(0, cap - J.lines)), ax, ay, az, c, s)
        budget = budget - done
        local segDone = J.i + 6 > #seg.list or J.lines >= cap
        if cur.n >= batchCap or segDone then
            local t = J.tag + 1
            local called, ok, why = pcall(setTag, t, finishBatch(cur), seg.style)
            if not called then
                -- the host's script budget ran out inside the engine call: smaller batches from now on,
                -- and this drawing starts over with them (the frame loop backs off for a moment)
                if tostring(ok):find("budget", 1, true) and batchCap > BATCH_MIN then
                    batchCap = math.max(BATCH_MIN, math.floor(batchCap / 2))
                    want, job = want or { name = J.name, a = J.a, full = J.full }, nil
                end
                error(ok, 0)
            end
            J.tag = t                                -- (a budget stop inside the set leaves it as it was)
            sets = sets + 1
            if not ok then return drawRefused(J.name, why) end
            J.cur = nil
            if seg.fast and segDone then J.fast = J.tag end
            if segDone then J.step, J.i = J.step + 1, 0 end
            if J.lines >= cap then J.step = #J.plan + 1 end
        end
        if not timeLeft() then break end
    end
    if J.step > #J.plan then
        for t = J.tag + 1, drawnTags do Open77.debugDraw.clear(TAG .. t) end
        drawnTags, fastTags = J.tag, J.fast
        lastDrawAt = now()
        local G = ghostOf(J.name)
        wire.state = (not G.ready) and "loading" or (detailOf(J.name) > 0 and "reduced" or "full")
        if wire.state ~= "reduced" then wire.why = nil end
        job = nil
    end
end

-- the fast lane: the outline, gates and cross at the newest spot, into the tags they already hold
local function fastDraw(name, a)
    -- the tags the fast segments hold in the drawing on screen (or in the one under way, once it has
    -- placed them)
    local limit = fastTags
    if job then
        local seg = job.plan[job.step]
        if job.name ~= name or (seg and seg.fast) or job.fast == 0 then return end
        limit = job.fast
    end
    if limit == 0 or drawnTags == 0 and not job then return end
    local plan = planFor(name, a, false, true)
    local count = 0
    for _, seg in ipairs(plan) do count = count + #seg.list / 6 end
    if count > FAST_MAX then return end
    local r = math.rad(a.yaw or 0)
    local c, s = math.cos(r), math.sin(r)
    local tag = 0
    for _, seg in ipairs(plan) do
        if not seg.fast then break end
        local prog = { i = 0, lines = 0 }
        while prog.i + 6 <= #seg.list do
            tag = tag + 1
            if tag > limit then return end              -- the layout changed: the next drawing fixes it
            local cur = batchFor(tag)
            startBatch(cur)
            place(seg, prog, cur, batchCap, a.x, a.y, a.z, c, s)
            local ok, why = setTag(tag, finishBatch(cur), seg.style)
            if not ok then return drawRefused(name, why) end
        end
    end
end

-- the points the ground check looks at, in the map's frame: evenly along the outline, and the middle
-- of each gate's threshold { x, y, z, isGate } (cached: a ghost does not change)
local fitPoints = {}
local function fitPointsOf(name)
    local hit = fitPoints[name]
    if hit then return hit end
    local G = ghostOf(name)
    if not G.ready then return nil end
    local pts = {}
    local foot = G.kinds[4]
    if foot and #foot > 0 then
        local total = 0
        for i = 1, #foot, 6 do
            total = total + math.sqrt((foot[i + 3] - foot[i]) ^ 2 + (foot[i + 4] - foot[i + 1]) ^ 2)
        end
        local step = total / FIT_SAMPLES
        local at, run = step / 2, 0
        for i = 1, #foot, 6 do
            local x1, y1, z1, x2, y2, z2 = foot[i], foot[i + 1], foot[i + 2], foot[i + 3], foot[i + 4], foot[i + 5]
            local len = math.sqrt((x2 - x1) ^ 2 + (y2 - y1) ^ 2)
            while len > 0 and at <= run + len and #pts < FIT_SAMPLES do
                local f = (at - run) / len
                pts[#pts + 1] = { x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, z1 + (z2 - z1) * f, false }
                at = at + step
            end
            run = run + len
        end
    end
    local gates = G.kinds[5]
    if gates and #gates >= 24 then
        local leaves = math.floor(#gates / 24)       -- 4 lines per gate leaf, the threshold first
        local every = math.max(1, math.ceil(leaves / FIT_GATES))
        for l = 0, leaves - 1, every do
            local i = l * 24 + 1
            pts[#pts + 1] = { (gates[i] + gates[i + 3]) / 2, (gates[i + 1] + gates[i + 4]) / 2,
                              (gates[i + 2] + gates[i + 5]) / 2, true }
        end
    end
    fitPoints[name] = pts
    return pts
end

-- is a point of the map's own frame inside its footprint (even-odd over the outline; the box when
-- the ghost has no outline)
local function insideFootprint(name, lx, ly)
    local b = (DEFS[name] or {}).bounds
    if b and b.min and b.max and (lx < b.min[1] or lx > b.max[1] or ly < b.min[2] or ly > b.max[2]) then return false end
    local G = ghostOf(name)
    local foot = G.ready and G.kinds[4]
    if not foot or #foot == 0 then return b ~= nil end
    local inside = false
    for i = 1, #foot, 6 do
        local x1, y1, x2, y2 = foot[i], foot[i + 1], foot[i + 3], foot[i + 4]
        if (y1 > ly) ~= (y2 > ly) and lx < (x2 - x1) * (ly - y1) / (y2 - y1) + x1 then inside = not inside end
    end
    return inside
end

-- the ground under the outline at the editor's spot, a few rays per frame once the map stands still:
-- red lines where the map's ground would float above the world's, blue where the world is above it.
-- Points over the place the map is now are left out (the rays would find the map's own floors), and
-- so is all of it when the player is too far for the world there to be streamed.
local function fitStep(name, a, rays)
    if not (Open77.world and Open77.world.groundZ) then return end
    local key = spotKey(a)
    if fit and fit.key == key and fit.name == name and not fit.far then return end
    if not fitJob or fitJob.key ~= key or fitJob.name ~= name then
        local pts = fitPointsOf(name)
        if not pts or #pts == 0 then return end
        local m = server.maps[name]
        local run = m and m.running
        local function result(extra)
            fit = { name = name, key = key, lines = {}, colors = {}, float = 0, buried = 0, missing = 0, samples = 0,
                    skipped = 0, gateFloat = 0, gateBuried = 0, at = now() }
            for k, v in pairs(extra) do fit[k] = v end
        end
        if run and sameSpot(run, a) then return result({ here = true }) end
        local px, py = Open77.character.position()
        if px and py then
            local d = math.sqrt((px - a.x) ^ 2 + (py - a.y) ^ 2)
            if d > FIT_MAX_DISTANCE then
                if not (fit and fit.far and fit.key == key and now() - fit.at < 1.0) then result({ far = d }) end
                return
            end
        end
        fitJob = { name = name, key = key, a = { x = a.x, y = a.y, z = a.z, yaw = a.yaw },
                   run = run and { x = run.x, y = run.y, yaw = run.yaw or 0 } or nil, pts = pts, i = 0,
                   lines = {}, colors = {}, float = 0, buried = 0, missing = 0, skipped = 0, gateFloat = 0, gateBuried = 0 }
    end
    local F = fitJob
    local r = math.rad(F.a.yaw or 0)
    local c, s = math.cos(r), math.sin(r)
    local rc, rs
    if F.run then
        local rr = math.rad(-F.run.yaw)
        rc, rs = math.cos(rr), math.sin(rr)
    end
    for _ = 1, rays do
        F.i = F.i + 1
        local p = F.pts[F.i]
        if not p then
            fit = { name = F.name, key = F.key, lines = F.lines, colors = F.colors, float = F.float,
                    buried = F.buried, missing = F.missing, samples = #F.pts, skipped = F.skipped,
                    gateFloat = F.gateFloat, gateBuried = F.gateBuried, at = now() }
            fitJob = nil
            requestDraw(name, a, true)
            return
        end
        local x, y, z = F.a.x + p[1] * c - p[2] * s, F.a.y + p[1] * s + p[2] * c, F.a.z + p[3]
        local over = false
        if F.run then
            local dx, dy = x - F.run.x, y - F.run.y
            over = insideFootprint(name, dx * rc - dy * rs, dx * rs + dy * rc)
        end
        if over then
            F.skipped = F.skipped + 1
        else
            local gz = Open77.world.groundZ(x, y, z + 25.0)
            local L, C = F.lines, F.colors
            if not gz then
                F.missing = F.missing + 1
                L[#L + 1], L[#L + 2], L[#L + 3], L[#L + 4], L[#L + 5], L[#L + 6] = x, y, z, x, y, z - 6.0
                C[#C + 1] = FLOAT_COLOR
            elseif gz < z - FIT_TOLERANCE then
                F.float = math.max(F.float, z - gz)
                if p[4] then F.gateFloat = math.max(F.gateFloat, z - gz) end
                L[#L + 1], L[#L + 2], L[#L + 3], L[#L + 4], L[#L + 5], L[#L + 6] = x, y, gz, x, y, z
                C[#C + 1] = FLOAT_COLOR
            elseif gz > z + FIT_TOLERANCE then
                F.buried = math.max(F.buried, gz - z)
                if p[4] then F.gateBuried = math.max(F.gateBuried, gz - z) end
                L[#L + 1], L[#L + 2], L[#L + 3], L[#L + 4], L[#L + 5], L[#L + 6] = x, y, z, x, y, gz
                C[#C + 1] = BURIED_COLOR
            end
        end
    end
end

-- ------------------------------------------------------------------ the page
-- what stands for the map's ground on screen: its outline (kind 4), or the box (an older build's
-- ghost, or one still being read)
local function outlineKind(name)
    local G = ghostOf(name)
    if not G.ready then return "loading" end
    return (G.kinds[4] and #G.kinds[4] > 0) and "outline" or "box"
end

local function sendEditorState()
    if not page then return end
    if not edit then
        page:send("editor:state", { active = false })
        return
    end
    local m = server.maps[edit.map]
    page:send("editor:state", {
        active = true, map = edit.map, display = display(edit.map), anchor = edit.anchor,
        running = m and m.running or nil, aim = edit.aim, speed = edit.speedLabel or "normal",
        keys = edit.keys, wire = wire.state, wireWhy = wire.why, outline = outlineKind(edit.map),
        fit = (fit and fit.name == edit.map and fit.key == spotKey(edit.anchor)) and
            { float = fit.float, buried = fit.buried, missing = fit.missing, samples = fit.samples,
              skipped = fit.skipped, gateFloat = fit.gateFloat, gateBuried = fit.gateBuried, here = fit.here,
              far = fit.far and math.floor(fit.far + 0.5) or nil } or nil,
    })
end

local function keyList()
    local out = {}
    local all = Open77.input and Open77.input.mappings and Open77.input.mappings() or {}
    for _, mp in ipairs(all) do
        if mp.resource == RES then
            out[#out + 1] = { id = mp.id, name = mp.name, key = mp.key, defaultKey = mp.defaultKey,
                              rebound = mp.rebound, hold = mp.hold }
        end
    end
    -- the key that opens the manager first, then the editor's keys by name
    table.sort(out, function(x, y)
        if (x.id == "open_manager") ~= (y.id == "open_manager") then return x.id == "open_manager" end
        return tostring(x.name) < tostring(y.name)
    end)
    return out
end

local function keyFor(id)
    return (Open77.input and Open77.input.keyFor and Open77.input.keyFor(id)) or "?"
end

-- the server's picture to the page; when the page cannot take it yet (or the send fails) the frame
-- loop sends it again, so the panel never stays empty
-- a map as the page gets it: the last moves only (the whole history is the server's), no build log
local PAGE_HISTORY = 12
local function pageMap(m, slim)
    local out = {}
    for k, v in pairs(m) do out[k] = v end
    local h = m.history
    if type(h) == "table" and #h > (slim and 3 or PAGE_HISTORY) then
        local keep = {}
        for i = 1, slim and 3 or PAGE_HISTORY do keep[i] = h[i] end
        out.history = keep
    end
    if type(m.bake) == "table" then
        out.bake = { id = m.bake.id, state = m.bake.state, message = m.bake.message,
                     log = (not slim and type(m.bake.log) == "string") and m.bake.log:sub(-400) or nil }
    end
    if type(m.apply) == "table" and m.apply.package ~= nil then
        local a = {}
        for k, v in pairs(m.apply) do if k ~= "package" then a[k] = v end end
        out.apply = a
    end
    return out
end

-- one map to the page; too much for the page bridge: the short version of it
local function sendPageMap(m)
    if not (page and pageReady) or type(m) ~= "table" then return false end
    local ok, why = page:send("maps:state", pageMap(m, link.slim))
    if ok == false then
        say("page-map:" .. tostring(m.name) .. ":" .. tostring(why), "the panel refused %s (%s): sending it shorter",
            tostring(m.name), tostring(why))
        ok, why = page:send("maps:state", pageMap(m, true))
        if ok == false then
            say("page-map-slim:" .. tostring(m.name) .. ":" .. tostring(why), "the panel refused %s shortened too (%s)",
                tostring(m.name), tostring(why))
        end
    end
    return ok ~= false
end

local function sendInit()
    needInit = true
    if not (page and pageReady) then return end
    local list = {}
    for _, m in pairs(server.maps) do list[#list + 1] = m end
    table.sort(list, function(x, y) return tostring(x.display or x.name):lower() < tostring(y.display or y.name):lower() end)
    local function payload(maps, keys)
        local out = {}
        for i, m in ipairs(maps) do out[i] = pageMap(m, link.slim) end
        return { maps = out, rights = server.rights, config = server.config, keys = keys and keyList() or nil,
                 menuKey = keyFor("open_manager"), baker = server.baker, loading = server.loading,
                 relaunch = (canRelaunch()), stale = stale, boot = server.boot,
                 answered = link.answered,
                 noAnswer = (not link.answered and link.since ~= nil and now() - link.since > 10) or nil }
    end
    local ok, why = page:send("maps:init", payload(list, true))
    if ok == false then
        -- too much for the page bridge in one go: the header, the keys, then one map at a time
        say("init-full:" .. tostring(why), "the panel refused the maps in one message (%s): sending them one by one",
            tostring(why))
        ok, why = page:send("maps:init", payload({}, false))
        if ok == false then
            say("init-head:" .. tostring(why), "the panel refused the header too (%s)", tostring(why))
            return                     -- needInit stays: the frame loop tries again
        end
        page:send("maps:keys", { keys = keyList(), menuKey = keyFor("open_manager") })
        for _, m in ipairs(list) do sendPageMap(m) end
    end
    local n = #list
    say("init:" .. n .. ":" .. tostring(link.answered) .. ":" .. tostring(server.loading),
        "panel has %d map(s)%s", n, link.answered and (server.loading and " (the server is still loading)" or "")
            or " (no answer from the server yet)")
    needInit = false
end

local function setPanel(open)
    if not page then return end
    if open == panelOpen then return end
    panelOpen = open
    if open then
        -- the person takes their view back from an agent's camera
        if agent.view and agent.release then agent.release("the panel was opened") end
        page:setFocus(true, true)
        sendInit()
        page:send("maps:open", {})
        local n = 0
        for _ in pairs(server.maps) do n = n + 1 end
        say("open:" .. n .. ":" .. tostring(pageReady) .. ":" .. tostring(link.answered),
            "panel opened: %d map(s) known, page %s, server %s", n, pageReady and "ready" or "NOT ready",
            link.answered and "answered" or "has NOT answered")
        -- no maps known here (their message came before this script listened): ask again
        if next(server.maps) == nil then
            link.helloAt, link.hellos = now(), link.hellos + 1
            TriggerServerEvent(EV .. "hello", { asked = link.hellos, slim = link.hellos >= 3 or nil })
        end
    else
        page:send("maps:close", {})
        page:setFocus(false, false)
        if preview and not edit then preview = nil; clearDrawing() end
    end
end

local function startEdit(name)
    if agent.view and agent.release then agent.release("editing started") end
    local m = server.maps[name]
    if not m then return notify("unknown map " .. tostring(name)) end
    if not server.rights[name] then return notify("You may not move " .. display(name) .. ".") end
    local a = m.pending or m.running
    if not a then
        local x, y, z = Open77.character.position()
        local view = Open77.camera.view and Open77.camera.view()
        local f = view and view.forward
        if f and f.x then
            local h = math.sqrt(f.x * f.x + f.y * f.y)
            if h > 1e-3 then x, y = x + f.x / h * 40.0, y + f.y / h * 40.0 end
            local gz = Open77.world.groundZ(x, y, z + 60.0)
            if gz then z = gz end
        end
        a = { x = x, y = y, z = z, yaw = 0 }
    end
    setPanel(false)
    held = {}
    edit = { map = name, anchor = { x = a.x, y = a.y, z = a.z, yaw = a.yaw or 0 }, aim = false, dirty = true,
             lastDraw = 0, lastHud = 0,
             keys = { move = keyFor("forward") .. "/" .. keyFor("back") .. "/" .. keyFor("left") .. "/" .. keyFor("right"),
                      height = keyFor("raise") .. "/" .. keyFor("lower"),
                      rotate = keyFor("rotate_left") .. "/" .. keyFor("rotate_right"),
                      fast = keyFor("fast"), fine = keyFor("fine"), aim = keyFor("aim"), ground = keyFor("ground"),
                      save = keyFor("save"), cancel = keyFor("cancel"), menu = keyFor("open_manager") } }
    wire.state, wire.why = ghostOf(name).ready and "full" or "loading", nil
    -- a new session tries full detail again, and says so again when it cannot
    reduced[name], wire.told.quota = nil, nil
    -- and checks the ground afresh (streamed-in terrain may differ from the last look)
    fit, fitJob = nil, nil
    requestDraw(name, edit.anchor, true)
    sendEditorState()
end

local function stopEdit(msg, reopen)
    if not edit then return end
    local name = edit.map
    edit = nil
    held = {}
    clearDrawing()
    sendEditorState()
    if msg then notify(msg) end
    if reopen then
        setPanel(true)
        if page then page:send("maps:select", { map = name }) end
    end
end

local function serverOp(map, op, args, pageRequest)
    opSeq = opSeq + 1
    local token = opSeq
    pendingOps[token] = pageRequest or true
    TriggerServerEvent(EV .. "op", { map = map, op = op, args = args or {}, token = token })
    return token
end

local function createPage()
    local create = (Open77.webui and Open77.webui.create) or (WebUI and WebUI.create)
    if not create then return print("[" .. RES .. "] WebUI unavailable: the manager panel is off (use /maps)") end
    local why
    page, why = create({ entry = "web/index.html", layer = "menu", transparent = true, visible = true,
                         width = 1920, height = 1080 })
    if not page then return print("[" .. RES .. "] WebUI failed: " .. tostring(why)) end
    say(nil, "panel page created")
    -- the page is built asynchronously and a send before it is ready is dropped: the first state
    -- goes out on the runtime's own ready event (Open77.ready() in the page), and again on the
    -- page's maps:ready, whichever the runtime delivers first.  A page that loads faster than this
    -- script registers its handlers (a reload, a cached page) sends both too early: the frame loop
    -- knocks with maps:ping until the page answers with maps:ready.
    local function ready(p)
        if not pageReady then say("page-ready", "panel page is ready") end
        pageReady = true
        sendInit()
        sendEditorState()
        if panelOpen then page:send("maps:open", {}) end
        if type(p) == "table" and p.again and next(server.maps) == nil and now() - link.helloAt > 1.0 then
            link.helloAt, link.hellos = now(), link.hellos + 1
            TriggerServerEvent(EV .. "hello", { asked = link.hellos, slim = link.hellos >= 3 or nil })
        end
    end
    page:on("ui:ready", ready)
    page:on("maps:ready", ready)
    -- what the page has to say (its errors above all) goes to the game's log, each line once
    page:on("maps:log", function(p)
        if type(p) ~= "table" then return end
        local text = tostring(p.text or ""):sub(1, 300)
        say("page:" .. text, "panel %s: %s", tostring(p.level or "info"), text)
    end)
    page:on("maps:close", function() setPanel(false) end)
    page:on("maps:relaunch", function() relaunch() end)
    page:on("maps:edit", function(p) if p and p.map then startEdit(p.map) end end)
    page:on("maps:preview", function(p)
        if not p or not p.map then return end
        if p.on then
            preview = p.map
            local m = server.maps[p.map]
            if m then requestDraw(p.map, m.pending or m.running, true) end
        else
            preview = nil
            clearDrawing()
        end
    end)
    page:on("maps:op", function(p)
        if type(p) ~= "table" or not p.map or not p.op then return end
        serverOp(p.map, p.op, p.args, { request = p.request })
    end)
    page:on("maps:keys:rebind", function(p)
        if type(p) ~= "table" or not p.id or not p.key then return end
        local key, why2 = Open77.input.rebind(RES, p.id, tostring(p.key))
        page:send("maps:keys", { keys = keyList(), menuKey = keyFor("open_manager"),
                                 error = (not key) and tostring(why2) or nil })
    end)
    page:on("maps:keys:reset", function(p)
        if type(p) ~= "table" or not p.id then return end
        Open77.input.reset(RES, p.id)
        page:send("maps:keys", { keys = keyList(), menuKey = keyFor("open_manager") })
    end)
end

-- ------------------------------------------------------------------ server events
local toldMoved = {}

-- a map that moved at the server's last restart: the people who manage it are told how to see it
local function tellMoved(name, m)
    local ap = m.apply
    if server.rights[name] and not toldMoved[name] and type(ap) == "table" and ap.state == "applied" and
        server.boot and tonumber(ap.appliedAt) and tonumber(ap.appliedAt) >= server.boot - 5 then
        toldMoved[name] = true
        notify(("%s moved at the server's last restart. If you see it at its old spot, your game started "
            .. "before the server was back: restart the game from the Open77 launcher."):format(display(name)))
    end
end

-- the server's picture: a short header (names, rights, baker, settings), then each map in a message
-- of its own (`state`).  An older server sends every map inside the header instead.
RegisterNetEvent(EV .. "init", function(p)
    if type(p) ~= "table" then return end
    server.rights = p.rights or {}
    server.config = p.config or {}
    server.baker = p.baker or server.baker
    server.loading = p.loading == true
    server.boot = type(p.server) == "table" and tonumber(p.server.boot) or server.boot
    local n = 0
    if type(p.names) == "table" then
        -- the maps follow: keep what we know of those still there until their message replaces it
        local keep = {}
        for _, name in ipairs(p.names) do
            if type(name) == "string" then keep[name] = server.maps[name]; n = n + 1 end
        end
        server.maps = keep
        link.names, link.headerAt, link.missingAsks = p.names, now(), 0
    else
        server.maps = {}
        for _, m in ipairs(p.maps or {}) do
            if type(m) == "table" and m.name then server.maps[m.name] = m; n = n + 1 end
        end
        link.names, link.headerAt = nil, nil
    end
    link.answered, link.loading, link.slim = true, server.loading, p.slim == true
    TriggerServerEvent(EV .. "ack", { maps = n })
    say("answer:" .. n .. ":" .. tostring(server.loading) .. ":" .. tostring(link.slim),
        "the server answered: %d map(s)%s%s", n, server.loading and ", still loading" or "",
        link.slim and " (the slim picture: this game missed the full one)" or "")
    sendInit()
    for name, m in pairs(server.maps) do tellMoved(name, m) end
end)

RegisterNetEvent(EV .. "state", function(m)
    if type(m) ~= "table" or not m.name then return end
    server.maps[m.name] = m
    if page and pageReady then sendPageMap(m) end
    tellMoved(m.name, m)
    if preview == m.name and not edit and (m.pending or m.running) then requestDraw(m.name, m.pending or m.running, true) end
end)

RegisterNetEvent(EV .. "baker", function(b)
    if type(b) ~= "table" then return end
    server.baker = b
    if page and pageReady then page:send("maps:baker", b) end
end)

RegisterNetEvent(EV .. "open", function() setPanel(true) end)

RegisterNetEvent(EV .. "denied", function(p)
    notify(("You need the ACL right %s (or command.mlo.<map>) to manage maps."):format(tostring(p and p.right)))
end)

RegisterNetEvent(EV .. "toast", function(p)
    if type(p) == "table" and p.text then notify(p.text) end
end)

RegisterNetEvent(EV .. "result", function(r)
    if type(r) ~= "table" then return end
    local req = pendingOps[r.token]
    pendingOps[r.token] = nil
    if type(req) == "table" and page then
        page:send("maps:result", { request = req.request, map = r.map, op = r.op, ok = r.ok, reason = r.reason,
                                   result = r.result })
    end
    if req == "editor" or (type(req) == "table" and req.editor) then
        if r.ok then
            stopEdit(("%s saved: %s -- Apply it in the map manager to move the map."):format(display(r.map),
                     describe(r.result or (server.maps[r.map] or {}).pending or { x = 0, y = 0, z = 0, yaw = 0 })), true)
        else
            notify("Not saved: " .. tostring(r.reason))
        end
    elseif not panelOpen and not r.ok then
        notify(("%s: %s failed (%s)"):format(display(r.map), tostring(r.op), tostring(r.reason)))
    end
end)

-- ------------------------------------------------------------------ key bindings (all rebindable)
local function bind(id, label, key, onPress, holdable)
    if holdable then
        RegisterKeyMapping(id, "Maps: " .. label, key, function() held[id] = true end, function() held[id] = false end)
    else
        RegisterKeyMapping(id, "Maps: " .. label, key, onPress)
    end
end

bind("open_manager", "open the map manager", CFG.menuKey or "F9", function()
    if edit then return stopEdit("editing cancelled", true) end
    if panelOpen then return setPanel(false) end
    TriggerServerEvent(EV .. "open")
end)
bind("forward", "editor: move forward", KEYS.forward or "UP", nil, true)
bind("back", "editor: move back", KEYS.back or "DOWN", nil, true)
bind("left", "editor: move left", KEYS.left or "LEFT", nil, true)
bind("right", "editor: move right", KEYS.right or "RIGHT", nil, true)
bind("raise", "editor: raise", KEYS.raise or "PAGEUP", nil, true)
bind("lower", "editor: lower", KEYS.lower or "PAGEDOWN", nil, true)
bind("rotate_left", "editor: rotate left", KEYS.rotateLeft or "HOME", nil, true)
bind("rotate_right", "editor: rotate right", KEYS.rotateRight or "END", nil, true)
bind("fast", "editor: hold for speed", KEYS.fast or "SHIFT", nil, true)
bind("fine", "editor: hold for fine steps", KEYS.fine or "CTRL", nil, true)
bind("aim", "editor: follow the crosshair", KEYS.aim or "INSERT", function()
    if not edit then return end
    edit.aim = not edit.aim
    edit.dirty = true
    notify(edit.aim and "following the crosshair" or "manual placement")
end)
bind("ground", "editor: drop to the ground", KEYS.ground or "DELETE", function()
    if not edit then return end
    local a = edit.anchor
    local gz = Open77.world.groundZ(a.x, a.y, a.z + 60.0)
    if gz then
        a.z = gz
        edit.dirty = true
    else
        notify("no ground found under the anchor")
    end
end)
bind("save", "editor: save the spot", KEYS.save or "ENTER", function()
    if not edit then return end
    serverOp(edit.map, "place", edit.anchor, { editor = true })
end)
bind("cancel", "editor: cancel", KEYS.cancel or "BACKSPACE", function()
    if edit then stopEdit("editing cancelled", true) end
end)

AddEventHandler("open77:keybinds:changed", function()
    if page and pageReady then page:send("maps:keys", { keys = keyList(), menuKey = keyFor("open_manager") }) end
end)

-- Escape is swallowed by the plugin and raised as open77:pauseKey: close the panel / the editor
AddEventHandler("open77:pauseKey", function()
    if panelOpen then setPanel(false) end
end)

-- ------------------------------------------------------------------ per-frame editing
local function facing()
    local view = Open77.camera.view and Open77.camera.view()
    local f = view and view.forward
    if f and f.x then
        local n = math.sqrt(f.x * f.x + f.y * f.y)
        if n > 1e-4 then return f.x / n, f.y / n end
    end
    return 0.0, 1.0
end

-- ------------------------------------------------------------------ is the map really where the server says?
-- The game mounts its archives when it starts: a player whose game started before the server
-- installed a map's new package (started too early after the restart, or never restarted) still has
-- the old files, and the map is not at its new spot for them.  Near the map, rays straight down from
-- above its roofs tell: where the map is, they land on the map itself (its floors, walls, roofs: at or
-- above its ground); where it is missing they land below its ground on the world's, or on nothing
-- (terrain is invisible to these rays).  Only clear answers count: a look where most rays found the
-- map says it is there (and takes back an earlier warning); three looks in a row where none did,
-- from close by, say it is missing.  A map flush on the world's ground cannot be told apart: then
-- nothing is said.  (Rays from just above its outline used to land on its own walls and hedges and
-- call a map that was there missing.)
local PRESENCE_RANGE = 70.0            -- metres from the map's anchor: its collision streams in around you
local PRESENCE_SETTLE = 10.0           -- seconds in range before looking (its sector streams in)
local PRESENCE_INTERVAL = 6.0          -- seconds between looks
local PRESENCE_RECHECK = 20.0          -- seconds between looks once it was called missing
local PRESENCE_PROBES = 16
local PRESENCE_INSET = 8.0             -- metres inside the outline: past the walls, hedges and planters on it
local PRESENCE_ABOVE = 40.0            -- the rays start this high above the map's ground (over its roofs)
local presence = {}                    -- name -> { key, inSince, misses, state, next, probes, look }

-- points well inside the outline, in the map's frame, with the height of the outline nearest to them
local function presenceProbes(name)
    local pts = fitPointsOf(name)
    if not pts then return nil end
    local cx, cy, n = 0, 0, 0
    for _, p in ipairs(pts) do
        if not p[4] then cx, cy, n = cx + p[1], cy + p[2], n + 1 end
    end
    if n < 4 then return {} end
    cx, cy = cx / n, cy / n
    local out = {}
    local step = math.max(1, math.floor(n / PRESENCE_PROBES))
    for i = 1, #pts, step do
        local p = pts[i]
        if not p[4] then
            local dx, dy = cx - p[1], cy - p[2]
            local d = math.sqrt(dx * dx + dy * dy)
            if d > 3 then
                local inset = math.min(PRESENCE_INSET, d * 0.5)
                local x, y = p[1] + dx / d * inset, p[2] + dy / d * inset
                if insideFootprint(name, x, y) then out[#out + 1] = { x, y, p[3] } end
            end
        end
    end
    return out
end

local function staleMap(name)
    stale[name] = true
    local m = server.maps[name]
    TriggerServerEvent(EV .. "presence", { map = name, state = "missing", runningId = m and m.runningId })
    local text = ("%s is not at its new spot in your game: the game started before the server installed the "
        .. "map's new files. Restart the game from the Open77 launcher (F9 > Restart the game)."):format(display(name))
    if page and pageReady then page:send("maps:stale", { map = name, relaunch = (canRelaunch()) }) end
    notify(text)
    say(nil, "%s: not at its new spot in this game (old files)", name)
end

local function presentMap(name)
    if not stale[name] then return end
    stale[name] = nil
    local m = server.maps[name]
    TriggerServerEvent(EV .. "presence", { map = name, state = "present", runningId = m and m.runningId })
    if page and pageReady then page:send("maps:stale", { map = name, present = true }) end
    notify(("%s is at its spot in your game."):format(display(name)))
    say(nil, "%s: at its spot in this game after all", name)
end

local function presenceStep(t)
    if edit or not (Open77.world and Open77.world.groundZ) then return end
    local px, py = Open77.character.position()
    if not px then return end
    for name, m in pairs(server.maps) do
        local a = m.running
        if a and DEFS[name] then
            local key = spotKey(a)
            local P = presence[name]
            if not P or P.key ~= key then
                if P and stale[name] then presentMap(name) end      -- a new spot: an old verdict is void
                P = { key = key, misses = 0, state = "unknown", next = 0 }
                presence[name] = P
            end
            if math.sqrt((px - a.x) ^ 2 + (py - a.y) ^ 2) > PRESENCE_RANGE then
                P.inSince, P.look = nil, nil
                P.misses = 0                       -- only looks from close by, in a row, count
            elseif P.state == "unknown" or P.state == "missing" then
                P.inSince = P.inSince or t
                if t - P.inSince > PRESENCE_SETTLE and t >= P.next then
                    local G = ghostOf(name)
                    if not G.ready then parseStep(G, PARSE_SLICE) return end
                    P.probes = P.probes or presenceProbes(name)
                    if not P.probes or #P.probes < 4 then P.state = "unknown_shape" return end
                    -- a few rays per frame
                    local L = P.look or { i = 0, map = 0, below = 0, none = 0 }
                    P.look = L
                    local r = math.rad(a.yaw or 0)
                    local c, s = math.cos(r), math.sin(r)
                    for _ = 1, 3 do
                        local q = P.probes[L.i + 1]
                        if not q then break end
                        local x, y, ez = a.x + q[1] * c - q[2] * s, a.y + q[1] * s + q[2] * c, a.z + q[3]
                        local gz = Open77.world.groundZ(x, y, ez + PRESENCE_ABOVE)
                        if gz == nil then
                            L.none = L.none + 1
                        elseif gz >= ez - 0.6 then
                            L.map = L.map + 1            -- its floor, a wall, a roof: the map is here
                        elseif gz < ez - 1.5 then
                            L.below = L.below + 1        -- the world's ground under where the map should be
                        end
                        L.i = L.i + 1
                    end
                    if L.i >= #P.probes then
                        local n = #P.probes
                        P.look = nil
                        if L.map >= math.max(2, n * 0.3) then
                            P.state, P.misses, P.next = "present", 0, t + PRESENCE_INTERVAL
                            presentMap(name)
                        elseif L.map == 0 and L.below + L.none >= n * 0.8 then
                            P.misses = P.misses + 1
                            P.next = t + (P.state == "missing" and PRESENCE_RECHECK or PRESENCE_INTERVAL)
                            if P.misses >= 3 and P.state ~= "missing" then
                                P.state = "missing"
                                staleMap(name)
                            end
                        else
                            P.next = t + PRESENCE_INTERVAL          -- unclear: look again
                        end
                    end
                end
            end
            return                     -- one map per frame
        end
    end
end

-- ------------------------------------------------------------------ roads (Night City's traffic lanes)
-- The server has the city's lanes (data/roads, from the game's own traffic data); while a map is
-- moved, and when an agent asks, the lanes around it are drawn: car lanes bright (their centre line,
-- then their edges when there is room), pavements and crossings faint.  Two debugDraw tags of their
-- own, refreshed before their TTL, and their lines come off the map drawing's share of the budget.
local ROAD_TAG = TAG .. "road"
local ROAD_COLORS = { r = "#F4F4F4E6", h = "#FF9F1AF0", i = "#FFF27ACC", c = "#5AD1FFC0", p = "#9AA5B190",
                      j = "#9AA5B170", t = "#C084FC90", o = "#9AA5B160" }
local ROAD_CARS = { r = true, h = true, i = true }

-- ask the server for the lanes around (x, y): at most once a second, and only when it moved away
local function askRoads(x, y, r, force)
    local t = now()
    if not force and roads.center and math.sqrt((x - roads.center.x) ^ 2 + (y - roads.center.y) ^ 2) < 60
        and roads.lanes then return end
    if t - roads.asked < 1.0 then return end
    roads.asked = t
    roads.want = ("%d:%.0f:%.0f"):format(math.floor(t * 1000), x, y)
    roads.parts = {}
    roads.pendingCenter = { x = x, y = y, r = r }
    TriggerServerEvent(EV .. "roads", { key = roads.want, x = x, y = y, r = r })
end

local function parseLane(text)
    local id, kind, width, rest = tostring(text):match("^(%d+) (%a) (%S+) (.+)$")
    if not id then return nil end
    local pts, n = {}, 0
    for v in rest:gmatch("%S+") do n = n + 1; pts[n] = tonumber(v) or 0 end
    if n < 6 or n % 3 ~= 0 then return nil end
    return { id = tonumber(id), kind = kind, width = tonumber(width) or 0, pts = pts }
end

RegisterNetEvent(EV .. "agent:roads", function(p)
    if type(p) ~= "table" or p.key ~= roads.want then return end
    if p.missing then
        roads.missing, roads.lanes, roads.want = true, nil, nil
        say("roads-missing", "the server has no road data (install it with the manager update's --roads)")
        if agent.roads and agent.roads.id then
            agentPrint({ id = agent.roads.id, ok = false, error = "no_road_data_on_the_server" })
            agent.roads.id = nil
        end
        return
    end
    roads.parts[tonumber(p.part) or 1] = p.lanes or {}
    local have = 0
    for _ in pairs(roads.parts) do have = have + 1 end
    if have < (tonumber(p.parts) or 1) then return end
    -- read in the frame loop, a slice at a time (a part can hold a few hundred lanes)
    local raw = {}
    for i = 1, tonumber(p.parts) or 1 do
        for _, text in ipairs(roads.parts[i] or {}) do raw[#raw + 1] = text end
    end
    roads.raw, roads.rawAt, roads.parsed = raw, 1, {}
    roads.key, roads.want, roads.missing, roads.parts = p.key, nil, false, {}
end)

-- the lanes that arrived, read a slice per frame; the agent that asked is answered once all are in
local function parseRoadsStep()
    local raw = roads.raw
    local n = 0
    while roads.rawAt <= #raw do
        local lane = parseLane(raw[roads.rawAt])
        roads.rawAt = roads.rawAt + 1
        if lane then roads.parsed[#roads.parsed + 1] = lane end
        n = n + 1
        if n >= 40 or (n % 8 == 0 and not timeLeft()) then return end
    end
    roads.lanes, roads.raw, roads.parsed = roads.parsed, nil, nil
    roads.center, roads.dirty, roads.batches = roads.pendingCenter, true, nil
    if agent.roads and agent.roads.id then
        local kinds = {}
        for _, ln in ipairs(roads.lanes) do kinds[ln.kind] = (kinds[ln.kind] or 0) + 1 end
        agentPrint({ id = agent.roads.id, ok = true, lanes = #roads.lanes, kinds = kinds, center = roads.center })
        agent.roads.id = nil
    end
end

local function clearRoads()
    for n = 1, roads.tags do Open77.debugDraw.clear(ROAD_TAG .. n) end
    roads.tags, roads.lines, roads.pending = 0, 0, nil
end

-- the road lines, built a slice at a time (a city block has hundreds of lane segments): car lanes
-- first (centre lines, then their edges), then the rest, up to ROAD_MAX
local function buildStep(B, budget)
    local lanes = roads.lanes or {}
    local done = 0
    while B.lane <= #lanes do
        local ln = lanes[B.lane]
        local P = ln.pts
        local color = ROAD_COLORS[ln.kind] or ROAD_COLORS.o
        while B.i <= #P - 3 do
            local i = B.i
            B.i = i + 3
            local x1, y1, z1, x2, y2, z2 = P[i], P[i + 1], P[i + 2] + 0.15, P[i + 3], P[i + 4], P[i + 5] + 0.15
            if ROAD_CARS[ln.kind] then
                B.car[#B.car + 1] = { a = { x = x1, y = y1, z = z1 }, b = { x = x2, y = y2, z = z2 }, color = color }
                local dx, dy = x2 - x1, y2 - y1
                local d = math.sqrt(dx * dx + dy * dy)
                if d > 1e-3 then
                    local ox, oy = -dy / d * ln.width / 2, dx / d * ln.width / 2
                    B.edges[#B.edges + 1] = { a = { x = x1 + ox, y = y1 + oy, z = z1 }, b = { x = x2 + ox, y = y2 + oy, z = z2 },
                                              color = "#F4F4F466" }
                    B.edges[#B.edges + 1] = { a = { x = x1 - ox, y = y1 - oy, z = z1 }, b = { x = x2 - ox, y = y2 - oy, z = z2 },
                                              color = "#F4F4F466" }
                end
            else
                B.walk[#B.walk + 1] = { a = { x = x1, y = y1, z = z1 }, b = { x = x2, y = y2, z = z2 }, color = color }
            end
            done = done + 1
            if done >= budget or (done % 16 == 0 and not timeLeft()) then return false end
        end
        B.lane, B.i = B.lane + 1, 1
    end
    return true
end

-- one debugDraw.set per frame at most; redrawn before the TTL runs out
local function roadsStep(t)
    if roads.raw then return parseRoadsStep() end
    if not roads.shown then
        if roads.tags > 0 or roads.pending then clearRoads() end
        roads.build = nil
        return
    end
    if not roads.lanes then return end
    if not roads.dirty and t - roads.drawnAt < REFRESH and not roads.build and not roads.pending then return end
    if not timeLeft() then return end              -- drawing waits for a frame with time left; clearing never does
    if not roads.pending and not roads.dirty and not roads.build and roads.batches then
        roads.pending, roads.pendingAt = roads.batches, 1      -- a refresh: the same lines again
    end
    if not roads.pending then
        if roads.dirty or not roads.build then
            roads.build = { lane = 1, i = 1, car = {}, walk = {}, edges = {} }
            roads.dirty = false
        end
        if not buildStep(roads.build, 200) then return end
        local B = roads.build
        roads.build = nil
        local all = {}
        for _, list in ipairs({ B.car, B.edges, B.walk }) do
            for _, l in ipairs(list) do
                if #all >= ROAD_MAX then break end
                all[#all + 1] = l
            end
        end
        roads.pending, roads.pendingAt = {}, 1
        for i = 1, #all, batchCap do
            local b = {}
            for k = i, math.min(#all, i + batchCap - 1) do b[#b + 1] = all[k] end
            roads.pending[#roads.pending + 1] = b
        end
        roads.lines = #all
        roads.batches = roads.pending
        return
    end
    local n = roads.pendingAt
    local batch = roads.pending[n]
    if batch then
        local ok, why = Open77.debugDraw.set(ROAD_TAG .. n, { lines = batch, ttl = TTL, maxDistance = MAX_DISTANCE,
                                                              thickness = n == 1 and 2.0 or 1.5 })
        if not ok then
            say("roads-refused:" .. tostring(why), "road lines refused: %s", tostring(why))
            roads.pending, roads.dirty, roads.drawnAt = nil, false, t
            return
        end
        roads.pendingAt = n + 1
        roads.tags = math.max(roads.tags, n)       -- on screen now: a clear must know it is there
        return
    end
    for k = #roads.pending + 1, roads.tags do Open77.debugDraw.clear(ROAD_TAG .. k) end
    roads.tags = #roads.pending
    roads.pending, roads.drawnAt = nil, t
end

-- ------------------------------------------------------------------ the agent
-- An agent (Claude through the map agent on this computer, or any tool speaking `maps @<id> ...`)
-- works through the server's maps command; the server checks its rights and hands the parts that
-- happen in this game to us: a camera over the map, the wireframe, the roads, the ground check, a
-- few ground probes, and what it says.  Every answer is one `@agent {json}` line in the game's log.
local AGENT_VIEW_SECONDS = 120         -- an agent's camera gives the view back after this, at the latest

local function mapCenter(name, a)
    local b = (DEFS[name] or {}).bounds
    local cx, cy, half = 0, 0, 60
    if b and b.min and b.max then
        cx, cy = (b.min[1] + b.max[1]) / 2, (b.min[2] + b.max[2]) / 2
        half = math.max(b.max[1] - b.min[1], b.max[2] - b.min[2]) / 2
    end
    local r = math.rad(a.yaw or 0)
    return a.x + cx * math.cos(r) - cy * math.sin(r), a.y + cx * math.sin(r) + cy * math.cos(r), half
end

local function releaseView(reason)
    local v = agent.view
    agent.view = nil
    if not v then return end
    if Open77.camera.deactivate then Open77.camera.deactivate({ blendMs = 0 }) end
    if v.cam and Open77.camera.destroy then Open77.camera.destroy(v.cam) end
    if page and pageReady then page:send("maps:agent", { view = false }) end
    if reason then say(nil, "agent camera released (%s)", reason) end
end

-- the gate `n` (1-based) of a map: its threshold's middle and the way out, in the map's frame
local function gateOf(name, n)
    local G = ghostOf(name)
    local g = G.ready and G.kinds[5]
    if not g or #g < 24 * n then return nil end
    local i = (n - 1) * 24 + 1
    local mx, my, mz = (g[i] + g[i + 3]) / 2, (g[i + 1] + g[i + 4]) / 2, (g[i + 2] + g[i + 5]) / 2
    local tx, ty = g[i + 3] - g[i], g[i + 4] - g[i + 1]
    local d = math.sqrt(tx * tx + ty * ty)
    if d < 1e-3 then return nil end
    local nx, ny = -ty / d, tx / d
    local b = (DEFS[name] or {}).bounds
    local cx, cy = 0, 0
    if b then cx, cy = (b.min[1] + b.max[1]) / 2, (b.min[2] + b.max[2]) / 2 end
    if (mx - cx) * nx + (my - cy) * ny < 0 then nx, ny = -nx, -ny end   -- pointing away from the middle
    return mx, my, mz, nx, ny, d
end

agent.release = releaseView

-- the agent's camera at `pos` looking at `look` (it replaces an earlier one); false, reason when refused
local function agentCamera(pos, look, fov, seconds, mode, name, label)
    local px, py, pz = Open77.character.position()
    if px then
        local d = math.sqrt((pos.x - px) ^ 2 + (pos.y - py) ^ 2 + (pos.z - pz) ^ 2)
        if d > 245 then return false, "camera_too_far_from_player" end
    end
    releaseView(nil)
    local cam, why = Open77.camera.create({ position = pos, lookAt = look, fov = fov })
    if not cam then return false, why end
    local ok, why2 = Open77.camera.activate(cam, { blendMs = 0 })
    if not ok then
        if Open77.camera.destroy then Open77.camera.destroy(cam) end
        return false, why2
    end
    agent.view = { cam = cam, map = name, mode = mode, untilT = now() + seconds }
    if page and pageReady then page:send("maps:agent", { view = true, mode = mode, map = label }) end
    say(nil, "agent camera: %s view of %s for %d s", mode, tostring(label), seconds)
    return true
end

local function agentView(req)
    local id, name, args = req.id, req.map, req.args or {}
    local mode = tostring(args.mode or "top")
    if mode == "off" then
        releaseView("asked")
        return agentPrint({ id = id, ok = true, view = "off" })
    end
    local m = server.maps[name]
    if not (m and DEFS[name]) then return agentPrint({ id = id, ok = false, error = "unknown_map" }) end
    local a = (args.which == "running" and m.running) or m.pending or m.running
    if not a then return agentPrint({ id = id, ok = false, error = "not_placed" }) end
    if not (Open77.camera.create and Open77.camera.activate) then
        return agentPrint({ id = id, ok = false, error = "scripted_cameras_unavailable" })
    end
    local wx, wy, half = mapCenter(name, a)
    local fov = math.max(20, math.min(100, tonumber(args.fov) or 60))
    local margin = math.max(0.3, math.min(3.0, tonumber(args.margin) or 1.15))
    local pos, look
    if mode == "top" then
        -- straight down the camera would take its heading from the player's body: a little south of
        -- the middle (7 degrees) it looks north, so north is up in the picture whatever the player faces
        local h = math.min(235, half * margin / math.tan(math.rad(fov / 2)))
        pos = { x = wx, y = wy - 0.12 * h, z = a.z + h }
        look = { x = wx, y = wy, z = a.z }
    elseif mode == "oblique" then
        local az = math.rad(tonumber(args.azimuth) or 200)
        local el = math.rad(math.max(10, math.min(80, tonumber(args.elevation) or 40)))
        local dist = math.min(240, half * margin / math.tan(math.rad(fov / 2)) * 1.1)
        pos = { x = wx - math.sin(az) * dist * math.cos(el), y = wy + math.cos(az) * dist * math.cos(el),
                z = a.z + dist * math.sin(el) }
        look = { x = wx, y = wy, z = a.z }
    elseif mode == "gate" then
        local gx, gy, gz, nx, ny = gateOf(name, math.max(1, math.floor(tonumber(args.gate) or 1)))
        if not gx then return agentPrint({ id = id, ok = false, error = "no_such_gate" }) end
        local r = math.rad(a.yaw or 0)
        local c, s = math.cos(r), math.sin(r)
        local px, py = a.x + gx * c - gy * s, a.y + gx * s + gy * c
        local ox, oy = nx * c - ny * s, nx * s + ny * c
        local dist = tonumber(args.distance) or 22
        pos = { x = px + ox * dist, y = py + oy * dist, z = a.z + gz + (tonumber(args.height) or 7) }
        look = { x = px, y = py, z = a.z + gz + 1.0 }
    elseif mode == "point" then
        pos = { x = tonumber(args.x) or wx, y = tonumber(args.y) or wy, z = tonumber(args.z) or (a.z + 50) }
        look = { x = tonumber(args.lx) or wx, y = tonumber(args.ly) or wy, z = tonumber(args.lz) or a.z }
    else
        return agentPrint({ id = id, ok = false, error = "unknown_view_mode", modes = "top oblique gate point off" })
    end
    local px, py, pz = Open77.character.position()
    if px then
        local d = math.sqrt((pos.x - px) ^ 2 + (pos.y - py) ^ 2 + (pos.z - pz) ^ 2)
        if d > 245 then
            return agentPrint({ id = id, ok = false, error = "camera_too_far_from_player", distance = math.floor(d),
                                hint = "maps goto <map> first: the camera must stay within 250 m of your body" })
        end
    end
    local seconds = math.max(5, math.min(600, tonumber(args.seconds) or AGENT_VIEW_SECONDS))
    local ok, why = agentCamera(pos, look, fov, seconds, mode, name, display(name))
    if not ok then return agentPrint({ id = id, ok = false, error = tostring(why) }) end
    agentPrint({ id = id, ok = true, view = mode, camera = pos, lookAt = look, fov = fov, anchor = a, seconds = seconds })
end

-- the ground check for an agent, at the map's new spot (or where it is): float/buried/missing
local function agentFitStart(req)
    local m = server.maps[req.map]
    if not (m and DEFS[req.map]) then return agentPrint({ id = req.id, ok = false, error = "unknown_map" }) end
    local args = req.args or {}
    local a = (args.which == "running" and m.running) or m.pending or m.running
    if not a then return agentPrint({ id = req.id, ok = false, error = "not_placed" }) end
    fit, fitJob = nil, nil
    agent.fit = { id = req.id, map = req.map, a = { x = a.x, y = a.y, z = a.z, yaw = a.yaw or 0 }, startT = now() }
end

local function agentFitStep(t)
    local F = agent.fit
    if not F then return end
    local G = ghostOf(F.map)
    if not G.ready then parseStep(G, PARSE_SLICE) return end
    fitStep(F.map, F.a, 4)
    if fit and fit.name == F.map and fit.key == spotKey(F.a) then
        agent.fit = nil
        agentPrint({ id = F.id, ok = true, map = F.map, anchor = F.a, float = fit.float, buried = fit.buried,
                     missing = fit.missing, samples = fit.samples, skipped = fit.skipped, gateFloat = fit.gateFloat,
                     gateBuried = fit.gateBuried, here = fit.here, far = fit.far and math.floor(fit.far) or nil })
    elseif t - F.startT > 10 then
        agent.fit = nil
        agentPrint({ id = F.id, ok = false, error = "ground_check_timed_out" })
    end
end

local function agentProbe(req)
    local out = {}
    for _, p in ipairs((req.args or {}).points or {}) do
        local x, y, z = tonumber(p[1] or p.x), tonumber(p[2] or p.y), tonumber(p[3] or p.z)
        if x and y and #out < 32 then
            -- z: where the ray starts (below a map's floor reaches the ground under it); default high up
            local gz = Open77.world.groundZ and Open77.world.groundZ(x, y, z or 900) or nil
            out[#out + 1] = { x = x, y = y, from = z, ground = gz }
        end
    end
    local px, py, pz = Open77.character.position()
    agentPrint({ id = req.id, ok = true, points = out, player = px and { x = px, y = py, z = pz } or nil })
end

-- A height map for an agent: rays from its camera through a grid of screen points.  The screen ray
-- sees terrain, which the Static-only ground probe (world.groundZ) does not: most of the hills and
-- the open ground of Night City are terrain.  With x y z the scan puts its own camera `h` metres
-- above that point first (looking down, north up) and gives the view back at the end.
local SCAN_PER_FRAME = 24

local function agentScanStart(req)
    local args = req.args or {}
    if not (Open77.camera and Open77.camera.screenRaycast) then
        return agentPrint({ id = req.id, ok = false, error = "screen_raycast_unavailable" })
    end
    local cols = math.max(2, math.min(64, math.floor(tonumber(args.cols) or 40)))
    local rows = math.max(2, math.min(48, math.floor(tonumber(args.rows) or 25)))
    local own = false
    local cx, cy, cz = tonumber(args.x), tonumber(args.y), tonumber(args.z)
    if cx and cy and cz then
        local h = math.max(20, math.min(235, tonumber(args.h) or 200))
        local fov = math.max(20, math.min(100, tonumber(args.fov) or 60))
        local ok, why = agentCamera({ x = cx, y = cy - 0.12 * h, z = cz + h }, { x = cx, y = cy, z = cz }, fov, 60,
                                    "scan", nil, ("%.0f %.0f"):format(cx, cy))
        if not ok then return agentPrint({ id = req.id, ok = false, error = tostring(why) }) end
        own = true
    elseif not agent.view then
        return agentPrint({ id = req.id, ok = false, error = "no_agent_view", hint = "maps scan x y z, or maps view <map> first" })
    end
    if agent.scan then agentPrint({ id = agent.scan.id, ok = false, error = "superseded" }) end
    local dyn = args.dynamic == true or args.dynamic == 1 or args.dynamic == "1" or args.dynamic == "true"
    agent.scan = { id = req.id, cols = cols, rows = rows, n = cols * rows, i = 0, slots = {}, mats = {}, matIndex = {},
                   errors = {}, failedRun = 0, tries = 0, filter = { static = true, dynamic = dyn },
                   misses = 0, margin = math.max(0, math.min(0.45, tonumber(args.margin) or 0.02)),
                   dist = math.max(10, math.min(1000, tonumber(args.distance) or 800)),
                   startT = now(), wait = math.max(0, math.min(10, tonumber(args.settle) or (own and 1.5 or 0.2))),
                   own = own and not args.keep, center = own and { x = cx, y = cy, z = cz } or nil }
end

-- Rays, then the answer's lines, then printing them: each step commits only whole work, so a frame
-- the host stops anywhere resumes where it was (a ray cast twice or a part printed twice is harmless).
local function agentScanStep(t)
    local S = agent.scan
    if not S then return end
    if S.i < S.n then
        if not agent.view then
            agent.scan = nil
            return agentPrint({ id = S.id, ok = false, error = "view_released" })
        end
        if t - S.startT < S.wait then return end
        local done = 0
        while S.i < S.n and done < SCAN_PER_FRAME do
            local k = S.i
            local c, r = k % S.cols, math.floor(k / S.cols)
            local u = S.margin + (1 - 2 * S.margin) * (c + 0.5) / S.cols
            local v = S.margin + (1 - 2 * S.margin) * (r + 0.5) / S.rows
            local hit, why = Open77.camera.screenRaycast(u, v, S.dist, S.filter)
            if hit == nil then
                -- the query could not run (the camera's state, for one): this cell again next frame, a
                -- few times; a long run of failures ends the scan with the reason
                why = tostring(why or "unknown")
                S.errors[why] = (S.errors[why] or 0) + 1
                S.tries = S.tries + 1
                if S.tries < 4 then return end
                S.failedRun = S.failedRun + 1
                if S.failedRun >= 2 * S.cols then
                    agent.scan = nil
                    if S.own and agent.view then releaseView(nil) end
                    return agentPrint({ id = S.id, ok = false, error = "screen_rays_failed", reason = why,
                                        errors = S.errors, done = S.i, of = S.n })
                end
                S.slots[k + 1] = "!"
                S.tries = 0
                S.i = k + 1
                done = done + 1
            else
                S.tries, S.failedRun = 0, 0
                local p = hit.hit and hit.position
                local slot = ""
                if p and p.x == p.x and p.y == p.y and p.z == p.z and math.abs(p.x) < 1e6 and math.abs(p.y) < 1e6
                    and math.abs(p.z) < 1e6 then
                    local name = tostring(hit.material or "")
                    local m = S.matIndex[name]
                    if not m then
                        m = #S.mats + 1
                        S.mats[m] = name
                        S.matIndex[name] = m
                    end
                    slot = ("%.1f,%.1f,%.1f,%d"):format(p.x, p.y, p.z, m)
                end
                S.slots[k + 1] = slot                   -- the slot first, then the count
                S.i = k + 1
                done = done + 1
            end
        end
        return
    end
    if not S.lines then
        if S.own and agent.view then releaseView(nil) end
        local misses, failed = 0, 0
        for k = 1, S.n do
            if S.slots[k] == "" then misses = misses + 1 elseif S.slots[k] == "!" then failed = failed + 1 end
        end
        local px, py, pz = Open77.character.position()
        -- the cells as one string ("x,y,z,material;" per cell, row by row, empty: nothing hit): the
        -- JSON encoder is bounded like an event, which a few thousand cells would overflow
        local head = agentLines({ id = S.id, ok = true, cols = S.cols, rows = S.rows, center = S.center,
                                  materials = S.mats, misses = misses, failed = failed, errors = S.errors,
                                  dynamic = S.filter.dynamic, seconds = math.floor((now() - S.startT) * 100) / 100,
                                  player = px and { x = px, y = py, z = pz } or nil })
        local text = head[1] and head[1]:match("^@agent (.*)}$")
        if not text then
            S.lines = head
        else
            S.lines = agentChunks(S.id, text .. ',"cells":"' .. table.concat(S.slots, ";", 1, S.n) .. '"}')
        end
        S.printed = 0
        return
    end
    while S.printed < #S.lines do
        print(S.lines[S.printed + 1])
        S.printed = S.printed + 1
    end
    agent.scan = nil
end

-- one screen ray through the agent's camera (or the player's), all the engine says about it
local function agentRay(req)
    local args = req.args or {}
    local u, v = tonumber(args.u) or 0.5, tonumber(args.v) or 0.5
    local dist = math.max(1, math.min(1000, tonumber(args.distance) or 800))
    local dyn = args.dynamic == true or args.dynamic == 1 or args.dynamic == "1" or args.dynamic == "true"
    local ray, why1, hit, why2
    if Open77.camera.screenRay then ray, why1 = Open77.camera.screenRay(u, v, dist) end
    if Open77.camera.screenRaycast then hit, why2 = Open77.camera.screenRaycast(u, v, dist, { static = true, dynamic = dyn }) end
    local px, py, pz = Open77.character.position()
    agentPrint({ id = req.id, ok = true, u = u, v = v, ray = ray, rayError = why1, hit = hit and {
                     hit = hit.hit, position = hit.position, normal = hit.normal, material = hit.material,
                     distance = hit.distance } or nil, hitError = why2, view = agent.view and agent.view.mode or nil,
                 player = px and { x = px, y = py, z = pz } or nil })
end

local function agentSay(text, by)
    text = tostring(text or ""):sub(1, 200)
    agent.said = { text = text, by = by, at = now() }
    if page and pageReady then page:send("maps:agent", { text = text, by = by }) end
    if not (page and pageReady) or not panelOpen then
        if Open77.hud and Open77.hud.notify then Open77.hud.notify((by or "Agent") .. ": " .. text, { replace = true }) end
    end
end

RegisterNetEvent(EV .. "agent:reply", function(p)
    if type(p) ~= "table" then return end
    agentPrint(p)
end)

RegisterNetEvent(EV .. "agent:say", function(p)
    if type(p) == "table" then agentSay(p.text, p.by) end
end)

RegisterNetEvent(EV .. "agent:do", function(req)
    if type(req) ~= "table" then return end
    local op, args = tostring(req.op or ""), req.args or {}
    if op == "view" then return agentView(req) end
    if op == "wire" then
        if args.on == false then
            agent.wire = nil
            if not preview and not edit then clearDrawing() end
        else
            agent.wire = { map = req.map, which = args.which }
            local m = server.maps[req.map]
            local a = m and ((args.which == "running" and m.running) or m.pending or m.running)
            if a and not edit then requestDraw(req.map, a, true) end
        end
        return agentPrint({ id = req.id, ok = true, wire = agent.wire ~= nil, map = req.map })
    end
    if op == "roads" then
        if args.on == false then
            agent.roads = nil
            return agentPrint({ id = req.id, ok = true, roads = false })
        end
        local m = server.maps[req.map]
        local a = m and ((args.which == "running" and m.running) or m.pending or m.running)
        if not a then return agentPrint({ id = req.id, ok = false, error = "not_placed" }) end
        local wx, wy, half = mapCenter(req.map, a)
        agent.roads = { on = true, map = req.map, id = req.id }
        askRoads(wx, wy, math.min(600, (tonumber(args.radius) or (half + 60))), true)
        return
    end
    if op == "fit" then return agentFitStart(req) end
    if op == "probe" then return agentProbe(req) end
    if op == "scan" then return agentScanStart(req) end
    if op == "ray" then return agentRay(req) end
    if op == "state" then
        local px, py, pz = Open77.character.position()
        local G = req.map and ghostOf(req.map)
        return agentPrint({ id = req.id, ok = true, client = {
            pageReady = pageReady, panelOpen = panelOpen, answered = link.answered, hellos = link.hellos,
            editing = edit and edit.map or nil, preview = preview, wire = agent.wire, roads = roads.lanes and #roads.lanes or 0,
            roadLines = roads.lines, view = agent.view and { map = agent.view.map, mode = agent.view.mode } or nil,
            wireState = wire.state, wireWhy = wire.why, ghost = G and { ready = G.ready, lines = G.n } or nil,
            player = px and { x = px, y = py, z = pz } or nil, stale = stale, errors = agent.errors } })
    end
    agentPrint({ id = req.id, ok = false, error = "unknown_agent_op", op = op })
end)

-- ------------------------------------------------------------------ per frame
local lastFrame = nil
local lastHud = 0
local stillSince = 0
local lastFast = -1e9
local startedAt = nil
local errors = 0

-- one frame of everything: the editor's movement, reading ghosts, drawing, the ground check
local function frameStep()
    local t = now()
    local dt = math.min(0.1, t - (lastFrame or t))
    lastFrame = t
    startedAt = startedAt or t
    local frameStart = clock()
    -- reading a ghost may take half the frame's slice; the drawing gets the rest
    deadline = frameStart + FRAME_BUDGET * budgetScale * 0.5
    local heavy = t >= backoffUntil
    if page and not pageReady and t - lastPing > 1.0 then
        lastPing = t
        page:send("maps:ping", {})
    end
    if needInit and pageReady then sendInit() end
    -- the server has not answered (its hello got lost, or the manager there is restarting), or it is
    -- still reading its maps: ask again, often at first, then now and then
    if not link.answered or link.loading then
        local since = t - (link.since or t)
        local every = link.loading and 3.0 or (since < 60 and 3.0 or 15.0)
        if t - link.helloAt > every then
            link.helloAt, link.hellos = t, link.hellos + 1
            TriggerServerEvent(EV .. "hello", { asked = link.hellos, slim = link.hellos >= 3 or nil })
            if link.hellos == 4 and not link.answered then
                say("no-answer", "the map manager on the server has not answered (asked %d times)", link.hellos)
                if page and pageReady then sendInit() end      -- the panel says so instead of "no maps"
            end
        end
    end
    -- the ghost of the map being edited or previewed first; the others a while after the start, in
    -- the background (the presence check and a later preview need them)
    local shown = preview or (agent.wire and agent.wire.map)
    local function shownAnchor(m)
        if not m then return nil end
        if not preview and agent.wire and agent.wire.which == "running" then return m.running or m.pending end
        return m.pending or m.running
    end
    local focus = (edit and edit.map) or shown
    if heavy and focus then
        local G = ghostOf(focus)
        if not G.ready then
            if parseStep(G, PARSE_SLICE) then
                local a = edit and edit.anchor or shownAnchor(server.maps[focus])
                if a then requestDraw(focus, a, true) end
            end
        end
    end
    -- the roads: around the map being moved, or where an agent asked for them
    roads.shown = (agent.roads ~= nil and agent.roads.on) or (edit ~= nil and not roads.missing)
    if edit and not roads.missing and heavy then
        local cx, cy, half = mapCenter(edit.map, edit.anchor)
        askRoads(cx, cy, math.min(600, half + 60), false)
    end
    if agent.view and t > agent.view.untilT then releaseView("its time was up") end
    deadline = frameStart + FRAME_BUDGET * budgetScale
    local captured = Open77.input.isCaptured and Open77.input.isCaptured()
    if edit and not captured then
        local a = edit.anchor
        local mult, label = 1.0, "normal"
        if held.fast then mult, label = 6.0, "fast" end
        if held.fine then mult, label = 0.15, "fine" end
        edit.speedLabel = label
        local speed, turn = (CFG.moveSpeed or 4.0) * mult * dt, (CFG.turnSpeed or 35.0) * mult * dt
        local fx, fy = facing()
        local rx, ry = fy, -fx
        local moved = edit.dirty
        if held.forward then a.x, a.y, moved = a.x + fx * speed, a.y + fy * speed, true end
        if held.back then a.x, a.y, moved = a.x - fx * speed, a.y - fy * speed, true end
        if held.right then a.x, a.y, moved = a.x + rx * speed, a.y + ry * speed, true end
        if held.left then a.x, a.y, moved = a.x - rx * speed, a.y - ry * speed, true end
        if held.raise then a.z, moved = a.z + speed * 0.5, true end
        if held.lower then a.z, moved = a.z - speed * 0.5, true end
        if held.rotate_left then a.yaw, moved = wrapYaw(a.yaw + turn), true end
        if held.rotate_right then a.yaw, moved = wrapYaw(a.yaw - turn), true end
        if edit.aim then
            local hit = Open77.camera.aimRay and Open77.camera.aimRay(800, { dynamic = false })
            if hit and hit.hit and hit.position then
                local p = hit.position
                if math.abs(p.x - a.x) + math.abs(p.y - a.y) + math.abs(p.z - a.z) > 0.01 then
                    a.x, a.y, a.z, moved = p.x, p.y, p.z, true
                end
            end
        end
        edit.dirty = false
        if moved then
            stillSince = t
            requestDraw(edit.map, a, true)
            edit.fastPending = true
        elseif not job and not want and t - lastDrawAt > REFRESH then
            requestDraw(edit.map, a, true)
        end
        if heavy then
            -- the outline, gates and cross follow the moving map 20 times a second; the rest after it
            if edit.fastPending and t - lastFast >= FAST_INTERVAL then
                lastFast = t
                edit.fastPending = false
                fastDraw(edit.map, a)
            end
            if timeLeft() then stepDraw(LINE_SLICE) end
            if not moved and t - stillSince > FIT_DELAY and timeLeft() then fitStep(edit.map, a, 2) end
        end
        -- the HUD: ten times a second while it moves, at once when it stops (the final spot)
        if t - lastHud > 0.1 or (edit.wasMoving and not moved) then
            lastHud = t
            sendEditorState()
        end
        edit.wasMoving = moved
    elseif shown and not edit then
        local a = shownAnchor(server.maps[shown])
        if a and not job and not want and t - lastDrawAt > REFRESH then requestDraw(shown, a, true) end
        if heavy and timeLeft() then stepDraw(LINE_SLICE) end
    elseif job or want then
        if heavy and timeLeft() then stepDraw(LINE_SLICE) end
    end
    if heavy then roadsStep(t) end
    if heavy and agent.fit and timeLeft() then agentFitStep(t) end
    if agent.scan and timeLeft() then agentScanStep(t) end
    if heavy and not edit and timeLeft() then
        presenceStep(t)
        if t - startedAt > BACKGROUND_DELAY and timeLeft() then
            for name in pairs(DEFS) do
                local G = ghostOf(name)
                if not G.ready then
                    parseStep(G, 64)
                    break
                end
            end
        end
    end
    if budgetScale < 1.0 then budgetScale = math.min(1.0, budgetScale + 0.005) end
    -- the header named maps whose own message never came: ask for those again (a few times)
    if link.answered and link.names and link.headerAt and t - link.headerAt > 4.0 and link.missingAsks < 3
        and t - link.missingAt > 5.0 then
        local missing = {}
        for _, name in ipairs(link.names) do if not server.maps[name] then missing[#missing + 1] = name end end
        if #missing > 0 then
            link.missingAsks, link.missingAt = link.missingAsks + 1, t
            say("missing:" .. link.missingAsks, "%d map(s) of the server's picture did not arrive (%s): asking again",
                #missing, table.concat(missing, ", "))
            TriggerServerEvent(EV .. "hello", { asked = link.hellos, missing = missing, slim = link.missingAsks >= 2 or nil })
        end
    end
end

CreateThread(function()
    local version = "?"
    if Open77.resource and type(Open77.resource.version) == "function" then
        local ok, v = pcall(Open77.resource.version)
        if ok and v then version = tostring(v) end
    end
    say(nil, "client %s starting", version)
    createPage()
    link.since, link.helloAt, link.hellos = now(), now(), 1
    TriggerServerEvent(EV .. "hello", { asked = 1 })
    while true do
        Wait(0)
        -- nothing that goes wrong in one frame may stop the loop: the next frame starts clean.  A
        -- refusal of the host's frame budget (other resources had it this frame) backs the heavy
        -- work off for a moment and shrinks its slices for a while.
        local ok, err = pcall(frameStep)
        if not ok then
            local why = tostring(err)
            if why:find("budget", 1, true) then
                backoffUntil = now() + 0.25
                budgetScale = math.max(0.25, budgetScale * 0.5)
            else
                job = nil
            end
            errors = errors + 1
            agent.errors = errors
            if errors <= 5 or errors % 500 == 0 then
                print(("[%s] frame error #%d: %s"):format(RES, errors, why))
            end
        end
    end
end)

AddEventHandler("onClientResourceStop", function(resource)
    if resource ~= RES then return end
    if page then page:setFocus(false, false) end
    clearDrawing()
end)
