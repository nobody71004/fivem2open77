// Live placement: a map on a running server, moved through the map manager's agent API (the
// Studio's SSH connection carries it).  The top-down canvas shows the spot it stands at now, the
// new spot, the previous one, saved presets, the other maps and the players; drag the map to move
// it, Shift+drag (or the right button) to turn it, arrows / Q E / PgUp PgDn to nudge.  Nothing in
// game moves until Apply: the server's map baker rebuilds the map for the new spot and the server
// restarts (players rejoin), exactly like Apply in the in-game panel (F9).
import { api, waitJob } from "../api.js";
import { store } from "../store.js";
import { h, clear, put, toast, confirmBox, prompt, fmtAgo, pill } from "../ui.js";
import { cityStage } from "./city3d.js";

const POLL_MS = 2000;
const STATE_LABEL = {
  requested: "waiting for the map baker", queued: "queued at the baker", baking: "the baker is building it",
  built: "built: waiting for the restart", restart_scheduled: "restart scheduled", waiting_empty: "waiting for the server to be empty",
  unstaging: "throwing the build away", applied: "applied", failed: "failed", cancelled: "cancelled",
};
const fmt = (a) => (a ? `${(+a.x).toFixed(1)}, ${(+a.y).toFixed(1)}, ${(+a.z).toFixed(1)} · ${(+(a.yaw || 0)).toFixed(0)}°` : "-");
const VIEW_KEY = "f2o77.live.view";

// a list from the map manager: Lua's JSON has no empty array, so "no saved spots yet" arrives as
// {} (and a list with holes as {"1": .., "3": ..}); older Studios looped over that and broke
export const arr = (v) => (Array.isArray(v) ? v : v && typeof v === "object"
  ? Object.keys(v).sort((a, b) => (+a) - (+b)).map((k) => v[k]) : []);
export function normMap(m) {
  if (m && typeof m === "object") { m.presets = arr(m.presets); m.history = arr(m.history); }
  return m;
}
function normLive(r) {
  if (r && r.status && typeof r.status === "object") r.status.maps = arr(r.status.maps).filter((m) => m && typeof m === "object").map(normMap);
  return r;
}

// the stage of the live page: Night City in 3D, or the plan (top-down canvas)
export function liveStage(opts = {}) {
  let want3d = true;
  try { want3d = (localStorage.getItem(VIEW_KEY) || "3d") === "3d"; } catch (_) { /* private mode */ }
  const plan = h("div.plan");
  const cityHost = h("div.city");
  const el = h("div.live-stage", plan, cityHost);
  const tab3d = h("button", { type: "button", onclick: () => setView(true) }, "3D city");
  const tab2d = h("button", { type: "button", onclick: () => setView(false) }, "Plan");
  const tabs = h("div.view-tabs", tab3d, tab2d);
  let city = null;
  let scene = null;
  function setView(on) {
    want3d = on;
    try { localStorage.setItem(VIEW_KEY, on ? "3d" : "plan"); } catch (_) { /* private mode */ }
    tab3d.classList.toggle("on", on);
    tab2d.classList.toggle("on", !on);
    plan.classList.toggle("hidden", on);
    cityHost.classList.toggle("hidden", !on);
    if (on && !city) {
      city = cityStage(cityHost, { onPick: opts.onPick, onNudge: opts.onNudge });
      if (scene) city.update(scene);
    }
    if (opts.onView) opts.onView(on);
  }
  setView(want3d);
  return {
    el, tabs, plan,
    is3d: () => want3d,
    update(s) { scene = s; if (city) city.update(s); },
    dispose() { if (city) city.dispose(); },
  };
}

export function placePanel(root, opts) {
  const servers = store.state.settings.servers;
  if (!servers.length) {
    root.append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "No servers yet"), h("span.muted", "Add your Open77 server in Servers, then install a map there: it shows up on the city."),
      h("div.grow"), h("a.btn.primary", { href: "#/servers" }, "Add a server"))));
    const st = liveStage();
    root.append(h("div", { style: { marginTop: "14px" } }, st.el));
    return () => st.dispose();
  }
  let disposed = false;
  let timer = null;
  let live = null;           // last /live answer
  let players = [];
  let playersAt = 0;
  let mapName = opts.mapName || opts.initialMap || null;
  let tile = null;           // the map's plan as an image (map projects)
  let tileFor = null;
  let busy = false;

  const srvSel = h("select", servers.map((s) => h("option", { value: s.id }, `${s.label} (${s.host})`)));
  if (opts.server && servers.find((s) => s.id === opts.server)) srvSel.value = opts.server;
  const folderSel = h("select");
  const mapSel = h("select", { class: opts.mapName ? "hidden" : "" });
  function fillFolders() {
    const s = servers.find((x) => x.id === srvSel.value);
    clear(folderSel).append(...s.names.map((n) => h("option", { value: n }, n)));
    if (opts.folder && s.names.includes(opts.folder)) folderSel.value = opts.folder;
  }
  fillFolders();
  const conn = h("span.muted", "connecting...");
  const banner = h("div");
  const canvas = h("canvas", { tabIndex: 0, style: { width: "100%", height: "100%", display: "block", outline: "none", cursor: "grab" } });
  const live3d = liveStage({
    onPick: (p) => {
      const m = current();
      if (!m) return toast("Pick a map on this server first", "err");
      const base = m.pending || m.running || {};
      op("place", { x: p.x, y: p.y, z: p.z, yaw: +(base.yaw || 0) }).then((r) => { if (r && r.ok) toast(`New spot set: ${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)} (nothing moves in game until Apply)`, "ok"); });
    },
    onNudge: (body) => op("move", body),
  });
  const stage = live3d.el;
  live3d.plan.append(canvas, h("div", { style: { position: "absolute", left: "10px", top: "8px", fontSize: "12px", color: "var(--faint)", pointerEvents: "none" } },
    "drag: move · Shift+drag or right button: turn · wheel: zoom · arrows / Q E / PgUp PgDn: nudge"));
  const coordsLbl = h("div", { style: { position: "absolute", right: "10px", top: "8px", fontSize: "12px", color: "var(--muted)", fontFamily: "var(--mono)" } });
  live3d.plan.append(coordsLbl);
  const side = h("div.col");
  root.append(
    h("div.card", h("div.row", h("b", "Server"), srvSel, folderSel, mapSel, conn, h("div.grow"), live3d.tabs,
      h("button.btn.small.ghost", { onclick: () => poll(true) }, "Refresh"))),
    banner,
    h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0, 1fr) 400px", gap: "14px", marginTop: "14px", alignItems: "start" } }, stage, side));

  // ---------------------------------------------------------------- data
  const sid = () => srvSel.value;
  const folder = () => folderSel.value;
  const q = () => `?name=${encodeURIComponent(folder())}`;
  const current = () => (live && live.status ? arr(live.status.maps).find((m) => m.name === mapName) : null);

  async function poll(force) {
    if (disposed) return;
    clearTimeout(timer);
    try {
      const r = normLive(await api.get(`/servers/${sid()}/live${q()}`));
      if (disposed) return;
      live = r;
      if (r.ok && !mapName && r.status.maps.length) mapName = r.status.maps[0].name;
      if (r.ok && Date.now() - playersAt > 5000) {
        playersAt = Date.now();
        api.get(`/servers/${sid()}/live/players${q()}`).then((p) => { players = arr(p && p.players).filter((x) => x && typeof x === "object"); safe(draw); }).catch(() => { players = []; });
      }
    } catch (e) {
      live = { ok: false, error: e.message };
    }
    // drawing is not the connection: a problem showing an answer must not read as "Not connected"
    safe(showState);
    if (!disposed) timer = setTimeout(poll, POLL_MS);
    if (force) toast("Refreshed", "ok");
  }

  let shownError = null;
  function safe(fn) {
    try {
      fn();
      if (shownError) { shownError.remove(); shownError = null; }
    } catch (e) {
      console.error("live maps:", e);
      if (!shownError) {
        shownError = h("div.banner", { style: { marginTop: "14px" } }, h("b", "Display problem"),
          h("span", `the server answered, but this page could not show it (${e.message}); the controls may be out of date`),
          h("div.grow"), h("button.btn.small", { onclick: () => location.reload() }, "Reload"));
        banner.after(shownError);
      }
    }
  }

  async function op(name, body = {}, quiet = false) {
    if (!mapName || busy) return null;
    busy = true;
    try {
      const r = await api.post(`/servers/${sid()}/live/maps/${mapName}/${name}${q()}`, body);
      if (!r.ok && !quiet) toast(`${name}: ${String(r.reason || "refused").replace(/_/g, " ")}`, "err");
      if (r.map && live && live.status) {
        const i = arr(live.status.maps).findIndex((m) => m.name === mapName);
        if (i >= 0) live.status.maps[i] = normMap(r.map);
      }
      safe(showState);
      return r;
    } catch (e) {
      toast(e.message, "err");
      return null;
    } finally {
      busy = false;
    }
  }

  // ---------------------------------------------------------------- the side panel
  function showState() {
    clear(banner);
    if (!live) return;
    if (!live.ok) {
      conn.replaceChildren(pill("failed"));
      const link = live.link || {};
      const fixes = [];
      if (link.exists && link.manager && link.http && !link.http.enabled) {
        fixes.push(h("button.btn.small.primary", { onclick: async () => {
          if (!(await confirmBox("Turn on the agent API", `Turn on the HTTP handler listener on ${folder()} (loopback only, reached through SSH)? The server restarts now if nobody is online, otherwise it is on from its next start.`, "Turn on"))) return;
          try {
            const job = await api.post(`/servers/${sid()}/agent-api`, { names: [folder()], enable: true, restart: true });
            toast("Turning on the agent API...");
            const j = await waitJob(job.id);
            toast(j.state === "done" ? "Agent API on" : `Failed: ${j.error}`, j.state === "done" ? "ok" : "err");
            poll();
          } catch (e) { toast(e.message, "err"); }
        } }, "Turn on the agent API"));
      }
      if (link.exists && !link.manager) fixes.push(h("a.btn.small", { href: opts.pid ? `#/map/${opts.pid}/deploy` : "#/maps" }, "Install a map there"));
      banner.append(h("div.banner", { style: { marginTop: "14px" } }, h("b", live.reachable === false ? "Server not answering" : "Not connected"),
        h("span", live.error || "unknown problem"), h("div.grow"), fixes));
    } else {
      conn.replaceChildren(pill("running"), h("span.muted", ` manager ${live.link.manager_version || ""}`));
    }
    // the map list (live page)
    if (!opts.mapName && live.ok) {
      const names = arr(live.status.maps).map((m) => m.name);
      if (mapSel.options.length !== names.length || [...mapSel.options].some((o, i) => o.value !== names[i])) {
        clear(mapSel).append(...names.map((n) => h("option", { value: n }, n)));
      }
      if (mapName) mapSel.value = mapName;
    }
    renderSide();
    loadTile();
    draw();
  }

  function renderSide() {
    clear(side);
    if (!live || !live.ok) {
      side.append(h("div.card", h("span.muted", live ? "Waiting for the server..." : "Connecting...")));
      return;
    }
    const m = current();
    if (!m) {
      side.append(h("div.card", h("h3", "Not on this server"), h("p.muted", `${mapName || "This map"} is not installed on ${folder()}.`),
        opts.pid ? h("a.btn.primary", { href: `#/map/${opts.pid}/deploy` }, "Install it") : null));
      return;
    }
    const a = m.apply || {};
    const st = live.status;
    const restart = st.restart || {};
    const inProgress = ["requested", "queued", "baking", "built", "restart_scheduled", "waiting_empty", "unstaging"].includes(a.state);
    const spot = m.pending || m.running || {};
    const f = {};
    const num = (k, step) => (f[k] = h("input", { type: "number", step, value: spot[k] != null ? (+spot[k]).toFixed(k === "yaw" ? 0 : 2) : "", style: { width: "100%" } }));
    const frame = h("select", h("option", { value: "world" }, "Night City axes"), h("option", { value: "map" }, "the map's axes"));
    const nudge = (label, body) => h("button.btn.small", { onclick: () => op("move", Object.assign({ frame: frame.value }, body)) }, label);
    const playerSel = h("select", players.length ? players.map((p) => h("option", { value: p.playerId }, `${p.name} (#${p.playerId})`))
      : h("option", { value: "" }, "nobody online"));
    const countdown = h("select", [30, 60, 120, 300].map((s) => h("option", { value: s }, `in ${s} s`)));
    countdown.value = "60";
    const mode = h("select", h("option", { value: "now" }, "restart"), h("option", { value: "empty" }, "when nobody is online"));
    put(side,
      h("div.card", h("h3", m.display || m.name),
        h("div.kv", h("span", "stands at"), h("span.mono", fmt(m.running)),
          h("span", "new spot"), h("span.mono", m.pending ? h("span", { style: { color: "var(--accent)" } }, fmt(m.pending)) : "-"),
          h("span", "previous"), h("span.mono", fmt(m.previous)),
          h("span", "build"), h("span.mono", m.runningId || "-")),
        a.state ? h("div", { style: { marginTop: "10px" } }, h("div.row", pill(a.state === "applied" ? "done" : a.state === "failed" ? "failed" : a.state === "cancelled" ? "cancelled" : "running"),
          h("b", STATE_LABEL[a.state] || a.state)), a.message ? h("div.help", a.message) : null,
          restart.scheduled ? h("div.help", `restart in ${restart.secondsRemaining} s`) : null) : null),
      h("div.card", h("h3", "New spot"),
        h("div.form", { style: { gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "8px 10px" } },
          h("label.field", "x (east)", num("x", 0.1)), h("label.field", "y (north)", num("y", 0.1)),
          h("label.field", "z (up)", num("z", 0.1)), h("label.field", "yaw ° (counter-clockwise)", num("yaw", 1))),
        h("div.row", { style: { marginTop: "8px" } },
          h("button.btn.small.primary", { disabled: inProgress, onclick: () => op("place", { x: +f.x.value, y: +f.y.value, z: +f.z.value, yaw: +f.yaw.value }) }, "Set"),
          h("button.btn.small", { disabled: !m.pending, onclick: () => op("discard") }, "Forget it"),
          h("button.btn.small", { disabled: !m.previous, onclick: () => op("revert") }, "Back to previous")),
        h("div.row", { style: { marginTop: "10px", gap: "4px" } }, h("span.muted", "move along"), frame),
        h("div", { style: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "4px", marginTop: "6px" } },
          nudge("x −10", { dx: -10 }), nudge("x −1", { dx: -1 }), nudge("x +1", { dx: 1 }), nudge("x +10", { dx: 10 }),
          nudge("y −10", { dy: -10 }), nudge("y −1", { dy: -1 }), nudge("y +1", { dy: 1 }), nudge("y +10", { dy: 10 }),
          nudge("z −1", { dz: -1 }), nudge("z −0.1", { dz: -0.1 }), nudge("z +0.1", { dz: 0.1 }), nudge("z +1", { dz: 1 }),
          nudge("⟲ 45°", { dyaw: 45 }), nudge("⟲ 5°", { dyaw: 5 }), nudge("⟳ 5°", { dyaw: -5 }), nudge("⟳ 45°", { dyaw: -45 })),
        h("div.row", { style: { marginTop: "10px" } },
          h("button.btn.small", { onclick: async () => {
            const r = await op("drop", {}, true);
            if (r && !r.ok) {
              toast(r.reason === "no_observer" ? "Dropping needs a player within 150 m of the spot: the server has no physics, a client measures the ground"
                : r.reason === "no_ground" ? "The player near the spot found no ground under it (water, a void, or not streamed yet): get closer and try again"
                  : `drop: ${String(r.reason).replace(/_/g, " ")}`, "err");
            }
          } }, "Drop onto the ground")),
        h("div.row", { style: { marginTop: "8px" } }, playerSel,
          h("button.btn.small", { disabled: !players.length, onclick: () => op("here", { playerId: +playerSel.value }) }, "To this player"),
          h("button.btn.small", { disabled: !players.length, onclick: () => op("goto", { playerId: +playerSel.value, which: m.pending ? "pending" : "running" }) }, "Send them there"))),
      h("div.card", h("h3", "Saved spots"),
        arr(m.presets).length ? h("table.tbl", h("tbody", arr(m.presets).map((p) => h("tr", h("td", h("b", p.name), h("div.mono.muted", { style: { fontSize: "11.5px" } }, fmt(p.anchor))),
          h("td", h("div.row", { style: { gap: "4px" } }, h("button.btn.small", { onclick: () => op("presetUse", { name: p.name }) }, "Use"),
            h("button.btn.small.ghost", { onclick: async () => {
              if (await confirmBox("Delete preset", `Delete the saved spot "${p.name}"?`, "Delete", "danger")) op("presetDelete", { name: p.name });
            } }, "×"))))))) : h("div.help", "none yet"),
        h("div.row", { style: { marginTop: "8px" } }, h("button.btn.small", { onclick: async () => {
          const n = await prompt("Save the spot", m.pending ? "Name for the new spot" : "Name for the spot it stands at", "");
          if (n && n.trim()) op("presetSave", { name: n.trim() });
        } }, m.pending ? "Save the new spot" : "Save this spot"))),
      h("div.card", h("h3", "Apply"),
        h("p.help", "The server's map baker builds the map for the new spot (seconds), then the server restarts and players rejoin with the new package."),
        h("div.row", mode, countdown,
          inProgress ? h("button.btn.danger", { onclick: async () => {
            if (await confirmBox("Cancel the move", "Call the restart off and throw the build away?", "Cancel the move", "danger")) op("cancel");
          } }, "Cancel the move")
            : h("button.btn.primary", { disabled: !m.pending, onclick: async () => {
              const txt = mode.value === "now"
                ? `Build ${m.name} at ${fmt(m.pending)} and restart ${folder()} ${countdown.options[countdown.selectedIndex].text}? ${players.length ? players.length + " player(s) online will be disconnected and rejoin." : "Nobody is online."}`
                : `Build ${m.name} at ${fmt(m.pending)} and restart ${folder()} as soon as nobody is online?`;
              if (await confirmBox("Apply the move", txt, "Apply")) {
                const r = await op("apply", { mode: mode.value, countdown: mode.value === "now" ? +countdown.value : undefined });
                if (r && r.ok) toast("Move requested: the baker builds it now", "ok");
              }
            } }, "Apply the move")),
        h("div.help", { style: { marginTop: "6px" } }, `baker: ${live.status.baker && live.status.baker.known ? (live.status.baker.version || "running") + (live.status.baker.hook ? ", start hook" : ", no start hook") : "not seen (moves wait for it)"}`)),
      arr(m.history).length ? h("div.card", h("h3", "Recent changes"), h("table.tbl", h("tbody", arr(m.history).slice(0, 8).map((x) =>
        h("tr", h("td.muted", x.at ? fmtAgo(x.at) : ""), h("td", x.what || ""), h("td.mono", fmt(x.anchor)), h("td.muted", x.by || "")))))) : null);
  }

  // ---------------------------------------------------------------- the canvas
  const view = { cx: 0, cy: 0, s: 1.2, init: false };
  let W = 0, H = 0;
  let drag = null;           // {mode: pan|move|turn, ...}
  const g = canvas.getContext("2d");

  function resize() {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    W = r.width; H = r.height;
    draw();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  const w2c = (x, y) => [(x - view.cx) * view.s + W / 2, H / 2 - (y - view.cy) * view.s];
  const c2w = (px, py) => [(px - W / 2) / view.s + view.cx, (H / 2 - py) / view.s + view.cy];

  function boundsOf(m) {
    if (opts.look && opts.look.visual_bounds) return opts.look.visual_bounds;
    const b = m && m.bounds;
    if (b && b.min && b.max) return [b.min, b.max];
    if (Array.isArray(b) && b.length === 2) return b;
    return [[-20, -20, 0], [20, 20, 10]];
  }
  function corners(a, b) {
    const r = (a.yaw || 0) * Math.PI / 180, c = Math.cos(r), s = Math.sin(r);
    return [[b[0][0], b[0][1]], [b[1][0], b[0][1]], [b[1][0], b[1][1]], [b[0][0], b[1][1]]]
      .map(([x, y]) => [a.x + c * x - s * y, a.y + s * x + c * y]);
  }
  let lookAsked = null;
  async function findLook() {
    // the live page: the Studio project of this map (if there is one) has its plan image and bounds
    if (!opts.lookFor || !mapName || lookAsked === mapName) return;
    lookAsked = mapName;
    try {
      const r = await opts.lookFor(mapName);
      if (lookAsked !== mapName) return;
      Object.assign(opts, r ? { pid: r.pid, look: r.look, lookKey: r.lookKey } : { pid: null, look: null, lookKey: null });
      tile = null; tileFor = null;
      loadTile();
      renderSide();
      draw();
    } catch (_) { /* no project: outlines only */ }
  }
  function loadTile() {
    findLook();
    if (!opts.pid || !opts.look || !opts.look.plan_frame || tileFor === opts.lookKey) return;
    tileFor = opts.lookKey;
    const im = new Image();
    im.onload = () => { tile = im; draw(); };
    im.src = `/api/projects/${opts.pid}/map/render.png?view=tile&mode=material&k=${opts.lookKey}`;
  }

  function gridStep() {
    const target = 70 / view.s;           // ~70 px between lines
    for (const st of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000]) if (st >= target) return st;
    return 5000;
  }

  function drawMap(m, a, style) {
    const b = boundsOf(m);
    if (tile && style.tile && opts.look) {
      const f = opts.look.plan_frame;
      const [px, py] = w2c(a.x, a.y);
      g.save();
      g.globalAlpha = style.tile;
      g.translate(px, py);
      g.rotate(-(a.yaw || 0) * Math.PI / 180);
      g.drawImage(tile, (f.cx - f.width / 2 * f.mpp) * view.s, -(f.cy + f.height / 2 * f.mpp) * view.s, f.width * f.mpp * view.s, f.height * f.mpp * view.s);
      g.restore();
    }
    const pts = corners(a, b).map(([x, y]) => w2c(x, y));
    g.save();
    g.strokeStyle = style.stroke;
    g.lineWidth = style.width || 1.5;
    if (style.dash) g.setLineDash(style.dash);
    if (style.fill) { g.fillStyle = style.fill; }
    g.beginPath();
    pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.closePath();
    if (style.fill) g.fill();
    g.stroke();
    // heading: from the anchor towards the map's own +y (north at yaw 0)
    const [ax, ay] = w2c(a.x, a.y);
    const r = (a.yaw || 0) * Math.PI / 180;
    const len = Math.max(18, Math.min(60, (b[1][1] - b[0][1]) * view.s * 0.35));
    g.setLineDash([]);
    g.beginPath();
    g.moveTo(ax, ay);
    g.lineTo(ax - Math.sin(r) * len, ay - Math.cos(r) * len);
    g.stroke();
    g.beginPath();
    g.arc(ax, ay, 4, 0, Math.PI * 2);
    g.fillStyle = style.stroke;
    g.fill();
    if (style.label) {
      g.font = "12px 'IBM Plex Sans', sans-serif";
      g.fillStyle = style.stroke;
      g.fillText(style.label, ax + 8, ay - 8);
    }
    g.restore();
  }

  // the same picture for the 3D city
  function scene3d() {
    const items = [];
    const ownBounds = (o) => {
      const b = o && o.bounds;
      if (b && b.min && b.max) return [b.min, b.max];
      if (Array.isArray(b) && b.length === 2) return b;
      return [[-20, -20, 0], [20, 20, 10]];
    };
    const m = current();
    if (live && live.ok && live.status) {
      for (const o of arr(live.status.maps)) {
        if (o.name === mapName || !o.running) continue;
        items.push({ key: `other:${o.name}`, map: o.name, name: o.display || o.name, role: "other", anchor: o.running, bounds: ownBounds(o) });
      }
    }
    if (m) {
      const b = boundsOf(m);
      const name = m.display || m.name;
      if (m.running) items.push({ key: `now:${m.name}`, map: m.name, name, role: "now", anchor: m.running, bounds: b });
      if (m.pending) items.push({ key: `new:${m.name}`, map: m.name, name, role: "new", anchor: m.pending, bounds: b, note: m.apply && m.apply.state ? (STATE_LABEL[m.apply.state] || m.apply.state) : "Apply to move it" });
      if (m.previous) items.push({ key: `prev:${m.name}`, map: m.name, name, role: "previous", anchor: m.previous, bounds: b });
      for (const p of arr(m.presets)) if (p && p.anchor) items.push({ key: `preset:${m.name}:${p.name}`, map: m.name, name: p.name, role: "preset", anchor: p.anchor, bounds: b });
    }
    return {
      focus: m ? m.name : null, items,
      players: arr(players).filter((p) => p && p.position).map((p) => ({ key: `p${p.playerId}`, name: p.name, x: +p.position.x, y: +p.position.y, z: +(p.position.z || 0) })),
    };
  }

  function draw() {
    live3d.update(scene3d());
    if (!W) return;
    g.clearRect(0, 0, W, H);
    const m = current();
    if (m && !view.init) {
      const a = m.pending || m.running;
      if (a) {
        const b = boundsOf(m);
        view.cx = a.x; view.cy = a.y;
        view.s = Math.max(0.05, Math.min(8, Math.min(W, H) * 0.55 / Math.max(10, b[1][0] - b[0][0], b[1][1] - b[0][1])));
        view.init = true;
      }
    }
    // grid
    const st = gridStep();
    const [x0, y1] = c2w(0, 0);
    const [x1, y0] = c2w(W, H);
    g.lineWidth = 1;
    g.font = "11px 'IBM Plex Mono', Consolas, monospace";
    for (let x = Math.floor(x0 / st) * st; x <= x1; x += st) {
      const [px] = w2c(x, 0);
      const major = Math.round(x / st) % 5 === 0;
      g.strokeStyle = major ? "#22252a" : "#16181b";
      g.beginPath(); g.moveTo(px + 0.5, 0); g.lineTo(px + 0.5, H); g.stroke();
      if (major) { g.fillStyle = "#4b4f57"; g.fillText(String(Math.round(x)), px + 3, H - 22); }
    }
    for (let y = Math.floor(y0 / st) * st; y <= y1; y += st) {
      const [, py] = w2c(0, y);
      const major = Math.round(y / st) % 5 === 0;
      g.strokeStyle = major ? "#22252a" : "#16181b";
      g.beginPath(); g.moveTo(0, py + 0.5); g.lineTo(W, py + 0.5); g.stroke();
      if (major) { g.fillStyle = "#4b4f57"; g.fillText(String(Math.round(y)), 4, py - 3); }
    }
    g.fillStyle = "#0b0c0e";
    g.fillRect(W - 118, H - 22, 118, 22);
    g.fillStyle = "#8d939b";
    g.fillText(`grid ${st} m · N ↑`, W - 110, H - 8);
    if (!live || !live.ok || !live.status) return;
    // other maps on the server
    for (const o of arr(live.status.maps)) {
      if (o.name === mapName || !o.running) continue;
      drawMap(o, o.running, { stroke: "#5b6068", dash: [3, 3], label: o.name });
    }
    if (!m) return;
    for (const p of arr(m.presets)) {
      if (!p || !p.anchor) continue;
      const [px, py] = w2c(p.anchor.x, p.anchor.y);
      g.save(); g.fillStyle = "#8d939b"; g.translate(px, py); g.rotate(Math.PI / 4); g.fillRect(-4, -4, 8, 8); g.restore();
      g.fillStyle = "#8d939b"; g.font = "11px 'IBM Plex Sans', sans-serif"; g.fillText(p.name, px + 7, py + 4);
    }
    if (m.previous) drawMap(m, m.previous, { stroke: "#6d7179", dash: [6, 5], width: 1, label: "previous" });
    const moving = drag && (drag.mode === "move" || drag.mode === "turn") ? drag.spot : null;
    if (m.running) drawMap(m, m.running, { stroke: "#4fb072", tile: m.pending || moving ? 0.35 : 0.9, label: "now" });
    const pend = moving || m.pending;
    if (pend) drawMap(m, pend, { stroke: "#e8b931", tile: 0.85, width: 2, fill: "rgba(232,185,49,0.05)", label: moving ? "drop to set" : "new spot" });
    for (const p of arr(players)) {
      if (!p || !p.position) continue;
      const [px, py] = w2c(p.position.x, p.position.y);
      g.beginPath(); g.arc(px, py, 5, 0, Math.PI * 2); g.fillStyle = "#6aa5e6"; g.fill();
      g.fillStyle = "#c9dcf2"; g.font = "12px 'IBM Plex Sans', sans-serif"; g.fillText(p.name, px + 8, py + 4);
    }
  }

  function inside(px, py, a, m) {
    const pts = corners(a, boundsOf(m)).map(([x, y]) => w2c(x, y));
    let c = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi) c = !c;
    }
    return c;
  }

  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("mousedown", (e) => {
    canvas.focus();
    const r = canvas.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const m = current();
    const base = m && (m.pending || m.running);
    const busyApply = m && m.apply && ["requested", "queued", "baking", "built", "restart_scheduled", "waiting_empty", "unstaging"].includes(m.apply.state);
    if (m && base && !busyApply && inside(px, py, base, m)) {
      const [wx, wy] = c2w(px, py);
      drag = { mode: e.shiftKey || e.button === 2 ? "turn" : "move", start: [wx, wy], base: Object.assign({}, base), spot: Object.assign({}, base),
        a0: Math.atan2(wy - base.y, wx - base.x) };
      canvas.style.cursor = drag.mode === "turn" ? "alias" : "move";
    } else {
      drag = { mode: "pan", px, py, cx: view.cx, cy: view.cy };
      canvas.style.cursor = "grabbing";
    }
  });
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  function onMove(e) {
    const r = canvas.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    if (px >= 0 && py >= 0 && px <= W && py <= H) {
      const [wx, wy] = c2w(px, py);
      coordsLbl.textContent = `${wx.toFixed(1)}, ${wy.toFixed(1)}`;
    }
    if (!drag) return;
    if (drag.mode === "pan") {
      view.cx = drag.cx - (px - drag.px) / view.s;
      view.cy = drag.cy + (py - drag.py) / view.s;
    } else {
      const [wx, wy] = c2w(px, py);
      if (drag.mode === "move") {
        drag.spot.x = Math.round((drag.base.x + wx - drag.start[0]) * 10) / 10;
        drag.spot.y = Math.round((drag.base.y + wy - drag.start[1]) * 10) / 10;
      } else {
        const a1 = Math.atan2(wy - drag.base.y, wx - drag.base.x);
        let yaw = drag.base.yaw + (a1 - drag.a0) * 180 / Math.PI;
        if (!e.altKey) yaw = Math.round(yaw / 5) * 5;
        drag.spot.yaw = ((yaw + 180) % 360 + 360) % 360 - 180;
      }
      coordsLbl.textContent = fmt(drag.spot);
    }
    draw();
  }
  function onUp() {
    if (!drag) return;
    const d = drag;
    drag = null;
    canvas.style.cursor = "grab";
    if (d.mode !== "pan" && (d.spot.x !== d.base.x || d.spot.y !== d.base.y || d.spot.yaw !== d.base.yaw)) {
      op("place", { x: d.spot.x, y: d.spot.y, z: d.spot.z, yaw: d.spot.yaw }).then(draw);
    }
    draw();
  }
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const [wx, wy] = c2w(px, py);
    view.s = Math.max(0.02, Math.min(20, view.s * Math.exp(-e.deltaY * 0.0015)));
    const [nx, ny] = c2w(px, py);
    view.cx += wx - nx; view.cy += wy - ny;
    draw();
  }, { passive: false });
  canvas.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
    const k = e.key;
    const body = k === "ArrowLeft" ? { dx: -step } : k === "ArrowRight" ? { dx: step } : k === "ArrowUp" ? { dy: step } : k === "ArrowDown" ? { dy: -step }
      : k === "PageUp" ? { dz: step } : k === "PageDown" ? { dz: -step } : (k === "q" || k === "Q") ? { dyaw: e.shiftKey ? 45 : 5 }
        : (k === "e" || k === "E") ? { dyaw: e.shiftKey ? -45 : -5 } : null;
    if (!body) return;
    e.preventDefault();
    op("move", body);
  });

  srvSel.addEventListener("change", () => { fillFolders(); live = null; view.init = false; players = []; if (!opts.mapName) mapName = null; poll(); });
  folderSel.addEventListener("change", () => { live = null; view.init = false; players = []; if (!opts.mapName) mapName = null; poll(); });
  mapSel.addEventListener("change", () => { mapName = mapSel.value; view.init = false; tile = null; tileFor = null; showState(); });
  poll();
  return () => {
    disposed = true;
    clearTimeout(timer);
    live3d.dispose();
    ro.disconnect();
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
  };
}
