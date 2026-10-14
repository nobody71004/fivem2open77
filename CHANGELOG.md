# Changelog

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
