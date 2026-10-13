# Studio agent architecture: tool registry, risk levels, MCP bridge

How fivem2open77 Studio's agent subsystem is put together: the tool registry that every
agent calls, the risk levels that decide when a human must approve a call, the MCP bridge
that lets agents outside the Studio (Claude Code, Claude Desktop, Codex, Cursor, VS Code)
drive the same tools, and the NCWE adapter that passes through to the Night City World
Editor's own toolset instead of reimplementing it.

## Source map

| File | What it holds |
|---|---|
| `fivem2open77/studio/agent/tools.py` | `Tool`, `Context`, the risk model, the core car/common tools, `registry()`, `tools_for()` |
| `fivem2open77/studio/agent/manager.py` | the built-in agent: `Session` loop, approvals, the system prompts |
| `fivem2open77/studio/agent/providers.py` | model providers (Anthropic API, a local Ollama) |
| `fivem2open77/studio/agent/maptools.py` | project, map-conversion and live-server tool groups |
| `fivem2open77/studio/agent/worldtools.py` | Night City world-edit tools |
| `fivem2open77/studio/agent/ncwe.py` | the NCWE adapter: a stdio MCP client for `NCWE.Mcp.exe` |
| `fivem2open77/studio/agent/ncwe_tools.json` | the captured NCWE catalogue (60 tool schemas) |
| `fivem2open77/studio/mcp.py` | the MCP server: sessions, tools/resources/prompts, Studio-side approvals |
| `fivem2open77/studio/bridge.py` | stdio→Studio relay (`python -m fivem2open77 mcp`) for stdio-only clients |
| `fivem2open77/studio/ncweassets.py` | NCWE asset imports in map projects (repacked into preload packages) |

## The shape of it

```
      built-in agent                    external agents
      (Claude / Ollama)                 (Claude Code, Codex, Cursor, VS Code)
             |                                    |
      providers.py                        POST /mcp (Bearer token)
      manager.Session                     or stdio bridge.py
             |                                    |
             +---------->  registry()  <---------+
                        (agent/tools.py)
                  Tool(name, description, schema, risk, fn)
                                |
                   Context(studio, session)
                                |
   +-----------+----------+-----+------+-------------+--------------+
   |           |          |            |             |              |
 projects   jobs       servers       QA         NCWE.Mcp.exe    Studio UI
 cars/maps/ build_car/  (SSH:        run_qa     (stdio MCP,     (events on
 world      build_map/  deploy,                 ncwe_* tools)   the bus:
            wait_job    restart)                                notes, approvals)
```

Both kinds of agent — the built-in one and MCP clients — call the *same* tool functions
with the same `Context`. The only differences are who answers approval prompts and who
the tool talks to when it says "the user".

## The tool registry

A tool is a name, a description, a JSON schema for its arguments, a **risk level**, and a
function `fn(ctx, args) -> list of content blocks` (`tools.py`):

```python
Tool("build_car", "Build the car with WolvenKit (2-5 minutes) ...", _obj(), "change", t_build)
```

`Tool.spec()` is what the model sees (name, description, schema — never the risk).
`Context` gives a call the Studio, the session's open project (raising `ToolError` when
none is open), `ctx.log()` for notes, and `ctx.wait_job()` which polls a Studio job,
cancels it on stop/timeout, and raises `StillRunning` past the session's soft timeout so
long calls can answer `{"status": "running", "job": ...}` instead of blocking a client.

`registry()` in `tools.py` merges five sources into one `{name: Tool}` dict:

| Group | From | Tools | Risk split |
|---|---|---|---|
| core car tools (`CAR_TOOLS`) | `tools.py` | `get_project, inspect_car, render_car, list_engine_sounds, update_profile, build_car, check_build, deploy_car` | 5 read, 2 change, 1 deploy |
| core common tools (`COMMON_TOOLS`) | `tools.py` | `list_servers, server_status, read_server_log, restart_server, ask_user, finish` | 5 read, 1 deploy |
| `STUDIO_TOOLS` | `maptools.py` | `list_projects, open_project, import_resource, enable_agent_api` | 2 read, 1 change, 1 deploy |
| `MAP_TOOLS` | `maptools.py` | `get_map, look_at_map, render_map, set_map_options, build_map, map_build_report, install_map_in_game, deploy_map, ncwe_assets_list, ncwe_assets_import, ncwe_assets_verify` | 6 read, 4 change, 1 deploy |
| `LIVE_TOOLS` | `maptools.py` | `live_maps, live_map, list_players, ground_height, set_map_spot, map_preset, apply_map_move, cancel_map_move, wait_map_move, send_player_to_map` | 5 read, 2 change, 3 deploy |
| `WORLD_TOOLS` | `worldtools.py` | `world_status, new_world_edit, world_prepare, world_find, world_object, world_delete, world_move, world_replace, world_add, world_clear_area, world_undo, world_drop_action, build_world_edit, install_world_edit_in_game, deploy_world_edit, remove_world_edit` | 4 read, 10 change, 2 deploy |
| `NCWE_TOOLS` | `ncwe.py` + `ncwe_tools.json` | `ncwe_*` (60 tools, see below) | 21 read, 39 deploy |

115 tools in total: 48 `read`, 19 `change`, 48 `deploy`.

`tools_for(kind)` decides what a session offers the model for its open project's kind —
a session never sees tools it cannot use; calling one that belongs to another kind returns
an error pointing at `open_project`. `change`/`deploy` failures also set the session's
`last_change_error`, which `finish` checks: the agent may not end a session with a summary
that hides a failed change unless it says so plainly on a second `finish` call.

| Session project kind | Offered tools | Count |
|---|---|---|
| `car` | `CAR_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + LIVE_TOOLS + NCWE_TOOLS` | 88 |
| `map` | `MAP_TOOLS + LIVE_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + NCWE_TOOLS` | 91 |
| `world` | `WORLD_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + LIVE_TOOLS + NCWE_TOOLS` | 96 |
| none | `STUDIO_TOOLS + COMMON_TOOLS + LIVE_TOOLS + (new_world_edit, world_status, world_find, world_prepare, remove_world_edit) + NCWE_TOOLS` | 85 |

## Risk levels

Three levels (`RISKS` in `tools.py`), attached to every tool at registration:

| Risk | Meaning (from the module docstring) | Examples |
|---|---|---|
| `read` | looks only | never asks |
| `change` | edits the project / builds | asks in "ask" mode |
| `deploy` | touches a game server | asks unless autonomy is "full" |

Who must approve is the risk level crossed with the session's **autonomy**
(`Session._needs_approval`, `manager.py`; autonomy comes from `ai.autonomy` in settings
or the session start call):

| Autonomy | `read` | `change` | `deploy` |
|---|---|---|---|
| `full` | runs | runs | runs |
| `auto_safe` | runs | runs | **asks** |
| `ask` (anything else) | runs | **asks** | **asks** |

An approval is a first-class event: the session emits `approval`, blocks on an event,
and resumes with `approval_result` when the user clicks approve/decline (with an optional
note that is passed back to the model as "Their note: ..."). Declining is not an
exception — the tool call simply returns as an error result.

Risk levels also drive **MCP tool annotations** (`McpHub.tool_list()`): `readOnlyHint`
and `idempotentHint` are `risk == "read"`, `destructiveHint` is `risk == "deploy"`, and
`openWorldHint` marks anything that reaches a game server (`LIVE_TOOLS` plus
`list_servers, server_status, read_server_log, restart_server, deploy_car, deploy_map,
deploy_world_edit, remove_world_edit, enable_agent_api`). External clients use these for
their own approval UIs.

## The built-in agent loop

`manager.py` runs one turn at a time per session: build the system prompt
(`SYSTEM_BASE` + the kind's playbook (`SYSTEM_CAR` / `SYSTEM_MAP` / `SYSTEM_WORLD` /
`SYSTEM_STUDIO`) + `SYSTEM_NCWE`), chat with the provider, execute every tool call it
returns (each gets a result, even when stopped mid-batch), append the results, repeat
until the model stops calling tools, `finish` is called, `max_steps` is reached, or the
user stops the session. User messages sent mid-run are queued and folded into the next
turn; `ask_user` blocks on the same inbox. Long builds/deploys are Studio jobs attached
to the session, so the user watches them like any other job. Events stream on the Studio
bus and are persisted per project under `agent/<session>.json`.

## The MCP bridge

`mcp.py` makes the Studio itself an MCP server so outside agents drive the same tools.

**Transport and auth.** MCP Streamable HTTP on the Studio's own port: `POST /mcp` with
JSON answers only (no server-initiated stream; `GET /mcp` is 405), `DELETE` closes a
session, batches are accepted. Every request needs `Authorization: Bearer <token>`, the
workspace's `mcp/token` file — created on first use with owner-only permissions, at least
32 characters, compared with `secrets.compare_digest`. A bridge started by a Microsoft Store
app only *reads* the token (it would write into that app's own AppData copy). When
`mcp.enabled` is off the endpoint answers 503. Clients that speak stdio only run
`python -m fivem2open77 mcp` (`bridge.py`): it finds the running Studio through the
workspace's `run/studio.json`, starts it when needed, and relays (log at
`<workspace>/logs/mcp-bridge.log`).

**Protocol.** `initialize` negotiates one of `2025-11-25, 2025-06-18, 2025-03-26,
2024-11-05`; the stateless `server/discover` era is explicitly not spoken. Notifications
(`notifications/cancelled`) set the running call's `stopping` flag. Each `McpSession`
holds the client's name/version (labelled for the Studio UI as Claude Code, Claude,
Codex, Cursor, VS Code, Windsurf...), its open project, the last 200 calls and 50 job
ids; sessions expire after 24 h idle.

**Tool surface.** `registry()` minus `ask_user` and `finish` (clients talk to their own
user) plus three bridge tools: `studio_status` (what is set up, running jobs, the world
index), `wait_job` and `cancel_job` — 116 tools. Project tools (the `CAR_TOOLS`,
`MAP_TOOLS` and the world tools minus the four global ones — `new_world_edit`,
`world_prepare`, `world_find`, `world_object`) gain an extra `project` argument: id or
name, switching the session's project before the call.

**Long calls.** A call that starts a long job answers when it finishes, or after
`mcp.call_seconds` with `{"status": "running", "job": "<id>", ...}`; the job goes on in
the Studio and `wait_job` follows it (each call waits up to ~55 s), `cancel_job` stops
it. No client times out waiting for a build.

**Studio-side approvals.** With `mcp.approval = "studio"`, `deploy`-risk calls pause in
the Studio (`McpHub._approve`): the approval appears on the bus, the user clicks
approve/decline (optionally with a note), the call resumes or returns "the user declined
this in the Studio". Unanswered approvals time out after 15 minutes. With any other
setting, the client's own approval UI is the gate, guided by the tool annotations.

**Resources and prompts.** `f2o77://guide` (workflows, rules, coordinates — composed
from the same `SYSTEM_*` texts the built-in agent uses), `f2o77://projects`,
`f2o77://servers` (no secrets) and `f2o77://project/<id>` (per-project JSON state, also
as a resource template). Four prompts turn arguments into a full job:
`convert_car`, `convert_map`, `move_map`, `edit_world`.

## The NCWE adapter

`ncwe.py` exposes the Night City World Editor's **own** toolset through its **own** stdio
MCP server (`NCWE.Mcp.exe`, shipped with NCWE Studio). No private engine
reimplementation: only public tool schemas are bundled, never NCWE binaries, game data or
account credentials. NCWE keeps its own sign-in and access checks; the adapter never
bypasses them.

**Catalogue.** `ncwe_tools.json` holds 60 tool schemas captured with NCWE Studio
beta.3's `NCWE.Mcp.exe --list-tools`, and every tool is registered as `ncwe_<original
name>`. They cover the editor as a whole: status/camera/screenshot/raycast/measuring,
object query and editing, collision and occlusion, roads and elevators, prefabs,
custom meshes and sounds, traffic lanes, undo/redo, and `export_mod` / `studio_command`.

**Risk mapping.** The catalogue's `readOnlyHint` annotation decides it: the 21 read-only
tools (search, screenshots, measurement, listing) become `read`; **all other 39 become
`deploy`** — the strongest existing gate — because exports and raw commands can write
arbitrary files or alter editor state. Under `ask` *and* `auto_safe` autonomy every NCWE
mutation therefore asks the user first.

**Connection lifecycle.** One isolated process per call, always reaped: `StdioClient`
spawns `NCWE.Mcp.exe` (no shell, fixed argv — tool arguments travel only as JSON-RPC
parameters) with its working directory next to the executable, does the MCP
`initialize`/`notifications/initialized` handshake (`protocolVersion 2024-11-05`,
`clientInfo fivem2open77-studio`), then `tools/list` (an installed NCWE that lacks the
tool gets a clear "update NCWE" error instead of a failure), then `tools/call`. Default
timeout is 180 s and a timeout reports failure — "no success is assumed". Results map
text, PNG/JPEG image and resource blocks back into the Studio's content blocks; an
`isError` result raises `ToolError` with the editor's own message.

**Configuration.** `Settings > Agents` points at `NCWE.Mcp.exe` (or the `NCWE_MCP_EXE`
environment variable). Calls **attach** to the user's open, signed-in editor by default
(`--attach-only`); launching the editor on demand needs the explicit `mcp.ncwe_launch`
setting.

**Prompt rules** (`SYSTEM_NCWE` in `manager.py`, also in `f2o77://guide`): NCWE has a
separate project — the Studio's converted MLO is *not* automatically loaded there, so
start with `ncwe_studio_status` and inspect before changing anything. Edits, custom
imports, raw commands and exports are approval-gated. Export is not installation or
deployment; an export reaches a server only through the Studio's own deploy tools. If the
editor is unavailable, signed out, or an export fails, the agent reports it — no promises
of perfect appearance without a visual check.

**NCWE asset imports** (`ncweassets.py`) are the complementary file-level path: a map
project stores what the user drags in from NCWE (an exported ArchiveXL `.zip`/`.7z`/`.rar`
or a folder) under `assets/ncwe/`. At build time each import that carries game archives
(`archive/pc/mod/*.archive`) is repacked into a clean preload package and shipped with the
map so NCWE-made props, lights and meshes load wherever the map is installed (Open77 caps
resources at 8 preload packages; the writer enforces the total). Imports without game
archives are kept but skipped with a note. This module never touches NCWE's own project
files or account state.

## Invariants when changing this code

- **Risk is the single switch.** It decides both human approval (built-in agent) and MCP
  annotations (external agents). A new tool must choose `read`, `change` or `deploy`
  honestly; anything that can reach a server or write outside the project is `deploy`.
- **Registration is three places:** add the `Tool(...)`, add its name to the right group
  tuple (`CAR_TOOLS`, `MAP_TOOLS`, `LIVE_TOOLS`, `WORLD_TOOLS`, ...) so `tools_for()`
  offers it to the right sessions, and let `registry()` pick it up (the map/world/ncwe
  registries are imported there).
- **`ask_user`/`finish` are the built-in agent's own.** MCP clients get them removed and
  bridge-replaced; keep tool functions from assuming one or the other exists.
- **The NCWE catalogue is captured, not written.** Regenerate `ncwe_tools.json` from
  `NCWE.Mcp.exe --list-tools`; the risk mapping is derived from its annotations, so
  review `readOnlyHint` changes when updating it.
- **Long calls must stay job-shaped.** Anything slower than a few seconds should submit a
  Studio job and let `Context.wait_job` raise `StillRunning`, so both the built-in loop
  and MCP clients can wait, watch or cancel it.
