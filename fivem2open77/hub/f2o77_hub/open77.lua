-- fivem2open77 car hub: brings cars installed by the fivem2open77 Studio online without anyone
-- having to restart the server by hand, and runs the converted cars' neon kits (/neon).
--
-- A converted car is a preload_mod package, and a server declares its packages only when it
-- starts.  The Studio installs the car's files and writes data/pending.json here; this resource
-- then restarts the server at a good moment -- as soon as nobody is online (after a grace
-- period), at a daily time, or after a maximum wait with a countdown -- announces the new cars to
-- players, and records its state in data/status.json for the Studio (only when it changes).
-- Car scripts (Lua) reload live and never wait for this.  Settings: data/config.json (see
-- server/main.lua).
resource "f2o77_hub"
version "1.1.2"
auto_start true

server_script "server/main.lua"
permissions { "runtime.restart", "filesystem.read", "filesystem.write", "world.vehicles", "world.vehicles.appearance" }
