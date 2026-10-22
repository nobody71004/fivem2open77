"""What the agent can do: the same operations the Studio's buttons call.

Each tool has a risk level that decides, with the session's autonomy, whether the user must
approve the call first:
    read    looks only                      never asks
    change  edits the project / builds      asks in "ask" mode
    deploy  touches a game server           asks unless autonomy is "full"
"""
from __future__ import annotations

import base64
import json
import re
import time

from ... import profile as profile_mod
from ...cp77 import sound as sound_mod

RISKS = ("read", "change", "deploy")


class ToolError(Exception):
    pass


class StillRunning(Exception):
    """A tool's job outlived the caller's patience (an MCP call answers before its client gives up):
    the job goes on; ``wait_job`` follows it."""

    def __init__(self, job):
        super().__init__(job.title)
        self.job = job


class Tool:
    def __init__(self, name, description, schema, risk, fn):
        self.name, self.description, self.schema, self.risk, self.fn = name, description, schema, risk, fn

    def spec(self):
        return {"name": self.name, "description": self.description, "schema": self.schema}


def _obj(props=None, required=None):
    return {"type": "object", "properties": props or {}, "required": required or [], "additionalProperties": False}


def text(s):
    return [{"type": "text", "text": s if isinstance(s, str) else json.dumps(s, indent=1, default=str)}]


class Context:
    """What a tool call gets: the Studio, the session's project, a logger, cancel check."""

    def __init__(self, studio, session):
        self.studio = studio
        self.session = session

    @property
    def project(self):
        pid = self.session.project
        if not pid:
            raise ToolError("this session has no project: list_projects and open_project, or import_resource")
        return self.studio.projects.get(pid)

    def log(self, msg):
        self.session.note(msg)

    def wait_job(self, job, timeout=3600):
        t0 = time.time()
        soft = getattr(self.session, "soft_timeout", None)
        asked = False
        while job.state in ("queued", "running"):
            if self.session.stopping and not asked:
                asked = True
                job.cancel()
                if getattr(job, "cancel_late", None) is None:
                    raise ToolError("stopped by the user")
                # a step that can't stop half-way (a server installing): the job stops after it; wait for that
            if time.time() - t0 > timeout:
                job.cancel()
                raise ToolError("timed out")
            if soft and time.time() - t0 > soft and not asked:
                raise StillRunning(job)
            time.sleep(0.25)
        if job.state != "done":
            tail = "\n".join(l["text"] for l in job.lines[-15:])
            raise ToolError(f"{job.title} {job.state}: {job.error or ''}\n{tail}")
        return job.result


# ----------------------------------------------------------------------------- read
def t_project(ctx, args):
    p = ctx.project
    s = p.summary()
    return text({"project": s["name"], "source": s["source"], "car": s["car"], "models": s["models"],
                 "builds": p.data.get("builds", [])[-5:], "deployments": p.data.get("deployments", [])[-5:],
                 "profile": p.raw_profile()})


def _compact_inspect(d, detail="summary"):
    out = {k: d[k] for k in ("model", "name", "display_name", "make", "class", "doors", "gta_sound", "dimensions",
                              "lids", "sound", "paint", "warnings", "models")}
    out["donor"] = d["donor"]["label"]
    out["record"] = f"Vehicle.{d['name']}"
    out["parts"] = {k: {"kind": v["kind"], "verts": v["verts"], "gta_bones": v["gta_bones"][:12]} for k, v in d["parts"].items()}
    out["handling"] = {"cyberpunk": {k: d["handling"]["cyberpunk"][k] for k in ("mass", "layout", "torque", "top_speed",
                                                                                  "gears", "brake", "grip", "steer")},
                       "gta": {k: d["handling"]["gta"][k] for k in ("fMass", "fInitialDriveForce", "fInitialDriveMaxFlatVel",
                                                                     "fBrakeForce", "fTractionCurveMax", "fDriveBiasFront",
                                                                     "nInitialDriveGears") if k in d["handling"]["gta"]}
                       if d["handling"].get("gta") else None}
    out["extras"] = d.get("extras")
    if d.get("extras_detail"):
        out["extras_detail"] = d["extras_detail"]
    if d.get("mods"):
        out["mods"] = d["mods"]
    if d.get("looks"):
        out["looks"] = d["looks"]
    out["lights"] = [{k: l[k] for k in ("name", "type", "part")} for l in d["lights"]]
    bones = d.get("bones", [])
    if detail == "bones":
        out["bones"] = [{k: b[k] for k in ("name", "parent", "auto", "role", "override", "part", "verts")} for b in bones]
    else:
        out["bones_with_geometry"] = [f"{b['name']}: {b['role']}{' (override)' if b['override'] else ''} -> {b['part']} ({b['verts']} v)"
                                      for b in bones if b["verts"]]
    out["textures"] = d["textures"]
    out["seating"] = d.get("seating")
    n = d.get("neon") or {}
    kit = n.get("kit")
    out["neon"] = {"enabled": n.get("enabled"), "gta_bones": n.get("gta_bones"),
                   "tubes": kit["tubes"] if kit else None, "colors": [c["key"] for c in kit["colors"]] if kit else None,
                   "default": kit["default"] if kit else None, "warnings": kit["warnings"] if kit else None}
    return out


def t_inspect(ctx, args):
    d = ctx.studio.inspect(ctx.project.id)
    return text(_compact_inspect(d, args.get("detail", "summary")))


def t_render(ctx, args):
    p = ctx.project
    s = ctx.studio.analyzer.session(p)
    mode = args.get("mode", "parts")
    views = args.get("views") or ["front_3q", "rear_3q"]
    blocks = [{"type": "text", "text": f"renders ({mode} colours; parts mode: body grey, doors blue, windows light blue, "
                                       "hood orange, trunk red, wheels silver, tyres black, lights yellow, interior brown, "
                                       "steering wheel purple, neon tubes magenta)"}]
    if not ctx.session.vision:
        return text("this model cannot see images; use inspect_car instead")
    for v in views[:4]:
        png = s.render(p.profile(), v, mode, (640, 400))
        blocks.append({"type": "text", "text": f"view {v}:"})
        blocks.append({"type": "image", "png": base64.b64encode(png).decode("ascii")})
    return blocks


def t_sounds(ctx, args):
    kind = args.get("kind")
    items = [s for s in sound_mod.choices() if not kind or s["kind"] == kind]
    return text([f"{s['name']} ({s['kind']}): {s['label']}" for s in items])


def t_check(ctx, args):
    p = ctx.project
    n = args.get("build") or (p.latest_build(ok_only=False) or {}).get("n")
    if not n:
        raise ToolError("no build yet")
    q = ctx.studio.run_qa(p.id, n)
    return text({"build": n, "ok": q["ok"], "failed": [c["message"] for c in q["checks"] if not c["ok"]],
                 "summary": q.get("summary")})


def t_servers(ctx, args):
    return text([{k: s[k] for k in ("id", "label", "host", "names")} for s in ctx.studio.redacted_settings()["servers"]])


def t_status(ctx, args):
    job = ctx.studio.server_job(args["server"], "status", names=args.get("names"))
    res = ctx.wait_job(job, 300)
    short = []
    for s in res:
        hub = s.get("hub") or {}
        st = hub.get("status") or {}
        mm = s.get("mapManager") or {}
        mb = s.get("mapBaker") or {}
        short.append({"server": s.get("name"), "open77": s.get("version"), "unit": s.get("unit"),
                      "online": len(s.get("online") or []), "cars": [{k: c.get(k) for k in ("name", "version", "record", "in_load")}
                                                                    for c in s.get("cars", [])],
                      "hub": {"installed": bool(hub), "waiting": [w.get("name") for w in st.get("waiting") or []],
                              "restart": st.get("restart")} if hub else None,
                      "maps": [{k: m.get(k) for k in ("name", "version", "anchor", "bakeId", "in_load")} for m in s.get("maps", [])],
                      "world_edits": [{k: w.get(k) for k in ("name", "version", "summary", "in_load")} for w in s.get("worlds", [])],
                      "map_manager": {"version": mm.get("version"), "in_load": mm.get("in_load"), "maps": mm.get("maps"),
                                      "started": mm.get("token")} if mm else None,
                      "map_baker": {"service": mb.get("service"), "toolkit": mb.get("toolkit"), "start_hook": mb.get("hook"),
                                    "staged": mb.get("staged")} if mb else None,
                      "agent_api": s.get("http")})
    return text(short)


def t_logs(ctx, args):
    out = ctx.studio.server_logs(args["server"], args["name"], int(args.get("lines", 200)))
    pat = args.get("filter")
    if pat:
        try:
            rx = re.compile(pat, re.I)
        except re.error as e:
            raise ToolError(f"bad filter: {e}")
        out = "\n".join(l for l in out.splitlines() if rx.search(l))
    return text(out[-12000:] or "(no matching lines)")


# ----------------------------------------------------------------------------- change
_NAME_MAPS = (("parts", "bone_roles"), ("parts", "extras"), ("parts", "mods"), ("lights",))


def _expand(patch: dict, path=()) -> dict:
    """Models often write {"sound.engine": x}: expand dotted keys into nested objects.  The keys
    of parts.bone_roles, parts.extras and lights are names and are left alone."""
    if path in _NAME_MAPS:
        return dict(patch)
    out = {}
    for k, v in patch.items():
        parts = str(k).split(".")
        full = path + tuple(parts)
        for i in range(1, len(full)):                 # a name map starts inside the dotted key
            if full[:i] in _NAME_MAPS and len(path) < i < len(full):
                parts = list(parts[: i - len(path)]) + [".".join(full[i:])]
                break
        here = path + tuple(parts[:-1])
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if isinstance(v, dict):
            v = _expand(v, here + (parts[-1],))
        if isinstance(v, dict) and isinstance(cur.get(parts[-1]), dict):
            cur[parts[-1]].update(v)
        else:
            cur[parts[-1]] = v
    return out


def _sound_name(value):
    """Accept a sound's label ("Rayfield Aerondight") or a name in any case."""
    if not isinstance(value, str) or value in ("auto", "donor") or value in sound_mod.CATALOGUE:
        return value
    low = value.strip().lower()
    for name, s in sound_mod.CATALOGUE.items():
        if low in (name.lower(), s["label"].lower()):
            return name
    return value


def t_update_profile(ctx, args):
    patch = args.get("patch")
    if not isinstance(patch, dict):
        raise ToolError("patch must be an object")
    patch = _expand(patch)
    if isinstance(patch.get("sound"), dict) and "engine" in patch["sound"]:
        patch["sound"]["engine"] = _sound_name(patch["sound"]["engine"])
    p = ctx.project
    before = p.profile()
    try:
        norm = p.patch_profile(patch)
    except profile_mod.ProfileError as e:
        raise ToolError("invalid profile: " + "; ".join(e.errors))
    changed = {k: norm[k] for k in norm if norm[k] != before.get(k)}
    ctx.log(f"profile updated: {', '.join(changed) or 'no change'}")
    return text({"saved": True, "changed": changed})


def t_build(ctx, args):
    job = ctx.studio.build(ctx.project.id)
    ctx.session.attach_job(job)
    info = ctx.wait_job(job, 3600)
    return text({k: info.get(k) for k in ("n", "ok", "version", "name", "record", "resources", "packages", "warnings",
                                         "seconds", "sound", "qa")})


# ----------------------------------------------------------------------------- deploy
def t_deploy(ctx, args):
    job = ctx.studio.deploy(ctx.project.id, args["server"], names=args.get("names"), mode=args.get("mode", "stage"),
                            build=args.get("build"), include_hub=bool(args.get("include_hub")))
    ctx.session.attach_job(job)
    res = ctx.wait_job(job, 1800)
    return text({k: res.get(k) for k in ("names", "mode", "staged", "live", "pending", "started")})


def t_restart(ctx, args):
    job = ctx.studio.server_job(args["server"], "restart", names=args.get("names"))
    ctx.session.attach_job(job)
    ctx.wait_job(job, 900)
    return text("restarted; read the server log to confirm the resources started")


# ----------------------------------------------------------------------------- conversation
def t_ask(ctx, args):
    return ctx.session.ask_user(str(args.get("question", "")).strip()[:2000])


def t_finish(ctx, args):
    err = ctx.session.last_change_error
    if err and not ctx.session.finish_warned:
        ctx.session.finish_warned = True
        raise ToolError(f"your last change did not go through ({err}). Fix it, or call finish again and say "
                        "plainly in the summary that it failed.")
    ctx.session.finish(str(args.get("summary", "")).strip()[:4000])
    return text("session finished")


CAR_TOOLS = ("get_project", "inspect_car", "render_car", "list_engine_sounds", "update_profile", "build_car",
             "check_build", "deploy_car")
COMMON_TOOLS = ("list_servers", "server_status", "read_server_log", "restart_server", "ask_user", "finish")


def registry() -> dict:
    roles = ", ".join(profile_mod.ROLE_VALUES)
    tools = [
        Tool("get_project", "The open project: source file, car, recent builds and deployments, and the saved profile.",
             _obj(), "read", t_project),
        Tool("inspect_car", "What the converter sees in the FiveM car: parts with vertex counts and the GTA bones in "
             "each, GTA bones with their automatic and effective roles, lights, hood/trunk mapping, wheels, handling "
             "(GTA values and the Cyberpunk numbers derived), engine sound, paint, textures, warnings. "
             "detail='bones' lists every bone.",
             _obj({"detail": {"type": "string", "enum": ["summary", "bones"]}}), "read", t_inspect),
        Tool("render_car", "Render the car as the conversion will assemble it. mode 'parts' colours each part kind "
             "(check doors, hood, trunk, wheels, windows are what they should be); 'material' shows colours/textures. "
             "Views: front_3q, rear_3q, left, right, front, rear, top, and from below: bottom, under_3q, "
             "under_rear_3q (the neon kit's tubes are magenta in parts mode).",
             _obj({"mode": {"type": "string", "enum": ["parts", "material"]},
                   "views": {"type": "array", "items": {"type": "string", "enum": ["front_3q", "rear_3q", "left", "right",
                                                                                 "front", "rear", "top", "bottom",
                                                                                 "under_3q", "under_rear_3q"]},
                             "maxItems": 4}}),
             "read", t_render),
        Tool("list_engine_sounds", "Cyberpunk engine sounds a car can use (names for profile sound.engine).",
             _obj({"kind": {"type": "string"}}), "read", t_sounds),
        Tool("update_profile", "Change the conversion profile with a JSON merge patch (null removes an entry). Keys: "
             "display_name (the car's name players see, e.g. 'BMW i8 Roadster'), name (record id: lower-case a-z0-9_, "
             "leave it unless asked), model (ONLY to choose one model file of a multi-car resource; never a car name), "
             "donor, parts.lids (auto|semantic|swap), parts.bone_roles {gta_bone: role} with "
             f"roles {roles}, parts.extras {{extra_n: true}}, parts.mods {{kit_part_model: true}}, lights {{name: {{color '#rrggbb', type, skip}}}}, "
             "handling {drive auto|rwd|fwd|awd, gears 4-7, mass kg, steer deg, multipliers {torque, top_speed, brake, "
             "grip, spring, drag: 0.25-4}}, sound.engine (auto|donor|name), paint.primary (auto|#rrggbb), "
             "chime {enabled, volume 0-1, range m}, neon {enabled, sides [left,right,front,back], colors ('gta' "
             "or a list of GTA colour names / '#rrggbb' / {name, color}), default ('on' = first colour, 'off', or a colour), brightness "
             "0.2-4 (how strongly the tubes light the road), spread 0.5-5 m (how far the glow reaches, default 2), tubes (false = light only like GTA V, true = visible glowing LED strips), "
             "height_offset -0.1..0.1 m}, seats {template auto|sport1|sport2|standard2|standard25|standard3, "
             "lower 0-0.2 m}. Returns what changed.",
             _obj({"patch": {"type": "object"}}, ["patch"]), "change", t_update_profile),
        Tool("build_car", "Build the car with WolvenKit (2-5 minutes) and check the output. Returns the build number, "
             "packages, warnings and QA result.", _obj(), "change", t_build),
        Tool("check_build", "Re-run the output checks on a build (latest by default).",
             _obj({"build": {"type": "integer"}}), "read", t_check),
        Tool("list_servers", "Open77 servers configured in the Studio.", _obj(), "read", t_servers),
        Tool("server_status", "What a server runs: Open77 version, players online, converted cars, the car hub and its "
             "queue, converted maps and where they are baked, world edits, the map manager, the map baker (service, toolkit, start hook, "
             "staged moves) and the agent API (httpHandlers).",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}}}, ["server"]),
             "read", t_status),
        Tool("read_server_log", "The end of a server's log (server.log), optionally only lines matching a regex filter.",
             _obj({"server": {"type": "string"}, "name": {"type": "string"}, "lines": {"type": "integer"},
                   "filter": {"type": "string"}}, ["server", "name"]), "read", t_logs),
        Tool("deploy_car", "Install the latest (or given) build on a server. mode 'stage' (default): installed now, the "
             "car hub restarts the server when nobody is online; 'restart': restart now if nobody is online; 'files': "
             "install only. include_hub also installs/updates the car hub.",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}},
                   "mode": {"type": "string", "enum": ["stage", "restart", "files"]}, "build": {"type": "integer"},
                   "include_hub": {"type": "boolean"}}, ["server"]), "deploy", t_deploy),
        Tool("restart_server", "Restart Open77 servers now (refused while players are online).",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}}}, ["server"]),
             "deploy", t_restart),
        Tool("ask_user", "Ask the user a question and wait for the answer (use when a choice is theirs).",
             _obj({"question": {"type": "string"}}, ["question"]), "read", t_ask),
        Tool("finish", "End the session with a short summary of what was done and what the user should check in game.",
             _obj({"summary": {"type": "string"}}, ["summary"]), "read", t_finish),
    ]
    out = {t.name: t for t in tools}
    from .maptools import registry as map_registry
    from .worldtools import registry as world_registry
    out.update(map_registry())
    out.update(world_registry())
    from .ncwe import registry as ncwe_registry
    out.update(ncwe_registry())
    return out


def tools_for(kind: str | None) -> tuple:
    """The tool names a session offers the model for its project kind (car, map, world, or None)."""
    from .maptools import CITY_TOOLS, LIVE_TOOLS, MAP_TOOLS, STUDIO_TOOLS
    from .worldtools import WORLD_TOOLS
    from .ncwe import NCWE_TOOLS
    if kind == "car":
        return CAR_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + LIVE_TOOLS + CITY_TOOLS + NCWE_TOOLS
    if kind == "map":
        return MAP_TOOLS + LIVE_TOOLS + CITY_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + NCWE_TOOLS
    if kind == "world":
        return WORLD_TOOLS + COMMON_TOOLS + STUDIO_TOOLS + LIVE_TOOLS + CITY_TOOLS + NCWE_TOOLS
    return STUDIO_TOOLS + COMMON_TOOLS + LIVE_TOOLS + CITY_TOOLS + ("new_world_edit", "night_city", "world_status",
                                                                    "world_find", "world_prepare", "remove_world_edit") + NCWE_TOOLS
