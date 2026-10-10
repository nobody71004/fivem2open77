-- f2o77_maps: Night City's road lanes for the map manager (server side).
--
-- The tiles come from the game's own traffic data (`fivem2open77 mlo-roads` extracts them; the
-- manager update installs them in data/roads/): index.json and one <cx>_<cy>.lanes text file per
-- 512 m tile, one lane per line, its centre line in the direction of travel:
--     <id> <kind> <width m> <max speed m/s> x y z x y z ...
-- kind: r road, h highway, i road inside a junction, c crosswalk, p pavement, j pavement crossing,
-- t patrol route, o other.
--
-- A tile is read the first time it is asked for (a few hundred kB of text, parsed a slice at a time
-- with a Wait between slices, so a query must run in a scheduler coroutine) and the last few are kept.
-- F2O77_ROADS.near(x, y, radius, kinds, limit) -> lanes near a point, nearest first, each
--   { id, kind, width, speed, pts = { x, y, z, ... } (clipped to the radius), distance, heading, edge }
-- F2O77_ROADS.encode(lane) -> the lane as one compact line (how lanes travel to the clients).
F2O77_ROADS = (function()
    local DIR = "roads/"
    local TILE = 512.0
    local KEEP = 12                       -- tiles kept parsed
    local SLICE = 150                     -- lane lines parsed between two Waits
    local atan2 = math.atan2 or math.atan
    local info, infoRead = nil, false
    local tiles, lru = {}, {}

    local function readInfo()
        if infoRead then return info end
        infoRead = true
        if not (Open77.io and Open77.io.exists and Open77.io.exists(DIR .. "index.json")) then return nil end
        local v = Open77.io.readJson(DIR .. "index.json")
        if type(v) == "table" then
            info = { lanes = v.lanes, tile = tonumber(v.tile) or TILE, source = v.source, extractedAt = v.extractedAt }
            TILE = info.tile
        else
            info = { lanes = nil, tile = TILE }      -- tiles without a readable index still work
        end
        return info
    end

    local function touch(key)
        for i, k in ipairs(lru) do
            if k == key then table.remove(lru, i) break end
        end
        table.insert(lru, key)
        while #lru > KEEP do tiles[table.remove(lru, 1)] = nil end
    end

    local function parseLine(line)
        local id, kind, width, speed, rest = line:match("^(%d+) (%a) (%S+) (%S+) (.+)$")
        if not id then return nil end
        local pts, n = {}, 0
        for v in rest:gmatch("%S+") do
            n = n + 1
            pts[n] = tonumber(v)
            if not pts[n] then return nil end
        end
        if n < 6 or n % 3 ~= 0 then return nil end
        return { id = tonumber(id), kind = kind, width = tonumber(width) or 0, speed = tonumber(speed) or 0, pts = pts }
    end

    local function tile(cx, cy)
        local key = cx .. "_" .. cy
        local t = tiles[key]
        if t then touch(key) return t end
        t = {}
        local path = DIR .. key .. ".lanes"
        if Open77.io.exists(path) then
            local text = Open77.io.read(path)
            if type(text) == "string" then
                local count = 0
                for line in text:gmatch("[^\n]+") do
                    local lane = parseLine(line)
                    if lane then t[#t + 1] = lane end
                    count = count + 1
                    if count % SLICE == 0 then Wait(0) end
                end
            end
        end
        tiles[key] = t
        touch(key)
        return t
    end

    -- the closest point of a lane's centre line to (x, y): distance, segment index, its direction
    local function closest(pts, x, y)
        local best, bi, bdx, bdy = math.huge, 0, 0.0, 1.0
        for i = 1, #pts - 3, 3 do
            local ax, ay, bx, by = pts[i], pts[i + 1], pts[i + 3], pts[i + 4]
            local dx, dy = bx - ax, by - ay
            local ll = dx * dx + dy * dy
            local t = 0.0
            if ll > 1e-12 then t = math.max(0.0, math.min(1.0, ((x - ax) * dx + (y - ay) * dy) / ll)) end
            local px, py = ax + t * dx, ay + t * dy
            local d = math.sqrt((x - px) ^ 2 + (y - py) ^ 2)
            if d < best then
                local n = math.sqrt(ll)
                if n < 1e-9 then n = 1.0 end
                best, bi, bdx, bdy = d, i, dx / n, dy / n
            end
        end
        return best, bi, bdx, bdy
    end

    -- the part of a centre line within `r` of (x, y): from the first to the last segment that comes
    -- that close (a long segment passing through counts, whatever its ends)
    local function segDist(ax, ay, bx, by, x, y)
        local dx, dy = bx - ax, by - ay
        local ll = dx * dx + dy * dy
        local t = 0.0
        if ll > 1e-12 then t = math.max(0.0, math.min(1.0, ((x - ax) * dx + (y - ay) * dy) / ll)) end
        return math.sqrt((x - ax - t * dx) ^ 2 + (y - ay - t * dy) ^ 2)
    end
    local function clip(pts, x, y, r)
        local n = #pts / 3
        local first, last
        for k = 1, n - 1 do
            local i = (k - 1) * 3 + 1
            if segDist(pts[i], pts[i + 1], pts[i + 3], pts[i + 4], x, y) <= r then
                first = first or k
                last = k
            end
        end
        if not first then return nil end
        local keep = {}
        for k = first, last + 1 do
            local i = (k - 1) * 3 + 1
            keep[#keep + 1], keep[#keep + 2], keep[#keep + 3] = pts[i], pts[i + 1], pts[i + 2]
        end
        return keep
    end

    local function near(x, y, radius, kinds, limit)
        readInfo()
        local r = math.max(1.0, math.min(1000.0, tonumber(radius) or 60.0))
        limit = math.max(1, math.min(2000, tonumber(limit) or 300))
        local x0, y0 = math.floor((x - r) / TILE), math.floor((y - r) / TILE)
        local x1, y1 = math.floor((x + r) / TILE), math.floor((y + r) / TILE)
        local seen, out = {}, {}
        local checked = 0
        for cx = x0, x1 do
            for cy = y0, y1 do
                for _, lane in ipairs(tile(cx, cy)) do
                    if not seen[lane.id] and (not kinds or kinds == "" or kinds:find(lane.kind, 1, true)) then
                        seen[lane.id] = true
                        local d, i, dx, dy = closest(lane.pts, x, y)
                        if d <= r + lane.width / 2 then
                            local pts = clip(lane.pts, x, y, r + lane.width)
                            if pts then
                                out[#out + 1] = { id = lane.id, kind = lane.kind, width = lane.width, speed = lane.speed,
                                                  pts = pts, distance = d, edge = d - lane.width / 2,
                                                  heading = math.deg(atan2(-dx, dy)) }
                            end
                        end
                        checked = checked + 1
                        if checked % 400 == 0 then Wait(0) end
                    end
                end
            end
        end
        table.sort(out, function(a, b) return a.distance < b.distance end)
        while #out > limit do table.remove(out) end
        return out
    end

    local function fmt(v)
        local s = ("%.1f"):format(v)
        if s == "-0.0" then s = "0.0" end
        return (s:gsub("%.0$", ""))
    end

    local function encode(lane)
        local parts = { tostring(lane.id), lane.kind, fmt(lane.width) }
        for i = 1, #lane.pts do parts[#parts + 1] = fmt(lane.pts[i]) end
        return table.concat(parts, " ")
    end

    local function available()
        local i = readInfo()
        if i then return true, i end
        -- no index: are there tiles at all?
        return false, nil
    end

    return { near = near, encode = encode, available = available, tileSize = function() return TILE end,
             info = readInfo }
end)()
