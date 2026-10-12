# Changelog

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
as `ncwe_*` tools to the Studio's own agent and every connected MCP client (113 tools total on the
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
  writer hook, both QA rules and the HTTP flow end to end.

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

Known: the full test suite has five pre-existing failures (four neon/seating expectations, one
Windows `tar` drive-letter path), reproduced identically against the previous release; they are
unchanged in this release.
