# Changelog

## Studio 1.7.1 · converter 1.6.0 (2026-10-18)

### Cancel stops an install

* Cancelling an install on a server did nothing while the bundle uploaded: the SFTP upload ran on to
  the end on a slow link (minutes for a car bundle), and the server then installed it. Now a cancel
  during the upload drops the SSH connection at once, nothing is installed, and the partial upload
  is removed from the server over a new connection (an upload folder a stopped deploy could not
  remove goes with a later deploy, after a day). Packing the bundle stops too.
* Once the server runs the install script (backups, files, restart), a cancel waits for its end
  instead of cutting it half-way: the job says so, the Activity page shows *stopping*, and nothing
  after that step runs (a map going to two server folders stops before the second). A job whose
  last step was that one ends *done*. The `cancel_job` tool answers `stops_after` with the step;
  the terminal's Ctrl+C waits for the end of the step and prints what it did.

### Fixes

* Answers are strict JSON whatever a build report holds: a map floor or wall too small to take a
  coverage sample gave `NaN` in its report, which a browser's `JSON.parse` (the Studio's own
  pages) and MCP clients refuse, losing the whole answer. Such a surface counts as 0 % holes, and
  any NaN or infinity goes out as `null` (the API, the event stream and MCP).

## Studio 1.7.0 · converter 1.6.0 (2026-10-18)

### A terminal in the Studio

* **Terminal** (the rail, or Ctrl+` on any page, docked under it): one command line for the whole
  Studio. Every tool the AI agent and MCP clients have is a command (`build_car`, `deploy_car prod
  mode=stage`, `render_car mode=parts`...), with built-ins to list and follow jobs (`jobs`,
  `log <job> -f`, `wait`, `cancel`), drive the AI agent (`agent start <goal>`, `agent say`,
  `agent approve`...), call any API endpoint (`api GET /api/qc`, `routes`), run the converter's
  command line (`cli sounds`), a system command (`sh` / `!`, PowerShell by default), open a page
  (`ui quality`), read and change settings, and pick the project tools work on (`use urus`).
  Tab completion of commands, arguments, values, projects, servers and jobs; the arguments of the
  command being typed under the prompt; history kept with the workspace; Ctrl+C stops; `&&` and `;`
  chain. Jobs are live progress lines with their logs folded under them, renders show inline, JSON
  in colour.
* `python -m fivem2open77 term` is the same terminal in a command prompt (one line, or a prompt).

### Agents drive the whole app without taking over the screen

* New MCP tools: `terminal` and `terminal_output` (the terminal, the same one the user sees: their
  lines show up there, labelled), `studio_api` and `studio_api_routes` (any endpoint the app's
  buttons call), `agent_start`, `agent_send`, `agent_status`, `agent_approve`, `agent_stop` (the
  Studio's own AI agent), `studio_ui` (show the user a page and a note). The Terminal page shows
  what each agent runs as it runs.
* What stays the user's: system commands and the converter's command line (it runs the programs it
  is told to) are off for agents until Settings > Agents allows them; secrets, the MCP token,
  approvals, the MCP settings, the servers (they hold credentials), the programs the Studio runs,
  whether an install may unsecure a server's mods and where the Claude key is sent cannot be changed
  by an agent; the user's own terminal lines and history are not readable by agents; uploads are not
  offered (agents give paths).
  With "installs and restarts also wait for a click here", every server change an agent asks for --
  a tool, a terminal line, an API call, or the AI agent it started (whose full autonomy is then
  capped to asking before server changes) -- waits for the user's click.
* The tool list follows the Studio: the stdio bridge polls the Studio's fingerprint of what it lists
  and tells its client `notifications/tools/list_changed` (and for prompts and resources) when an
  update or a restart changed them, so new tools show up without restarting Claude, Codex or Cursor.
  (A client still running a bridge from before 1.7 needs one restart to get this bridge.)

The converter's output is unchanged: nothing is rebuilt.

## Studio 1.6.3 · converter 1.6.0 (2026-10-18)

### A car deploy that cannot read the server installs nothing

* Deploying one car first reads which car bundles the server has, so that a car carried in a bundle
  is re-packed with it. When that read failed (the server not answering SSH, say), the Studio went on
  and installed the car on its own, and a server that carried it in a bundle would then have defined
  its record twice. It now stops there, installs nothing and says so: deploy again when the server
  answers.

## Studio 1.6.2 · converter 1.6.0 (2026-10-18)

### Maps shipped as a single-player dlc.rpf

* A map packed the single-player way, one OpenIV `dlc.rpf` with its models and placements in a
  nested `x64/levels/gta5/<name>.rpf` (the Eiffel Tower is one), is unpacked on import like a
  single-player car, so the map converter sees its `.ymap` and `.ydr` files. It used to find
  none, and the Studio could neither look at the map nor build it. An archive that cannot be read
  is named in the error instead of being skipped in silence.

The converter's car output is unchanged, so builds from 1.6.1 stay current: nothing is rebuilt.

## Studio 1.6.1 · converter 1.6.0 (2026-10-18)

### Faster cars with more grip and stronger brakes

* **Performance tune** (`handling.tune`, the Handling tab): GTA's handling numbers are made for GTA's
  physics; mapped one to one onto Cyberpunk's drive model, an SUV or a muscle car came out with less
  grip and weaker brakes than any vanilla car (the Urus: 0.33 N m/kg, brakes 0.8x and grip 0.92x the
  donor's). `sport`, the default, measures every car against Cyberpunk's own fast cars: at least
  0.40 N m of torque per kg and 30 % more than the mapping (0.70 at most), a top gear 20 % higher
  (151-223 km/h), brakes at least the donor's and 40 % more (and a stronger braking friction), tyres
  at least the donor's and 10 % more, less drag, and less body roll (anti-roll bars 1.5x as stiff,
  less weight thrown sideways and forwards, centre of mass 3 cm lower) so the extra grip does not
  tip a tall car over. `track` goes further; `stock` is the old mapping. The per-car multipliers
  apply after the tune.

### Engine sounds by the GTA counterpart's engine

* A car's Cyberpunk engine is now picked by what its GTA counterpart (the GTA car whose sound it
  used in FiveM) really has: a V12 or V10 exotic (the Revuelto, the Aventador, a Viper-voiced
  Cayenne: Herrera Outlaw), a W16 or hybrid hypercar (Rayfield Caliburn), a twin-turbo V8 supercar
  (Quadra Turbo-R), a fast luxury SUV (the Urus: Mahir Supron GT), a G-Class (Militech Hellhound), a
  boxer four (the WRX STI: Mizutani Hozuki STI), a revvy four (Hozuki sport), a straight-six or
  twin-turbo V6 (Mizutani Shion), a rotary, V8 sport saloons (Quadra Sport R-7 GT), limousines
  (Chevalier Thrax), grand tourers (Villefort Deleon), 60s muscle (Thorton Colby GT)... out of 81
  Cyberpunk sounds instead of one per vehicle class. Add-on sound packs go by the engine their name
  gives (`chevydmaxeng`: a Duramax diesel pickup, `aq70bmws58`: BMW's S58 straight-six), and a car
  with neither by its own model and display name. GTA's own engine audio is not copied (Cyberpunk
  cannot play it, and it is Rockstar's to ship).

### Every car on a server: car bundles

* **Deploy every car** (Servers page; `deploy_all_cars` for agents): every car whose latest build is
  good goes onto the server in car bundles, `f2o77_cars_N`, twelve cars each (an Open77 server may
  require at most 64 mods, and each preloading resource is one). Cars keep their bundle (read back
  from the server), only bundles whose cars changed are re-packed, cars installed on their own are
  updated in place, and deploying one car the server carries in a bundle re-packs that bundle
  instead of installing the car twice.
* Each car resource also writes what it announces to the car hub as data (`server/car.json`).

### Quality checks v3

* Builds made before the tune and the new engine sounds are stale: the sweep rebuilds them. A new
  check reports each car's tune, torque per kg, top gear, brakes and grip.

## Studio 1.6.0 · converter 1.5.0 (2026-10-18)

### Cars come out whole: four-door donor, every part on a bone, wheels on their bones

* **Four-door cars** (SUVs, saloons, pickups with rear doors) convert with a four-door donor, the
  Archer Hella: its rig has the rear door, rear window and rear seat bones, its animations open the
  rear doors, its seat set seats four and its entity has the rear seats' interaction points. With the
  two-door Porsche as the only donor, the rear doors and windows hung from bones its rig does not
  have and were left behind at the car's origin in game (the Urus, the F-250, every four-door car).
  The donor is picked by the doors the car really has (a door-sized panel at the side: a
  Lamborghini's engine-cover flaps on the rear door bones are not doors). The physics stays the
  Porsche's, which the FiveM handling is mapped onto; the record, seats and destruction are the
  donor's.
* **No part left behind**: a part bound to a bone the donor's rig lacks is fixed to the body instead
  (27 two-door cars had rear quarter windows floating at the car's origin).
* **Wheels on their bones**: the record wrote the Porsche's wheel offset (10 cm front, 8 cm rear)
  for every car, which pushed every tyre out of its arch (the Revuelto), and inherited the Porsche's
  camber (1.5 degrees front, 1 rear, rear wheels moved 2.7 cm). Both are 0 now: the tyres sit on the
  GTA wheel centres, upright (a profile can still set `handling.wheel_offset`).
* **Bodies that failed to import**: GTA models with zero-length normals or tangents (and a tangent
  handedness of 0) made WolvenKit refuse the mesh ("Operation is not valid due to the current state
  of the object"): the CHARGER and the Aventador (lp700r) did not build. Normals and tangents are
  made unit length with a handedness of +-1 before import.
* **Mod-kit parts**: a part is measured against the model itself, so a truck shipped as chassis,
  body and bed mods gets all three (the F-250 had no cab roof); a mod-kit part is fitted only when it
  is a big piece of the car (GTA fits none by default) and never when it stands above the car (the
  Urus's roof box) or hides inside it.
* **Third axle**: a 6x6's middle wheels are drawn, fixed to the body (they were missing).
* Third person camera by body size (sports car, saloon, SUV, truck presets).

### Quality checks around the clock

* Every build writes what it built (`conversion-report.json` `qc`) and its QA checks it: every part
  on a bone of the rig, doors the donor opens, seats a player can use, wheels on the GTA wheel
  centres with no offset or camber, tyres in their arches, no body geometry left out, head and brake
  lights. A build made before these checks is reported stale.
* The Studio sweeps every car and map a few minutes after it starts and every 6 hours (Settings
  `qc`), rebuilds what fails or is stale and checks it again; a project its rebuild did not fix is
  tried again only after the converter, its source or its profile changes. The **Quality** page shows
  every project's status and what the checks found; `qc_report` and `run_qc` give agents the same;
  `python -m fivem2open77 qc` runs a sweep from the command line, and Windows Task Scheduler can run
  it when the Studio is closed (Quality > *Run them in the background*).
* Maps: the floor check counts the floor the conversion lost (not the floors the FiveM map does not
  have either), and collision coverage far below its target fails the QA.

## Studio 1.5.3 · converter 1.4.2 (2026-10-14)

### Live maps: Night City as the game's own map screen draws it

* **The map screen's buildings, block for block.** The game's 3D map does not draw its buildings
  from the ~10,000 boxes its cursor hits (what the 3D city had): each district is a point cloud of
  oriented blocks, kept in a 16-bit texture (`base\fx\textures\3dmap\static\<district>_data.xbm`:
  three tiles side by side, the blocks' positions, their rotations and their sizes) that the
  map's triangle soup turns into boxes on the GPU, decoded with its material's ranges (TransMin,
  TransMax, CubeSize). The Studio now reads them the same way: **255,220 blocks** for the city
  (Watson 42,062, Heywood 41,683, City Center 41,291, Westbrook 38,544, Santo Domingo 38,006,
  Pacifica 23,022, Dogtown 21,882, the spaceport 8,730), each shaded by its district's own top-down
  grey map (`*_m.xbm`, the map's BaseColor), the roofs carrying its detail. Walking and the cursor
  use the blocks too.
* **The game's look**: the city in the map screen's slate blues, roofs lighter than the walls, thin
  outlines on the faces big enough to show them (the material's EdgeThickness), the city's pale
  mass from afar, quiet streets and NCART lines in their colours. *Neon* (the Studio's look) is a
  click away in the layers menu, with the same blocks.
* **The map screen's angles**: *Top*, *Map* (tilting as you zoom, like the game: a plan far out,
  the streets close in), *Iso* (35°, on a diagonal) and *Low* (14°), turned in 45° steps (`,` `.`),
  *North up* (`N`), through a *wide*, *normal*, *long* or *flat* (no perspective) lens. Keys
  T / M / I / L in orbit mode; the angle and the lens are remembered.
* A 3D city built by an older Studio still opens, with a note: one click rebuilds it from the game
  (about a minute) with the blocks. The cache grows from about 8 MB to about 21 MB; it stays on
  this computer.

### Setup

* **Fixed:** updating failed with *Access to the path is denied* while an agent app (Claude,
  Cursor, VS Code, Codex) had the Studio's MCP connection open: its bridge runs
  `runtime\python\python.exe` from the install folder, so the folder could not be moved. The setup
  now closes the Studio, its server and WolvenKit runs left from one, and when something still runs
  from the folder it replaces the files one by one: a file in use is renamed out of the way (Windows
  allows that) and deleted on a later start. The agent apps keep their connection.

## Studio 1.5.2 · converter 1.4.2 (2026-10-14)

### Liberty V (and any big map) imports

* Why it did not work: the 6.9 GB `libertyv.zip` was over the 4 GiB upload limit and was refused; a
  dropped folder cannot be uploaded at all (a browser only sees an empty file); a typed path made
  the request copy the 6.5 GB folder before it answered; and the first look of a whole city
  stopped with an error before the district picker could show.
* An import is a job now, with progress, and the project is there at once. A folder of 1 GB or more
  is **read where it is** (nothing copied; keep it there), an archive of 512 MB or more is
  **extracted once** into the Studio's library, smaller ones are copied as before. The free space is
  checked before anything is written (Liberty V: 31,835 files, 6.5 GB, linked in place).
* In the Studio's own window a dropped file or folder is read from its place on disk: the page asks
  the window for the path, nothing is uploaded. In a browser, type the path (uploads now go to
  64 GB, with the free space checked first).
* **A whole city opens on its overview**: where its objects stand (a density map of the city from
  above), its districts (ymaps grouped by name, centred where their objects are, a stray ymap far
  away no longer makes a district kilometres wide) and an estimate of what an area holds. Liberty V:
  160,319 objects in 1,300 ymaps, 37 districts, read in 13 s (its collision is not read for the
  overview). Click the map or a district, set the radius (400 m by default), and *Convert this
  district*. Each district is a map of its own with its own resource id (`libertyv_manhat01`...),
  so several stand side by side on a server; *Convert another district* makes a new project that
  reads the same files, with the converted districts shown on the map, and refuses an id that is
  already taken. Building a whole city at once is refused with the reason.
* The AI agent: `pick_district` (by name or area, in the project or as a new one); `import_resource`
  waits for the import and the first look, and says when a map is a city.

### Car packs

* Drop a car pack on *Cars* (the FiveM Civ Car Pack 2: 68 cars in one 2 GB zip): it is held once,
  its cars are listed with their name, make, class and folder, you tick the ones you want and each
  becomes a project of its own that reads only its own folder of the pack. One job reads them one
  after another (parts, textures, name, thumbnail); the list fills as they are done.
* The pack's card: *Build all* (one build after another), *Build the unbuilt*, *Install all* (every
  built car in one bundle: one install, one restart), *Add cars* for the ones not picked yet, and
  *Delete* (the pack with its cars' projects). Importing the same pack again reuses it.
* A car's other texture dictionaries are its own resource's only, and they are read only for a
  texture its own dictionaries lack (a car of a pack loaded every texture of the whole pack).
* A car folder named after the car's model id (`ikx3_c63se24`) is not taken for a better name.
* The AI agent: `import_car_pack` (list a pack's cars, make projects for some or all of them).

## Studio 1.5.1 · converter 1.4.1 (2026-10-14)

### Live maps: no more black screens in the 3D city

* Root cause, found on the reporting machine (RTX 5070 Laptop GPU, ANGLE on Direct3D 11, in the
  Studio's own page): half of the 3D view was black. With *Glow* off it was clean; skipping one
  shader at a time showed it was the map beams. A beam's fade was `pow(1.0 - y, 1.6)`, and on
  Direct3D `pow` is `exp2(y * log2(x))`: where the texture coordinate interpolates to a hair over
  1.0 the base is a hair below zero and the result is NaN. One NaN pixel in the HDR buffer, blurred
  by the bloom, comes out as a black square the size of half the screen. Software rendering (the
  cloud tests) never produced the NaN.
* Fixed at the source and guarded: fractional powers take a clamped base, every square is a
  multiplication (no `pow(x, 2.0)` anywhere: the city, the home hologram, the background), normals
  are normalised so that a zero vector cannot become NaN, and the composer now cleans the HDR
  buffer before the bloom (anything not finite becomes black, nothing brighter than the bloom can
  carry). Checked on the same GPU with the corrected shaders: 0 % of the view black with Glow on
  (53 % before).
* A lost graphics context (a driver reset, a GPU switch, too many contexts) no longer leaves the
  view black: it says so and builds the view again on a new context; three losses in a minute stop
  with a way out (the Fast quality, Glow off). Every 3D view gives its context back when it closes
  (the car, map, city and hologram viewers), so moving between pages never runs into the browser's
  context limit. Checked by losing the context on purpose.
* A narrow window put the city beside the side panel until it had no width left (a black strip):
  under 1100 px the panel goes under the city.
* The page's own problems go to the Studio's log now (`logs/studio-*.log`): script errors,
  rejected promises, a lost context, a shader the GPU refused (with the driver's words) and the GPU
  itself, so a problem on someone's machine leaves a trace (`POST /api/diag`, bounded).
* The focus pulse is a little softer (with the bloom it washed the streets out at its peak).

### Cars (converter 1.4.1)

* Packs that ship a FiveM (legacy) and a GTA V Enhanced (gen9) copy of the car: the legacy files
  are used. The vehicles.meta met first could be the Enhanced one, and the model nearest to it was
  then the gen9 one the converter cannot read (the Revuelto SV pack: *this is a GTA V Enhanced
  (gen9) resource*). Texture dictionaries follow the same rule.

## Studio 1.5.0 · converter 1.4.0 (2026-10-14)

### Live maps: Night City in 3D

* The Studio reads the game's own 3D world map — the model on the game's map screen, under
  `base\entities\cameras\3dmap` — out of the installed game once (Live maps > *Build the 3D city*,
  about a minute: WolvenKit `uncook` exports the meshes, `convert serialize` reads the map entity and
  the building colliders) and keeps a compact cache in the workspace (`citymap/`, about 8 MB, never
  uploaded or shipped): terrain, streets and kerbs, the NCART lines in their colours, water, the
  landmarks, 10,166 building volumes and the 23 district and sub-district outlines, every part placed
  the way the map entity places it (parent bindings, the streets' half-turn, the lowered terrain).
  A game patch marks the cache stale.
* Live maps opens on it (*3D city* / *Plan*): each map on the server where it stands, its new spot,
  the previous one and the saved spots, as light beams, footprints (seen through buildings) and
  labels; the players online; the other maps. *Orbit* like a map (drag, right-drag, wheel to the
  cursor, WASD, double-click to go somewhere), *Fly* (mouse look, WASD, Space/E up, C/Q down, Shift
  fast, Alt slow, wheel for speed) or *Walk* the streets (gravity, run, double jump, buildings in the
  way, roofs to stand on; walking starts on the nearest street, not inside a building). *Set the
  spot* places the new spot with a click on the city (ground, street or roof); arrows, Q/E and
  PgUp/PgDn nudge it as in the plan view; nothing moves in game until Apply, as before.
* HUD: compass, camera and aim coordinates, altitude, the district under the cursor, *Go to*
  (districts, sub-districts, the maps), layers (buildings, streets, NCART, districts, labels, grid,
  district colours, hologram buildings), glow (bloom) and a fast mode; dynamic resolution holds the
  frame rate; full screen. Works with no server configured too.
* Fixed: Live maps said *Not connected — object is not iterable (cannot read property
  Symbol(Symbol.iterator))* for servers whose maps have no saved spots. The map manager is Lua, and
  Lua's JSON has no empty array: no saved spots went out as `"presets": {}`, the page looped over it
  and the error was shown as a connection failure. The Studio now turns every list the manager sends
  (saved spots, history, maps, players) into a list before anything uses it (the agent's
  `live_maps`/`live_map` too), the page copes on its own as well, and a problem drawing an answer is
  shown as a display problem, never as *Not connected*. Reproduced with the 1.4.1 page against a
  manager answering like the real one, fixed with both layers checked separately.
* `GET /api/citymap`, `POST /api/citymap/build`, `GET /api/citymap/citymap.{json,bin}` (cacheable by
  key). Regression tests build the cache from a synthetic 3dmap (WolvenKit-shaped JSON and glTF) and
  drive the API; the real build was checked against the game's own files (`basegame_1_engine`).

### The Neural HUD: a high-tech Studio

* Every page sits over a living background (WebGL): a hex lattice that lights up around the cursor,
  a horizon grid, drifting data motes, a slow scan, and shockwaves that ripple the lattice where you
  click. Over it: glass panels with corner brackets, neon hairlines, mono read-outs.
* The cursor: a light trail with sparks, a turning reticle, target-lock brackets that jump to the
  button, link, field or card under the cursor (with what it is), and neural links to the motes
  around it. Panel borders light up where the cursor is, primary buttons are magnetic, showcase
  cards tilt, buttons ripple, page titles decode, the window boots with a scan.
* Home: Night City as a hologram on a projector pad (when the 3D city is built), turning slowly and
  leaning towards the cursor; it opens Live maps.
* The start screen: the window no longer shows a plain "Starting..." line while the server starts
  -- it opens on the HUD drawn natively (GDI+, about 60 fps, only while on screen): the hex lattice
  lighting up around the cursor and rippling where you click, the horizon grid running towards you,
  drifting motes wired to the cursor, a scan, the emblem with turning rings and pulses, the title
  decoding, a segmented progress bar, read-outs in the corners, and the cursor's reticle, light
  trail, sparks and shockwaves. It stays over the page until the page has loaded (no blank flash),
  says so if the page does not load (with *Try again*), and carries *Open in browser* when the
  Studio runs in the browser. The installer's window is the same HUD with the copy's progress.
* Settings > Appearance: *Full*, *Lite* (still background that answers the cursor, brackets and lit
  panels) or *Off*, kept per computer; the system's reduce-motion setting starts at Lite. Bounded
  work: nothing runs while the window is hidden, the background falls to 20 fps at rest and to 8 fps
  behind a 3D view, and over the 3D views the cursor stays clean for precise work.

### Cars (converter 1.4.0): every GTA texture and part comes across

* Lamps wear their GTA textures (per-shader `vehicle_lights` materials: base colour + emissive),
  lenses use the light-cover glass; lamps on doors and lids get light meshes that move with them;
  lamp geometry modelled on body bones is claimed by the nearest light bone; light bones without
  geometry still cast light; light-less concept models get head and brake lights.
* Paint textures (baked logos, trims, ambient occlusion) and liveries become decal overlays over the
  multilayer paint, so CrystalCoat / Open77 paint still recolours the car.
* Glass: palette textures split into tints, opaque frit and pillars solid, mixed triangles get an
  alpha-tested copy. GTA colour slots in `matDiffuseColor` (wheels, interior, dashboard) tint the
  texture instead of blowing it out.
* Extras: `requiredExtras` on, extras under lids follow their toggle, parts the car needs to be whole
  are fitted; carcols mod-kit parts are fitted when the body or seats only exist there; profiles can
  choose (`parts.extras`, `parts.mods`).
* Near-name texture matching, unique texture file names, unused materials pruned, a BC3 encoder for
  generated textures. The Studio's preview and renders show textures per triangle; the project page
  shows required/needed extras and a mod-kit card.
* Checked with real builds and QA of the cars in the Studio (C63 S E, M4 F82, the Sosa, F-250,
  Revuelto, i8, Biome, the e400 of the FiveM Civ Car Pack).

### Maps: whole cities, GTA-only textures, speed

* `--area x,y,radius` (and the map options' *area* with a district picker) converts a city such as
  Liberty V a district at a time: only what stands there, with the texture dictionaries it needs
  through `gtxd.meta` parents; a city-sized input without an area stops and lists its biggest
  districts. Far collision files are read for their bounds only; the city-wide texture search is
  gone (a lazy index instead).
* Texture dictionaries converted with OpenIV name their textures `pack:/tl_church_marble.dds`; GTA
  finds that texture as `tl_church_marble` (the dictionary's hash list holds exactly that) and so do
  shaders. The converter keyed the long form and found none of them: 37,181 of Liberty V's 39,065
  textures are named so, and a district (150 m around -156, 161) came out with 594
  textures "missing" and 85 decal pieces left out. Names are now read the way GTA reads them (no
  `pack:/` or folder prefix, no file extension, in dictionaries and in shaders, for maps and cars):
  the same district has 945 textures, 2 truly absent (`mh_whitewall02`, and `none`, a placeholder
  that is now simply no texture), and every decal kept.
* Textures that only exist in GTA V itself (which cannot be shipped) give the surface a likely colour
  from its name instead of white; untextured cut-out decal sheets are dropped.
* The doorway check moves only the collision that can reach a doorway, all at once (a Liberty V
  district: minutes instead of a quarter of an hour).

## Studio 1.4.1 (2026-10-13)

Patch on top of 1.4.0: NCWE asset imports and MLO tooling follow-ups, the map deploy/upgrade
scripts for the VPS and home test server, and the studio architecture notes. The 1.4.0 section
below is the release it builds on.

## Studio 1.4.0 (2026-10-12)

### Universe home

The Studio opens on a redesigned home: an animated, cursor-reactive starfield and orbital Night
City scene, live project counts, and showcases of the user's actual converted cars and MLOs (real
build thumbnails and build status, linking into each project). Motion can be turned off (the
choice persists), follows `prefers-reduced-motion`, and stops on a hidden tab; the animation is
bounded (110 stars, DPR capped at 1.5, a 30 fps drawing target — not a benchmark) and cleans up
its listeners on navigation. Layout verified at 480 px and 1280 px with no console errors; the
editor pages keep the previous dense styling.

### NCWE world editor tools in the agent/MCP pipeline

All 60 tools of NCWE Studio beta.3's native MCP server (`NCWE.Mcp.exe --list-tools`) are exposed
as `ncwe_*` tools to the Studio's own agent and every connected MCP client (116 tools total on the
endpoint): asset search/preview, screenshots, raycasts, space measurements, collision editing,
lights/sound/effects, rooms, roads, elevators, prefabs, custom model import and exports — for
finishing MLOs (adding to them, fixing them) from the agent.

* Schemas are the native catalogue exactly; text, PNG and JPEG results are forwarded (JPEG
  screenshots were previously mislabelled as PNG to vision models — fixed).
* Read-only tools stay read-only. Editor mutations, raw commands, custom imports and exports use
  the Studio's strongest existing approval gate; external clients keep their own approval UI.
* Settings gains `paths.ncwe_mcp` and `mcp.ncwe_launch`. Default is **attach-only**: the agent
  connects to the user's open, signed-in NCWE editor; launching the editor is opt-in. NCWE's own
  account/access checks are never bypassed, and no NCWE binaries or game data are bundled.
* NCWE has a separate project from Studio's converted MLOs; agent instructions require explicit
  asset integration and visual verification, and export is not treated as installation.

Regression tests cover the catalogue/permission mapping, stdio handshake, notifications,
cancellation, timeout, process reaping, error propagation and image forwarding against a fake
MCP server; the real `NCWE.Mcp.exe` handshake and `tools/list` were checked against the bundled
catalogue.

### Maps: drag-and-drop NCWE asset imports

Map projects gain an **NCWE assets** tab: drop an NCWE export (a `.zip`/`.7z`/`.rar` ArchiveXL mod),
a folder, or loose game archives on it and the props, lights and meshes made in the NCWE world
editor ship with the map's builds, so they load wherever the map is installed. Imports live under
the project (`assets/ncwe/`), keep their drop-relative paths, and are repacked at build time into
clean preload packages (`<map>_ncwe_*`) declared alongside the map's own — the writer enforces
Open77's 8-package budget across both and names the overflow clearly. Imports without game
archives are kept but skipped at build with a note.

* Imported packages keep the archive names their editor exported; the "archives named after the
  map" QA rule now applies only to the map's own packages (it exists to stop two maps' geometry
  clashing, which imported assets do not affect).
* Uploads stream with progress, validate names, sizes and truncation, and are removable per
  import. Regression tests cover storage, repacking (folder, zip export, loose archive), the
  writer hook, both QA rules and the HTTP flow end to end — plus one fully offline end-to-end run
  through the map writer itself: three kinds of import repacked into preload packages, the shipped
  manifest declaring each (and passing the build QA), and the package budget enforced both ways
  (1 + 7 fits the 8 slots, 1 + 8 is refused with the overflow named).
* The same imports are agent/MCP tools now: `ncwe_assets_import` (a `.zip`/`.7z`/`.rar` ArchiveXL
  mod or a folder of game archives, name optional), `ncwe_assets_list`, and `ncwe_assets_verify` —
  a dry-run of the build's packaging: which imports become preload packages, what is skipped and
  why, and how many of Open77's 8 package slots the map would use.
* The assets tab shows what each import holds — the game archives it ships (with entry counts), the
  meshes and props its `.archive.xl` files list (checked against the archive's own index), and its
  other files — and warns about archive name collisions (one name shipped by several imports, or a
  name starting with `fivem2open77_map_` that belongs to map resources) before building. The agent's
  `ncwe_assets_list`/`ncwe_assets_verify` report the same, and build logs carry the warnings too.
* The last build goes **stale** when the NCWE imports change after it (builds record an import
  fingerprint): the build tab marks the build and both the build and assets tabs offer a one-click
  **Rebuild and deploy** — rebuild with the current imports, then deploy that fresh build to the
  server the map already lives on (the agent composes the same with `deploy_map` `rebuild: true`;
  `get_map` reports staleness).

### Importing converted maps into NCWE

The other direction: **Import to NCWE** (the map's NCWE assets tab, and the agent/MCP tool
`ncwe_import_map`) sends the converted map/MLO to the user's NCWE world editor — exported as a glb
(Y up, metres) and imported into NCWE's custom mesh library with its native `import_model`, then
placed in the NCWE world at the map's Night City spot (when one is set in Options) with
`place_objects`. A position/yaw can be given instead (`[x, y]` drops it on the surface below), and
`analyze_only` reports what would be imported without importing; with no spot and no position the
map goes to the mesh library only. It attaches to the user's open, signed-in NCWE editor like the
other `ncwe_*` tools (Settings > Agents > NCWE MCP executable) and uses the same strongest approval
gate as NCWE editor mutations. The `import_model`/`place_objects` schemas were checked against NCWE
Studio beta.11's `NCWE.Mcp.exe --list-tools` (unchanged from the bundled beta.3 catalogue).

### Maps: quiet interiors survive server re-bakes

Root-caused from a live repair (Cookies MLO): the server map baker's library shipped without
`mlo/ambience.py`, so when the baker rebuilt a map's sector after an in-game move it silently
dropped the quiet-interior ambient areas and the bottom floor heard the street again. The module
is now part of `BAKER_LIB`; the packaging is regression-tested in isolation (the toolkit imports
and emits the ambient-area node without Studio's source tree). A repaired sector round-trips
through WolvenKit with its `amb_interior` volume intact and covering the basement floor only.

### Package

* Windows host rebuilt (net48 x64, zero warnings); `fivem2open77-studio-1.4.0-setup.exe` installs
  per user as before and preserves projects and settings. Setup archive integrity verified.
* Deploy scripts `map_deploy.sh` / `baker_install.sh` are now tracked in `tools/deploy/remote`, so
  map/baker deployments work from a source checkout.

Known: none outstanding. The full test suite passes (81 tests); the five previously known failures
(four neon/seating expectations, one Windows `tar` drive-letter path) are fixed in this release.
