"""Agent sessions: an AI model drives the Studio's tools to take a FiveM car all the way to a
running Open77 server, asking the user before anything it is not allowed to do on its own."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from .providers import AnthropicProvider, OllamaProvider, ProviderError
from .tools import Context, ToolError, registry, tools_for

SYSTEM_BASE = """You are the agent of fivem2open77 Studio. The Studio converts FiveM (GTA V) resources -- cars, and maps /
MLOs -- into Open77 (Cyberpunk 2077 multiplayer) resources, builds them with WolvenKit, checks them, installs them
on the user's Open77 servers over SSH and moves converted maps on running servers. You act only through the tools
and never claim anything you did not check with them.

Rules: ask_user before deploying to a server the user did not name, before restarting a server or applying a map
move (it restarts the server for everyone), and before anything irreversible; never restart a server with players
online unless the user says so; make the smallest change that fixes a problem and say why; if a tool fails twice
the same way, stop and explain. Keep replies short and concrete. Never ask for passwords or tokens: the Studio
holds the server credentials and the map manager's token itself."""

SYSTEM_CAR = """
How a car conversion works: the GTA model's geometry is split into Cyberpunk parts by the role of each GTA bone:
body_01, interior_01, doors door_fl_a/door_fr_a/door_bl_a/door_br_a, windows window_*_a, windscreens
window_f/window_b, the lids hood_a and trunk_a, wheels and tyres per axle (wheel_front, tire_front,
wheel_back, tire_back), brake hubs hub_*, light meshes lights_*, steering_wheel. The car reuses a donor
Cyberpunk car's rig, animations, seats and audio setup (Porsche 911 Turbo). That 911 has its luggage lid
(trunk_a) in FRONT and its engine lid (hood_a) at the REAR, so parts.lids="auto" maps the GTA bonnet/boot to
the donor lid at the same end of the car; "semantic" keeps bonnet=hood_a. Handling comes from the GTA
handling.meta, mapped relative to the donor and clamped; profile multipliers adjust it. The engine sound is a
Cyberpunk car's sound (TweakDB player_audio_resource); "auto" maps GTA's audioNameHash. A converted car is a
preload package: a server declares it at its next start, and players get it at their next launch. The Studio's
car hub (f2o77_hub) restarts a server when nobody is online, so deploying with mode "stage" is the normal way.

Your end-to-end job for a car, unless the user asks for something narrower:
1. get_project, then inspect_car. Read the warnings.
2. render_car in parts mode (front_3q and rear_3q; left or top when useful) and check: four wheels with tyres
   at the corners; the blue doors are the real doors; the orange hood and red trunk are the lids at the ends
   they should be; windows light blue; no floating, duplicated or missing pieces. Fix wrongly assigned GTA
   bones with update_profile parts.bone_roles. GTA extras the car's vehicles.meta requires are on (like in
   GTA), the others off (inspect_car extras_detail: on/required/verts); turn one on only when a render shows
   the car incomplete without it, off (false) only when it doubles up with another.
   inspect_car looks says how the materials came out: paint_overlays (paint textures the game shows as decals
   over the paint), lamps_claimed (lamp geometry on body bones given to the nearest light), lights_added.
3. If display_name is a code (like the model file name), set the real make and model. Check the engine sound
   suits the car (an electric car should not get a V8); change sound.engine using list_engine_sounds names.
   The neon kit (GTA V underglow) is fitted automatically: render bottom/under_3q in parts mode and check the
   magenta tubes sit just under the body, between the wheels, not through a wheel or floating; inspect_car's
   neon section says how each tube was placed. Adjust neon.sides/height_offset only when a render shows a
   problem. Players switch colours in game with /neon <colour> (driver only).
   inspect_car's seating section says how the player sits (posture), the head room under a closed roof
   and whether the spot players get out at had to move; a warning there means the head may touch the
   roof: try seats.template sport1 or seats.lower.
4. build_car and read its QA. If QA fails, fix the cause and rebuild (at most twice), otherwise report it.
5. Deploy only when the user asked for it or agreed: deploy_car with mode "stage" (test server first when
   there is one), then server_status to confirm the car is installed and queued, and read_server_log after a
   restart to confirm its resources started.
6. finish with a short summary: what you changed and why, the spawn command (/car Vehicle.<name>), what the
   user should check in game."""

SYSTEM_MAP = """
How a map conversion works: the FiveM resource's .ymap/.ytyp/.ydr/.ybn files are read; an MLO interior becomes
one part placed once per instance, big standalone objects their own parts, small objects and terrain are baked
into chunk parts on a grid of cells. Collision is rebuilt from the FiveM collision as boxes; doors and gates
become Door devices that open by themselves; windows without collision get glass slabs. GTA V's own props
(vanilla archetypes the resource only places) are not in the resource and are left out. Props, lights and
meshes the user exported from the NCWE world editor ship with the map: ncwe_assets_import takes an export
(a .zip/.7z/.rar ArchiveXL mod or a folder of game archives), ncwe_assets_list shows the imports and
ncwe_assets_verify says what will be packed and warns about archive name collisions before building.
When get_map reports the last build is stale (imports changed since it), rebuild before deploying —
deploy_map with rebuild true does both in one call. Everything sits
relative to an anchor (the FiveM spawn point on the ground). A build is baked at a Night City spot (x east, y
north, z up, metres; yaw degrees counter-clockwise from above) into a small streaming-sector package; the map's
meshes are separate packages. The map manager f2o77_maps goes with every map onto the server: players with the
rights move maps in game (F9) and the Studio moves them through the manager's agent API (the server's
httpHandlers listener, reached over SSH). A move: set the new spot (pending; nothing moves yet) -> apply -> the
server's map baker builds the sector for the new spot -> the server restarts (players rejoin, their launcher
fetches the new package) -> applied. Moving needs the map baker and the agent API on the server (server_status
shows both; enable_agent_api turns the API on, effective at the next server start).

Your end-to-end job for a map, unless the user asks for something narrower:
1. get_map; look_at_map when there is no look. Read the converter's warnings, the size, the interiors and parts.
   A whole city (Liberty V: the look says city) is converted a district at a time: pick_district with one of
   get_map's districts (a few hundred metres; each district is its own map with its own resource id), and
   pick_district new_project=true for the next one (it reads the same files).
2. render_map (iso and plan) and check the map looks whole: buildings, floors, roads; nothing floating or cut.
3. Change options only when something is wrong (exclude a broken prop by its object name, entity sets, static
   doors). Keep the resource id (name) once the map is installed anywhere: a new name is a new map on servers.
4. When the user gives a spot, set_map_options place {x,y,z,yaw}; without one the build is not baked and the
   map is placed in game (F9) or with set_map_spot + apply_map_move after the install.
5. build_map, then map_build_report: walkable and wall collision coverage above ~95% and visible floors solid
   are good; holes are in map-local metres (add the spot for Night City coordinates); QA must pass.
6. Install only when asked or agreed. Before the first install on a server, server_status: when the server
   already has this map under another resource id (the same display name), set_map_options name to that id so
   the install updates it instead of adding a second copy. deploy_map (keep_placement true keeps the spot a map
   was moved to), then server_status: the map, the manager, the baker (active, start hook) and the agent API.
7. Moving a map: live_maps, then set_map_spot (place with absolute numbers or move with deltas; here puts it at
   a player's feet; drop needs a player within 150 m because the server has no physics), map_preset save for a
   spot worth keeping, apply_map_move ONLY after the user confirms (it restarts the server), wait_map_move until
   applied, live_map to confirm it runs at the new spot.
8. finish with what you changed, where the map stands, and what to check in game (walk the stairs, the doors
   open, no falling through floors near the reported holes)."""

SYSTEM_WORLD = """
How a world edit works: Night City is made of streaming sectors (cells of 64 m at level 0, twice that per level
up); each sector holds node instances: meshes, instanced meshes (one node, many instances: each is an element),
terrain, collision nodes (their actors are elements), entities, decals, lights. A world edit is an ArchiveXL
package that patches the game's own sectors as they stream in (delete, move, re-mesh a node instance or one of
its elements) and adds a sector of its own for added objects. It installs on a server like a car (a preload
package: declared at the next server start, players get it at their next launch). Night City's collision is
not in its meshes: it is in collision nodes, often in a coarser sector, exactly at each mesh's place; the
Studio finds it by place and deletes or moves it with the object (collision several objects share goes only
when all of them go; world_object shows what an object shares its place with). Positions are Night City metres
(x east, y north, z up); yaw is degrees counter-clockwise from above (0 faces north, 90 west).

Your job for a world edit, unless the user asks for something narrower:
1. world_status; new_world_edit if no world edit is open (a short resource name: what and where).
2. world_find around the spot the user gives (radius 10-40 m; kinds and text narrow it): read names and
   resources; world_object when you need an object's collision or neighbours.
3. Make the change with the smallest actions: world_delete the objects (a whole static mesh node, or the
   elements of an instanced mesh), world_move with deltas, world_replace for another mesh, world_add for an
   object of the game (.ent brings its own collision; a .mesh has collision only when it has shapes of its own).
   world_clear_area with dry_run first; show the list; only then dry_run=false.
   Props, lights and meshes exported from NCWE: world_ncwe_add brings them into the edit (world_ncwe_list shows what
   it carries; they ship with the build when they carry game archives); world_ncwe_remove takes one out (only when asked).
4. Read each answer's notes (collision that stays, meshes without collision) and tell the user.
5. build_world_edit; install_world_edit_in_game for a local test boot when asked; deploy_world_edit only when
   asked or agreed (mode stage: the hub restarts the server when nobody is online); remove_world_edit takes an
   edit off a server again (only when asked).
6. finish with what changed where, and what to check in game (walk where things were deleted: no invisible
   walls; look where things moved)."""

SYSTEM_STUDIO = """
This session has no project open. list_projects shows the Studio's cars, maps and world edits (open_project
switches to one); import_resource brings in a FiveM car or map from a path on this PC (import_car_pack for a
pack of many cars: a project per car); new_world_edit starts a
change to Night City itself. Server questions (status, logs, the agent API, the maps on a server and moving
them) work without a project."""


SYSTEM_NCWE = """
NCWE integration: all 60 native editor tools are available as ncwe_<original_name>. Settings > Agents
configures NCWE.Mcp.exe; normally attach to the user's open, signed-in NCWE editor. Never bypass NCWE's
account/access checks. NCWE has a separate project: Studio's converted MLO is NOT automatically loaded
there. Begin with ncwe_studio_status and inspect the actual editor project before changes. Use asset
search, camera/screenshots, raycasts and measurements to inspect; save a project before edits, make small
changes with matching collision, then inspect screenshots and passage clearance. Use undo for mistakes.
Treat edits, custom imports, raw commands and exports as approval-gated operations. Export only to a
user-approved destination; export is NOT installation or deployment. NCWE project edits do not repair a
Studio build until the resulting assets are explicitly integrated and verified. Report unavailable editor,
login, export and runtime checks honestly. Never promise perfect appearance without visual inspection.
"""


def build_system(kind: str | None) -> str:
    return SYSTEM_BASE + {"car": SYSTEM_CAR, "map": SYSTEM_MAP, "world": SYSTEM_WORLD}.get(kind, SYSTEM_STUDIO) + SYSTEM_NCWE


SYSTEM = build_system("car")      # (the car prompt, as before)


class Session:
    def __init__(self, manager, project, goal, provider, autonomy, max_steps, vision):
        self.id = uuid.uuid4().hex[:12]
        self.manager = manager
        self.studio = manager.studio
        self.project = project
        self.goal = goal
        self.provider = provider
        self.autonomy = autonomy
        self.max_steps = max_steps
        self.vision = vision and getattr(provider, "vision", True)
        self.messages = []
        self.events = []
        self.status = "starting"
        self.steps = 0
        self.created = time.time()
        self.stopping = False
        self.finished = False
        self.summary = None
        self.pending = None            # {"call_id", "tool", "input", "event": Event, "approved": bool, "note"}
        self.inbox = []
        self.inbox_event = threading.Event()
        self.lock = threading.RLock()
        self.tools = registry()
        self.ctx = Context(self.studio, self)
        self.jobs = []
        self.job = None
        self.running = False
        self.asking = False
        self.last_change_error = None     # the latest change/deploy tool failure not yet fixed
        self.finish_warned = False
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    # ------------------------------------------------------------------ the project
    @property
    def kind(self):
        if not self.project:
            return None
        try:
            return self.studio.projects.get(self.project).kind
        except Exception:  # noqa: BLE001 - a deleted project
            return None

    def switch_project(self, pid):
        self.studio.projects.get(pid)
        self.project = pid
        self.emit("note", text=f"working on project {pid}")

    def offered(self):
        return [self.tools[n] for n in tools_for(self.kind) if n in self.tools]

    # ------------------------------------------------------------------ events
    def emit(self, kind, **data):
        ev = {"kind": kind, "t": time.time(), **data}
        with self.lock:
            self.events.append(ev)
        self.studio.bus.publish({"type": "agent", "session": self.id, "event": ev, "status": self.status})
        self.manager.persist(self)

    def note(self, text):
        self.emit("note", text=text)

    def set_status(self, st):
        self.status = st
        self.emit("status", status=st)

    def attach_job(self, job):
        self.jobs.append(job.id)
        self.emit("job", job=job.id, title=job.title)

    # ------------------------------------------------------------------ user side
    def post_message(self, text):
        """While a tool waits for the user (ask_user) the message answers it; otherwise it starts the
        next turn of the conversation."""
        if self.asking:
            with self.lock:
                self.inbox.append(text)
            self.inbox_event.set()
            return
        if self.running:
            with self.lock:
                self.inbox.append(text)       # read at the next step
            return
        self.emit("user", text=text)
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        self.finished = False
        self.manager.launch(self)

    def decide(self, call_id, approve, note=None):
        p = self.pending
        if not p or p["call_id"] != call_id:
            raise ValueError("nothing is waiting for that approval")
        p["approved"] = bool(approve)
        p["note"] = note
        p["event"].set()

    def stop(self):
        self.stopping = True
        self.inbox_event.set()
        if self.pending:
            self.pending["approved"] = False
            self.pending["event"].set()
        if self.job:
            self.job.cancel_event.set()

    # ------------------------------------------------------------------ tool helpers
    def ask_user(self, question):
        self.asking = True
        try:
            self.set_status("waiting_user")
            self.emit("question", text=question)
            answer = self._wait_message()
        finally:
            self.asking = False
        if answer is None:
            return [{"type": "text", "text": "(the session was stopped before the user answered)"}]
        self.set_status("running")
        return [{"type": "text", "text": f"The user answered: {answer}"}]

    def finish(self, summary):
        self.finished = True
        self.summary = summary
        self.emit("finished", text=summary)

    def _wait_message(self):
        """Block until the user writes (ask_user); None when the session is stopped."""
        while not self.stopping:
            if self.inbox_event.wait(0.5):
                msg = None
                with self.lock:
                    if self.inbox:
                        msg = self.inbox.pop(0)
                    if not self.inbox:
                        self.inbox_event.clear()
                if msg is not None:
                    self.emit("user", text=msg)
                    return msg
        return None

    def _needs_approval(self, risk):
        if risk == "read" or self.autonomy == "full":
            return False
        if self.autonomy == "auto_safe":
            return risk == "deploy"
        return True

    # ------------------------------------------------------------------ loop
    def run(self, job):
        """One turn: the model works until it has nothing more to do (or finishes, or is stopped)."""
        self.job = job
        self.running = True
        try:
            return self._run()
        finally:
            self.running = False

    def _run(self):
        self.set_status("running")
        if not self.messages:
            default = {"car": "Convert this car end to end and tell me what to check.",
                       "map": "Convert this map end to end and tell me what to check.",
                       "world": "Show me what this world edit changes and what to check."}.get(self.kind, "What can you do?")
            first = [{"type": "text", "text": self.goal or default}]
            if self.project:
                p = self.studio.projects.get(self.project)
                first.insert(0, {"type": "text", "text": f"Open project: {p.data.get('name')} (a {p.kind}, id {p.id})."})
            self.messages.append({"role": "user", "content": first})
            self.emit("user", text=self.goal)
        steps = 0
        while not self.stopping:
            if steps >= self.max_steps:
                self.emit("error", text=f"step limit ({self.max_steps}) reached; send a message to continue")
                break
            # messages the user sent while the model was working
            with self.lock:
                extra, self.inbox = self.inbox, []
            for t in extra:
                self.emit("user", text=t)
                self.messages.append({"role": "user", "content": [{"type": "text", "text": t}]})
            steps += 1
            self.steps += 1
            try:
                reply = self.provider.chat(build_system(self.kind), self.messages, [t.spec() for t in self.offered()])
            except ProviderError as e:
                self.emit("error", text=str(e))
                self.set_status("failed")
                return {"error": str(e)}
            for k in ("input_tokens", "output_tokens"):
                self.usage[k] += int((reply.usage or {}).get(k) or 0)
            self.messages.append({"role": "assistant", "content": reply.blocks})
            if reply.text:
                self.emit("assistant", text=reply.text)
            calls = reply.tool_calls
            if not calls:
                break
            results = []
            for call in calls:
                if self.stopping:
                    break
                results.append(self._call(call))
            # every tool call gets a result, even the ones not run
            done = {r["id"] for r in results}
            for call in calls:
                if call["id"] not in done:
                    results.append({"id": call["id"], "name": call["name"], "is_error": True,
                                    "content": [{"type": "text", "text": "not run: the session was stopped"}]})
            self.messages.append({"role": "tool", "results": results})
            if self.finished:
                break
        if self.stopping:
            self.set_status("stopped")
        else:
            self.set_status("finished" if self.finished else "waiting_user")
        return {"summary": self.summary, "steps": self.steps, "usage": self.usage}

    def _call(self, call):
        name, args = call["name"], call.get("input") or {}
        tool = self.tools.get(name)
        self.emit("tool_call", call_id=call["id"], tool=name, input=args)
        if tool is None:
            return self._result(call, f"unknown tool {name}", True)
        if name not in tools_for(self.kind):
            what = {"car": "a car", "map": "a map", "world": "a world edit"}.get(self.kind, "no")
            msg = f"{name} is not available in a session with {what} project (open_project switches)"
            if tool.risk in ("change", "deploy"):
                self.last_change_error = f"{name}: {msg}"
                self.finish_warned = False
            return self._result(call, msg, True)
        if self._needs_approval(tool.risk):
            ev = threading.Event()
            self.pending = {"call_id": call["id"], "tool": name, "input": args, "event": ev, "approved": False,
                            "note": None, "risk": tool.risk}
            self.set_status("waiting_approval")
            self.emit("approval", call_id=call["id"], tool=name, input=args, risk=tool.risk)
            while not ev.wait(0.5):
                if self.stopping:
                    break
            p, self.pending = self.pending, None
            self.emit("approval_result", call_id=call["id"], approved=p["approved"], note=p.get("note"))
            if not p["approved"]:
                self.set_status("running" if not self.stopping else "stopping")
                why = f" Their note: {p['note']}" if p.get("note") else ""
                return self._result(call, f"The user declined this call.{why}", True)
            self.set_status("running")
        t0 = time.time()
        try:
            content = tool.fn(self.ctx, args)
            is_error = False
        except ToolError as e:
            content, is_error = [{"type": "text", "text": f"error: {e}"}], True
        except Exception as e:  # noqa: BLE001 - reported to the model
            content, is_error = [{"type": "text", "text": f"error: {e.__class__.__name__}: {e}"}], True
        if tool.risk in ("change", "deploy"):
            if is_error:
                self.last_change_error = f"{name}: " + " ".join(b["text"] for b in content if b["type"] == "text")[:300]
                self.finish_warned = False
            else:
                self.last_change_error = None
        texts = [b["text"] for b in content if b["type"] == "text"]
        self.emit("tool_result", call_id=call["id"], tool=name, error=is_error, seconds=round(time.time() - t0, 1),
                  text=("\n".join(texts))[:3000], images=sum(1 for b in content if b["type"] == "image"))
        return {"id": call["id"], "name": name, "content": content, "is_error": is_error}

    def _result(self, call, text, err):
        self.emit("tool_result", call_id=call["id"], tool=call["name"], error=err, text=text, images=0)
        return {"id": call["id"], "name": call["name"], "content": [{"type": "text", "text": text}], "is_error": err}

    def view(self, since=0):
        with self.lock:
            evs = self.events[since:]
        return {"id": self.id, "project": self.project, "goal": self.goal, "status": self.status,
                "autonomy": self.autonomy, "provider": getattr(self.provider, "name", "?"),
                "model": getattr(self.provider, "model", None), "created": self.created, "steps": self.steps,
                "pending": ({k: v for k, v in self.pending.items() if k != "event"} if self.pending else None),
                "events": evs, "total_events": len(self.events), "usage": self.usage, "summary": self.summary}


class AgentManager:
    def __init__(self, studio, provider_factory=None):
        self.studio = studio
        self.sessions = {}
        self.factory = provider_factory or self._provider

    def _provider(self, name=None):
        ai = self.studio.settings.get()["ai"]
        name = name or ai["provider"]
        if name == "anthropic":
            return AnthropicProvider(self.studio.secrets.get("anthropic_api_key") or "", ai["anthropic_model"],
                                     ai["anthropic_base_url"])
        if name == "ollama":
            return OllamaProvider(ai["ollama_url"], ai["ollama_model"])
        raise ProviderError("no AI provider configured (Settings > AI)")

    def list_models(self, provider=None):
        try:
            return {"models": self.factory(provider).list_models()}
        except ProviderError as e:
            return {"models": [], "error": str(e)}

    def test(self, provider=None):
        try:
            p = self.factory(provider)
            model = p.pick_model() if hasattr(p, "pick_model") else p.model
            r = p.chat("Reply with the single word: ready", [{"role": "user", "content": [{"type": "text", "text": "ping"}]}], [])
            return {"ok": True, "model": model, "reply": r.text[:200]}
        except ProviderError as e:
            return {"ok": False, "error": str(e)}

    def start(self, project=None, goal="", autonomy=None, provider=None):
        ai = self.studio.settings.get()["ai"]
        if project:
            self.studio.projects.get(project)        # exists
        prov = self.factory(provider)
        s = Session(self, project, goal, prov, autonomy or ai["autonomy"], ai["max_steps"], ai.get("vision", True))
        self.sessions[s.id] = s
        self.launch(s)
        return s.view()

    def launch(self, s):
        if s.running:
            return
        s.stopping = False
        s.running = True              # set before the thread starts so a quick message queues
        job = self.studio.jobs.submit("agent", f"AI agent: {(s.goal or 'end-to-end build')[:60]}", s.run,
                                      project=s.project, meta={"session": s.id})
        job.on_cancel.append(s.stop)
        s.job = job

    def get(self, sid, since=0):
        s = self.sessions.get(sid)
        if not s:
            raise ValueError("no such session")
        return s.view(since)

    def list(self, project=None):
        return [{k: v for k, v in s.view(len(s.events)).items() if k != "events"}
                for s in sorted(self.sessions.values(), key=lambda x: -x.created)
                if not project or s.project == project]

    def message(self, sid, text):
        s = self.sessions.get(sid)
        if not s:
            raise ValueError("no such session")
        if not text.strip():
            raise ValueError("empty message")
        s.post_message(text.strip()[:8000])
        return {"ok": True}

    def approve(self, sid, call_id, approve, note=None):
        s = self.sessions.get(sid)
        if not s:
            raise ValueError("no such session")
        s.decide(call_id, approve, note)
        return {"ok": True}

    def stop(self, sid):
        s = self.sessions.get(sid)
        if not s:
            raise ValueError("no such session")
        s.stop()
        return {"ok": True}

    def persist(self, s):
        if not s.project:
            return
        try:
            p = self.studio.projects.get(s.project)
            d = p.path("agent")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{s.id}.json"), "w", encoding="utf-8") as f:
                json.dump({"id": s.id, "goal": s.goal, "created": s.created, "status": s.status, "events": s.events},
                          f, default=str)
        except (OSError, ValueError):
            pass
