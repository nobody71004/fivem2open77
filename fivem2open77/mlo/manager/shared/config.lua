-- f2o77_maps defaults, read by the client and the server.  An update of the resource replaces this
-- file: a server owner sets the server-side values in data/config.json instead (same names, e.g.
-- { "defaultCountdown": 120, "emptyGraceSeconds": 60 }), which survives updates.  Players rebind
-- every key under Pause > Settings > KEY BINDINGS (or in the manager's Keys tab); the keys here are
-- only the defaults a player starts with.
F2O77_CONFIG = {
    -- the key that opens the map manager
    menuKey = "F9",

    -- the in-world editor (hold to move, tap the others)
    editorKeys = {
        forward = "UP", back = "DOWN", left = "LEFT", right = "RIGHT",
        raise = "PAGEUP", lower = "PAGEDOWN", rotateLeft = "HOME", rotateRight = "END",
        fast = "SHIFT", fine = "CTRL",
        aim = "INSERT", ground = "DELETE", save = "ENTER", cancel = "BACKSPACE",
    },
    moveSpeed = 4.0,          -- metres per second, x6 with the fast key, x0.15 with the fine key
    turnSpeed = 35.0,         -- degrees per second

    -- Apply: the countdowns offered for "apply now", and the default
    restartCountdowns = { 30, 60, 120, 300 },
    defaultCountdown = 60,
    emptyGraceSeconds = 30,   -- "apply when empty": nobody online for this long, then restart
    bakerTimeoutSeconds = 45, -- no answer from the map baker this long after a request: say so

    -- who may move maps: ACL right command.maps (every map) or command.mlo.<map> (one map)
    allMapsRight = "command.maps",
}
