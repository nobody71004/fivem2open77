// World edits: Night City's own objects around a spot -- deleted, moved, given another mesh -- and
// objects of the game added, with the collision that belongs to each object following it.  An edit
// builds into a resource every player of a server gets (an ArchiveXL package).
//
//   #/world           the world edits, the index of Night City's sectors, a new edit
//   #/world/<id>      the editor: objects around a spot (left), the plan (middle), the selection (right)
import { api, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, put, toast, modal, confirmBox, prompt, fmtAgo, fmtBytes, pill, logLines, icon, setCrumbs } from "../ui.js";
import { assetsTab } from "./map.js";

const KINDS = [
  ["mesh", "Meshes"], ["instanced", "Instanced"], ["terrain", "Terrain"], ["collision", "Collision"], ["entity", "Entities"],
  ["decal", "Decals"], ["light", "Lights"], ["foliage", "Foliage"], ["occluder", "Occluders"], ["effect", "Effects"],
  ["road", "Roads"], ["other", "Other"],
];
const COLORS = { mesh: "#b9bdc4", instanced: "#8d939b", terrain: "#5f9a6c", collision: "#6aa5e6", entity: "#c98f52",
  decal: "#9a8fd0", light: "#d9c873", foliage: "#6f9a5a", occluder: "#5b6068", effect: "#c47ab0", road: "#7c8a99",
  spawner: "#8a8a8a", water: "#4f86b8", traffic: "#6b7480", other: "#6d7179" };
const DEFAULT_SHOWN = new Set(["mesh", "instanced", "terrain", "entity", "decal", "foliage", "road"]);

export function worldPage(root, args) {
  if (args[0]) return worldEditor(root, args[0]);
  return worldList(root);
}

// ------------------------------------------------------------------ the list
function worldList(root) {
  const list = h("div");
  const indexBox = h("div");
  const count = h("span.sub");

  async function loadIndex() {
    clear(indexBox);
    let st;
    try { st = await api.get("/world/status"); } catch (e) { indexBox.append(h("div.card.err", e.message)); return; }
    const card = h("div.card");
    card.append(h("h3", icon("layers"), "Night City index"));
    if (st.ready) {
      card.append(h("div.row", h("span.pill.ok", "ready"),
        h("span.muted", `${st.sectors.toLocaleString()} sectors listed · ${st.cached} read so far (each area is read from the game the first time you look at it)`)));
    } else {
      const can = st.game && st.wolvenkit;
      const out = h("div");
      card.append(h("div.row", h("span.pill.warn", "not read yet"),
        h("span.muted", can ? "The list of Night City's sectors is read from your game once (a minute or two)."
          : "Needs the Cyberpunk 2077 folder and WolvenKit (Settings > Tools)."),
        h("div.grow"),
        can ? h("button.btn.primary", { onclick: async () => {
          const job = await api.post("/world/prepare", {});
          const log = h("div.log", { style: { maxHeight: "160px", marginTop: "10px" } });
          clear(out).append(log);
          const j = await waitJob(job.id, (jj) => clear(log).append(...logLines(jj.allLines)));
          if (j.state === "done") { toast("Night City's sectors are listed", "ok"); loadIndex(); }
        } }, "Read it now") : h("a.btn", { href: "#/settings/tools" }, "Open setup")), out);
    }
    indexBox.append(card);
  }

  const cityBox = h("div", { style: { marginTop: "12px" } });
  async function loadCity() {
    let st;
    try { st = await api.get("/world/city"); } catch (e) { clear(cityBox); return; }
    const off = st.off || [];
    const card = h("div.card");
    card.append(h("h3", icon("layers"), "Night City"));
    if (!off.length) {
      card.append(h("div.row", h("span.pill.ok", "on"),
        h("span.muted", "Turn the whole city off — its terrain, roads, buildings, interiors and quest content — and keep only the maps you convert and the objects edits add."),
        h("div.grow"),
        h("button.btn", { onclick: turnOff }, "Turn Night City off…")));
    } else {
      for (const o of off) {
        const where = [o.game ? "your game" : null, ...(o.servers || [])].filter(Boolean);
        card.append(h("div.row", { style: { marginBottom: "6px" } }, h("span.pill.warn", "off"),
          h("span", h("b", o.display || o.name), " ", h("code", o.name)),
          h("span.muted", o.build ? (o.build.ok ? `build ${o.build.n}` : `build ${o.build.n} failed`) : "not built yet"),
          h("span.muted", where.length ? `· off in ${where.join(", ")}` : "· not installed anywhere yet"),
          h("div.grow"), h("a.btn.small", { href: `#/world/${o.project}` }, "Open")));
      }
      card.append(h("p.help", "Night City is off wherever one of these is installed. Remove it there (Remove from my game, or the server's World edits) to bring the city back."));
    }
    clear(cityBox).append(card);
  }

  async function turnOff() {
    const body = h("div.col",
      h("p", "This makes a world edit, ", h("code", "nightcity_off"), ", that replaces the game's two streaming blocks (Night City and Dogtown) with ones that list none of the city's sectors, and switches off the distant lights of its skyline."),
      h("p", "Wherever it is installed nothing of the city streams in: no ground, roads, buildings, interiors, props or quest content. The maps you convert and objects edits add still appear, so put the players on a map (a spot with a map under it), or they fall."),
      h("p.muted", "Build it, then Test in my game or Install on a server. Removing it brings the city back (players: after the server restarts and they launch again)."));
    const r = await modal("Turn Night City off", body, [{ label: "Cancel", value: null }, { label: "Make the edit", value: "ok", kind: "primary" }]);
    if (r !== "ok") return;
    try {
      const res = await api.post("/projects", { kind: "world", name: "nightcity_off", display: "Night City off", city: "off" });
      location.hash = `#/world/${res.project.id}`;
    } catch (e) { toast(e.message, "err"); }
  }

  async function load() {
    const st = await refreshState();
    clear(list);
    const edits = st.projects.filter((p) => p.kind === "world");
    count.textContent = edits.length ? `${edits.length} edit${edits.length === 1 ? "" : "s"}` : "";
    if (!edits.length) {
      list.append(h("div.empty", "No world edits yet. Start one: name it after what it changes and where."));
      return;
    }
    const body = h("tbody");
    for (const p of edits) {
      const w = p.world || {};
      const lb = p.last_build;
      const deps = p.deployments || [];
      const byServer = new Map();
      for (const d of deps) byServer.set(d.server, d);          // the latest per server
      const on = [...byServer.values()].filter((d) => !d.removed);
      const last = deps.length ? deps[deps.length - 1] : null;
      body.append(h("tr.link", { onclick: () => { location.hash = `#/world/${p.id}`; } },
        h("td", h("div.title", p.name || "(unnamed)", w.city === "off" ? h("span.pill.warn", { style: { marginLeft: "8px" } }, "Night City off") : null),
          h("div.sub", h("code", w.name || ""))),
        h("td.num", `${w.edits || 0}`),
        h("td", lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built")),
        h("td.muted", on.length ? `${on.map((d) => d.server).join(", ")} · ${fmtAgo(Math.max(...on.map((d) => d.time)))}`
          : last ? `removed from ${last.server} · ${fmtAgo(last.time)}` : "—"),
        h("td.muted.num", fmtAgo(p.updated))));
    }
    list.append(h("div.card.table-card", h("table.tbl.list",
      h("thead", h("tr", h("th", "World edit"), h("th.num", "Edits"), h("th", "Build"), h("th", "Installed on"), h("th.num", "Updated"))),
      body)));
  }

  async function create() {
    const display = h("input", { type: "text", placeholder: "e.g. Clear the lot at Cedar Crest", style: { width: "100%" } });
    const name = h("input", { type: "text", placeholder: "made from the name", style: { width: "100%" } });
    const x = h("input", { type: "number", step: "0.1", placeholder: "x" });
    const y = h("input", { type: "number", step: "0.1", placeholder: "y" });
    const z = h("input", { type: "number", step: "0.1", placeholder: "z" });
    const body = h("div.col",
      h("label.field", "Name", display),
      h("label.field", "Resource id (lower case letters, digits, _)", name),
      h("label.field", "Start at (Night City metres; optional)", h("div.row", x, y, z)));
    const r = await modal("New world edit", body, [{ label: "Cancel", value: null }, { label: "Create", value: "ok", kind: "primary" }]);
    if (r !== "ok") return;
    const center = x.value && y.value ? [Number(x.value), Number(y.value), Number(z.value || 0)] : undefined;
    try {
      const res = await api.post("/projects", { kind: "world", display: display.value.trim(), name: name.value.trim() || undefined, center });
      location.hash = `#/world/${res.project.id}`;
    } catch (e) { toast(e.message, "err"); }
  }

  root.append(h("div.page-head", h("h1", "World edits"), count, h("div.grow"),
    h("button.btn.ghost.icon-only", { onclick: load, title: "Refresh" }, icon("refresh")),
    h("button.btn.primary", { onclick: create }, icon("plus"), "New world edit")),
    h("p.muted", { style: { maxWidth: "860px" } },
      "Change Night City itself: delete, move or re-mesh its own objects and ground pieces, add objects of the game. ",
      "The collision of each object goes with it. An edit installs on a server like a car: players get it at their next launch."),
    indexBox, cityBox, h("div", { style: { height: "12px" } }), list);
  loadIndex();
  loadCity();
  load().catch((e) => toast(e.message, "err"));
  return {};
}

// ------------------------------------------------------------------ the editor
function worldEditor(root, pid) {
  root.className = "flush";
  const S = {
    state: null, objects: [], byKey: new Map(), sel: [], hover: null, shown: new Set(DEFAULT_SHOWN),
    view: { cx: 0, cy: 0, scale: 4 }, query: null, tool: "select", drag: null, outTab: "objects", filter: "",
    selInfo: null, busy: false, disposers: [],
  };
  const head = h("div.we-head");
  const outliner = h("aside.outliner.we-out");
  const stage = h("section.we-stage");
  const inspector = h("aside.inspector");
  const canvas = h("canvas.we-canvas", { tabindex: "0" });
  const tip = h("div.we-tip.hidden");
  const status = h("div.we-status");
  const fx = h("input", { type: "number", step: "0.1", title: "x (east)" });
  const fy = h("input", { type: "number", step: "0.1", title: "y (north)" });
  const fz = h("input", { type: "number", step: "0.1", title: "z (up): used for added objects" });
  const fr = h("select", { title: "radius" }, [10, 20, 30, 50, 80, 120].map((r) => h("option", { value: r }, `${r} m`)));
  fr.value = "30";
  const ftext = h("input", { type: "search", placeholder: "name or resource", style: { width: "130px" } });
  const findBtn = h("button.btn", { onclick: () => find() }, icon("search"), "Find");
  const toolSeg = h("div.seg", [["select", "Select", "select"], ["move", "Move", "move"], ["add", "Add", "plus"]].map(([k, l, ic]) =>
    h("button", { "data-tool": k, title: { select: "Select (S): click, shift+click adds; drag pans",
      move: "Move (M): drag the selected object; Q / E turn it by 15°", add: "Add (A): click to place the object set in the panel" }[k],
    onclick: () => setTool(k) }, icon(ic, "sm"), l)));
  const addForm = { res: h("input", { type: "text", placeholder: "base\\...\\something.mesh or .ent", style: { width: "100%" } }),
    yaw: h("input", { type: "number", step: "5", value: "0" }), app: h("input", { type: "text", placeholder: "default" }),
    col: h("select", h("option", { value: "mesh" }, "its own shapes"), h("option", { value: "none" }, "none")) };

  stage.append(
    h("div.we-bar", h("span.lbl", "X"), fx, h("span.lbl", "Y"), fy, h("span.lbl", "Z"), fz, fr, findBtn,
      h("span.sep"), ftext, h("span.sep"), toolSeg, h("div.grow"),
      h("button.btn.ghost.small", { title: "Clear an area: delete everything of some kinds around the spot (you see the list first)",
        onclick: () => clearArea() }, icon("trash"), "Clear…")),
    h("div.we-canvas-wrap", canvas, tip, h("div.we-legend"), status));
  root.append(h("div.world-editor", head, h("div.workspace.with-inspector.we-body", outliner, stage, inspector)));

  // ---------------------------------------------------------------- state
  async function reload() {
    S.state = await api.get(`/projects/${pid}/world`);
    renderHead();
    renderOutliner();
    renderInspector();
    draw();
    return S.state;
  }

  function renderHead() {
    const st = S.state;
    const p = st.project;
    setCrumbs([["World", "#/world"], [p.name || "(unnamed)"]]);
    const sm = st.summary || {};
    const lb = st.builds[0];
    const game = st.game || {};
    const city = st.city || "on";
    const citySeg = h("div.seg", { title: "Night City itself: off replaces the game's streaming blocks with ones without the city's sectors (wherever this edit is installed)" },
      ["on", "off"].map((v) => h("button", { class: city === v ? "on" : "", onclick: () => setCity(v) }, v === "on" ? "City on" : "City off")));
    clear(head).append(
      h("div.title", h("b", p.name || "(unnamed)"), h("code", st.name),
        h("span.muted", `${sm.deleted || 0} deleted · ${sm.elementsDeleted || 0} parts · ${(sm.changed || 0) + (sm.elementsMoved || 0)} moved/changed · ${sm.added || 0} added`),
        city === "off" ? h("span.pill.warn", { title: "None of Night City's own sectors stream in where this edit is installed" }, "Night City off") : null),
      h("div.grow"),
      h("div.toolbar",
        h("button.btn.ghost.small", { disabled: !st.can_undo, title: "Undo (Ctrl+Z)", onclick: () => undo(false) }, "Undo"),
        h("button.btn.ghost.small", { disabled: !st.can_redo, title: "Redo (Ctrl+Y)", onclick: () => undo(true) }, "Redo"),
        h("span.sep"),
        h("button.btn.ghost.small", { title: "Props made in NCWE that ship with this edit", onclick: assetsPanel }, "NCWE assets"),
        citySeg,
        h("span.sep"),
        h("button.btn.ghost.small", { onclick: rename }, "Rename"),
        h("button.btn.ghost.small", { onclick: remove }, icon("trash")),
        h("span.sep"),
        st.build_stale ? h("span.pill.warn", { title: st.build_stale.reason }, "imports changed") : null,
        lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built"),
        h("button.btn.small", { disabled: !lb || !lb.ok || !game.game, title: game.files && game.files.length ? "Remove it from your game" : "Write it in your game's archive/pc/mod for a test boot",
          onclick: () => gameInstall(!!(game.files && game.files.length)) }, icon("game"), game.files && game.files.length ? "In my game ✓" : "Test in my game"),
        h("button.btn.small", { disabled: !lb || !lb.ok, onclick: deploy }, icon("deploy"), "Install on a server…"),
        h("button.btn.primary.small", { disabled: !(st.edits || []).length && city !== "off", onclick: build }, icon("build"), "Build")));
  }

  async function setCity(v) {
    if ((S.state.city || "on") === v) return;
    if (v === "off" && !(await confirmBox("Turn Night City off",
      "Where this edit is installed, none of Night City's own sectors stream in: no ground, roads, buildings, interiors or quest content (maps and added objects stay). Build and install it to take effect; remove it to bring the city back.",
      "Turn it off", "danger"))) return;
    try {
      await api.patch(`/projects/${pid}/world`, { city: v });
      toast(v === "off" ? "Night City off: build and install the edit" : "Night City on: build and install again", "ok");
      reload();
    } catch (e) { toast(e.message, "err"); }
  }

  // the NCWE imports this edit ships with (the same tab as a map's)
  function assetsPanel() {
    const body = h("div.col");
    modal("NCWE assets", body, [{ label: "Done", value: null }]).then(() => reload());
    assetsTab(body, { pid, kind: "world" });
  }

  async function rename() {
    const n = await prompt("Rename", "Name of this world edit", S.state.display);
    if (n) { await api.patch(`/projects/${pid}/world`, { display: n }); reload(); }
  }

  async function remove() {
    if (await confirmBox("Delete world edit", `Delete ${S.state.display} and its builds from the Studio? Servers and your game are not touched.`, "Delete", "danger")) {
      await api.del(`/projects/${pid}`);
      location.hash = "#/world";
    }
  }

  // ---------------------------------------------------------------- finding objects
  function key(o) { return `${o.sector.toLowerCase()}|${o.index}|${o.element == null ? "" : o.element}`; }

  async function find(opts = {}) {
    const x = Number(fx.value), y = Number(fy.value);
    if (!isFinite(x) || !isFinite(y) || fx.value === "" || fy.value === "") { toast("Give a spot: x and y in Night City metres", "err"); return; }
    const r = Number(fr.value);
    S.busy = true;
    status.textContent = "Reading the sectors here from the game (the first look at an area takes a moment)…";
    findBtn.disabled = true;
    try {
      const q = await api.get(`/world/query?x=${x}&y=${y}&radius=${r}&limit=3000${ftext.value.trim() ? "&text=" + encodeURIComponent(ftext.value.trim()) : ""}`);
      S.query = { x, y, r, total: q.total, sectors: q.sectors.length };
      const objs = [];
      for (const e of q.entries) {
        const label = (e.name && e.name !== "None" ? e.name : "") || (e.res ? e.res.split("\\").pop() : e.type);
        const base = { sector: e.sector, index: e.i, type: e.type, kind: e.kind, res: e.res, app: e.app, label, expectedNodes: e.expectedNodes };
        if (e.elements && e.elements.items && e.elements.items.length) {
          for (const it of e.elements.items) {
            objs.push({ ...base, element: it.k, count: e.elements.count, pos: it.pos, yaw: it.yaw, shapes: it.shapes,
              label: `${label} #${it.k}`, kind: e.kind === "collision" ? "collision" : e.kind });
          }
        } else {
          objs.push({ ...base, element: null, pos: e.pos, yaw: e.yaw, scale: e.scale });
        }
      }
      S.objects = objs;
      S.byKey = new Map(objs.map((o) => [key(o), o]));
      S.sel = S.sel.filter((o) => S.byKey.has(key(o))).map((o) => S.byKey.get(key(o)));
      if (!opts.keepView) frame(x, y, r);
      status.textContent = `${q.total.toLocaleString()} objects within ${r} m · ${q.sectors.length} sectors`;
      if (!fz.value) {
        const zs = objs.filter((o) => o.kind === "mesh" || o.kind === "terrain").map((o) => o.pos[2]).sort((a, b) => a - b);
        if (zs.length) fz.value = zs[Math.floor(zs.length / 2)].toFixed(1);
      }
      api.patch(`/projects/${pid}/world`, { center: [x, y, Number(fz.value || 0)] }).catch(() => {});
    } catch (e) {
      status.textContent = "";
      toast(e.message, "err");
    } finally {
      S.busy = false;
      findBtn.disabled = false;
      renderOutliner();
      renderInspector();
      draw();
    }
  }

  // ---------------------------------------------------------------- outliner
  function renderOutliner() {
    const tabs = h("div.we-tabs", [["objects", "Objects"], ["changes", `Changes${S.state && S.state.actions.length ? ` (${S.state.actions.length})` : ""}`]].map(([k, l]) =>
      h("button" + (S.outTab === k ? ".on" : ""), { onclick: () => { S.outTab = k; renderOutliner(); } }, l)));
    clear(outliner).append(tabs);
    if (S.outTab === "changes") return renderChanges();
    const counts = {};
    for (const o of S.objects) counts[o.kind] = (counts[o.kind] || 0) + 1;
    const chips = h("div.we-chips", KINDS.filter(([k]) => counts[k]).map(([k, l]) =>
      h("button.chip" + (S.shown.has(k) ? ".on" : ""), { title: `show or hide ${l.toLowerCase()}`, onclick: () => {
        if (S.shown.has(k)) S.shown.delete(k); else S.shown.add(k);
        renderOutliner(); draw();
      } }, h("span.sw", { style: { background: COLORS[k] } }), l, h("span.n", counts[k]))));
    const filt = h("input.filter", { type: "search", placeholder: "Filter the list", value: S.filter, oninput: (e) => { S.filter = e.target.value; renderItems(); } });
    const items = h("div.items");
    outliner.append(chips, filt, items);
    function renderItems() {
      clear(items);
      if (!S.objects.length) {
        items.append(h("div.we-hint", S.query ? "Nothing here of the kinds shown." : "Give a spot above and press Find. Night City's objects around it are listed here."));
        return;
      }
      const f = S.filter.trim().toLowerCase();
      const edited = editedKeys();
      const rows = visible().filter((o) => !f || o.label.toLowerCase().includes(f) || (o.res || "").toLowerCase().includes(f));
      const q = S.query;
      rows.sort((a, b) => dist(a, q) - dist(b, q));
      for (const o of rows.slice(0, 600)) {
        const k = key(o);
        const mark = edited.get(k);
        items.append(h("div.item" + (S.sel.some((s) => key(s) === k) ? ".selected" : ""), {
          onclick: (e) => { select(o, e.shiftKey); centerOn(o); }, title: o.res || o.type },
          h("span.sw", { style: { background: COLORS[o.kind] || COLORS.other } }),
          h("span.name", o.label),
          mark ? h("span.tag" + (mark === "delete" ? ".del" : ".mov"), mark === "delete" ? "deleted" : "moved") : null,
          h("span.sub.num", `${dist(o, q).toFixed(0)} m`)));
      }
      if (rows.length > 600) items.append(h("div.we-hint", `${rows.length - 600} more: narrow with the filter or a smaller radius`));
    }
    renderItems();
  }

  function renderChanges() {
    const st = S.state;
    const box = h("div.items");
    if (st.problem) box.append(h("div.banner", h("b.warn", "Conflict"), h("span", st.problem)));
    if (!st.actions.length) box.append(h("div.we-hint", "No changes yet. Select objects on the plan and delete or move them; add objects with the Add tool."));
    st.actions.forEach((a, i) => box.append(h("div.item.change", { title: a.text },
      h("span.num.faint", `${i + 1}`), h("span.name", a.text),
      h("button.btn.ghost.small.icon-only", { title: "Take this change out", onclick: async (e) => {
        e.stopPropagation();
        try { await api.del(`/projects/${pid}/world/edits/${a.group}`); await reload(); } catch (err) { toast(err.message, "err"); }
      } }, icon("close")))));
    outliner.append(box);
  }

  function editedKeys() {
    const m = new Map();
    for (const e of (S.state && S.state.edits) || []) {
      if (e.op === "add") continue;
      const s = e.sector.toLowerCase();
      if (e.op === "delete") {
        if (e.elements) for (const k of e.elements) m.set(`${s}|${e.index}|${k}`, "delete");
        else m.set(`${s}|${e.index}|`, "delete");
      } else if (e.op === "move") {
        m.set(`${s}|${e.index}|${e.element == null ? "" : e.element}`, "move");
      }
    }
    return m;
  }

  // ---------------------------------------------------------------- selection + inspector
  function select(o, add) {
    if (!o) { S.sel = []; }
    else if (add) {
      const k = key(o);
      S.sel = S.sel.some((s) => key(s) === k) ? S.sel.filter((s) => key(s) !== k) : [...S.sel, o];
    } else S.sel = [o];
    S.selInfo = null;
    renderOutliner();
    renderInspector();
    draw();
    if (S.sel.length === 1) loadSelInfo(S.sel[0]);
  }

  async function loadSelInfo(o) {
    try {
      const q = `sector=${encodeURIComponent(o.sector)}&index=${o.index}${o.element == null ? "" : `&element=${o.element}`}`;
      const info = await api.get(`/world/object?${q}`);
      if (S.sel.length === 1 && key(S.sel[0]) === key(o)) { S.selInfo = info; renderInspector(); draw(); }
    } catch (e) { /* the panel shows what it has */ }
  }

  function num(v, d = 2) { return v == null ? "—" : Number(v).toFixed(d); }

  function renderInspector() {
    clear(inspector);
    const st = S.state;
    if (!st) return;
    if (!S.sel.length) {
      const sm = st.summary || {};
      put(inspector,
        h("div.group", h("h4", "This edit"), h("div.kv",
          h("span", "resource"), h("code", st.name), h("span", "deleted"), h("span.num", `${sm.deleted || 0} objects, ${sm.elementsDeleted || 0} parts`),
          h("span", "moved / changed"), h("span.num", `${(sm.changed || 0) + (sm.elementsMoved || 0)}`), h("span", "added"), h("span.num", `${sm.added || 0}`),
          h("span", "sectors"), h("span.num", `${sm.sectors || 0}`))),
        h("div.group", h("h4", "Add an object"),
          h("label.field", "Resource of the game (.mesh or .ent)", addForm.res),
          h("div.form", { style: { marginTop: "8px" } }, h("label.field", "Yaw °", addForm.yaw), h("label.field", "Appearance", addForm.app),
            h("label.field", "Collision (.mesh)", addForm.col)),
          h("p.help", { style: { marginTop: "8px" } }, "With the Add tool, click the plan to place it (at the Z above). An .ent brings its own collision."),
          h("div.row", h("button.btn.small", { onclick: () => { setTool("add"); } }, icon("plus"), "Use the Add tool"),
            h("button.btn.small", { onclick: () => addAt(Number(fx.value), Number(fy.value)) }, "Place at the spot"))),
        st.builds.length ? h("div.group", h("h4", "Builds"), st.builds.slice(0, 4).map((b) => h("div.row", { style: { padding: "3px 0" } },
          b.ok ? h("span.pill.ok", `build ${b.n}`) : h("span.pill.err", `build ${b.n}`), h("span.muted", fmtAgo(b.time)),
          b.error ? h("span.err", b.error) : h("span.faint.num", fmtBytes(b.bytes || 0))))) : null,
        st.deployments.length ? h("div.group", h("h4", "Installed on"), st.deployments.slice(-4).reverse().map((d) => h("div.we-dep",
          h("b", d.server), d.removed ? h("span.pill.warn", "removed") : null,
          h("span.muted", d.removed ? `${(d.names || []).join(", ")} · ${fmtAgo(d.time)}` : `build ${d.build} · ${d.mode} · ${fmtAgo(d.time)}`))),
          h("div.row", { style: { marginTop: "6px" } },
            h("button.btn.ghost.small.danger", { title: "Take this world edit off a server", onclick: () => removeFromServer() }, "Remove from a server…"))) : null);
      return;
    }
    if (S.sel.length > 1) {
      inspector.append(h("div.group", h("h4", `${S.sel.length} objects`),
        h("div.col", S.sel.slice(0, 12).map((o) => h("div.row", h("span.sw", { style: { background: COLORS[o.kind] } }), h("span", o.label)))),
        h("div.row", { style: { marginTop: "12px" } },
          h("button.btn.danger.small", { onclick: () => deleteSel() }, icon("trash"), "Delete all (with collision)"),
          h("button.btn.ghost.small", { onclick: () => select(null) }, "Clear"))));
      return;
    }
    const o = S.sel[0];
    const info = S.selInfo;
    const mk = (l) => h("input", { type: "number", step: l.endsWith("°") ? "5" : "0.1", value: "0", title: l });
    const dx = mk("dx"), dy = mk("dy"), dz = mk("dz"), dyaw = mk("turn °");
    const rmesh = h("input", { type: "text", placeholder: "another .mesh of the game", style: { width: "100%" } });
    const rapp = h("input", { type: "text", placeholder: o.app || "default" });
    inspector.append(
      h("div.group", h("h4", "Object"), h("div.we-objtitle", h("span.sw", { style: { background: COLORS[o.kind] } }), h("b", o.label)),
        h("div.kv",
          h("span", "type"), h("code", o.type), h("span", "resource"), h("code", o.res || "—"),
          o.app ? h("span", "appearance") : null, o.app ? h("code", o.app) : null,
          h("span", "position"), h("code", `${num(o.pos[0])}, ${num(o.pos[1])}, ${num(o.pos[2])}`),
          h("span", "yaw"), h("code", `${num(o.yaw, 1)}°`),
          h("span", "sector"), h("code", o.sector.split("\\").pop()),
          h("span", o.element == null ? "node" : "node / element"), h("code", o.element == null ? `#${o.index}` : `#${o.index} / ${o.element} of ${o.count}`))),
      h("div.group", h("h4", "Its collision"),
        !info ? h("div.faint", "looking…") :
          info.collision.length ? h("div.col", { style: { gap: "4px" } }, info.collision.map((c) => h("div.we-colrow",
            h("span.sw", { style: { background: COLORS.collision } }),
            h("code", `${c.sector.split("\\").pop().replace(".streamingsector", "")} · node ${c.index}${c.element == null ? "" : " · actor " + c.element}`))),
          h("p.help", "Deleted and moved with the object.")) : h("div.faint", "none found at its place"),
        info && info.proxies && info.proxies.length ? h("p.help", { style: { marginTop: "8px" } },
          `Its far view (${info.proxies.length} stand-in mesh${info.proxies.length > 1 ? "es" : ""} in ${info.proxies[0].sector.split("\\").pop().replace(".streamingsector", "")}) goes and moves with it.`) : null,
        info && info.sharers.length ? h("div.banner", { style: { marginTop: "8px" } }, h("span",
          `${info.sharers.length} other object(s) stand exactly here (${info.sharers.slice(0, 3).map((x) => x.label).join(", ")}): the collision is theirs too and stays unless they go as well.`)) : null),
      h("div.group", h("h4", "Change it"),
        h("div.row", h("button.btn.danger.small", { onclick: () => deleteSel() }, icon("trash"), "Delete (Del)"),
          h("button.btn.ghost.small", { onclick: () => centerOn(o) }, icon("target"), "Center")),
        h("div.we-move", { style: { marginTop: "12px" } },
          h("label", h("span.lbl", "dx m"), dx), h("label", h("span.lbl", "dy m"), dy), h("label", h("span.lbl", "dz m"), dz),
          h("label", h("span.lbl", "turn °"), dyaw)),
        h("div.row", { style: { marginTop: "6px" } }, h("button.btn.small", { onclick: () => moveSel(
          [Number(dx.value) || 0, Number(dy.value) || 0, Number(dz.value) || 0], Number(dyaw.value) || 0) }, icon("move"), "Move")),
        o.element == null && /Mesh/.test(o.type) ? h("div", { style: { marginTop: "12px" } },
          h("label.field", "Replace the mesh", rmesh), h("label.field", { style: { marginTop: "6px" } }, "Appearance", rapp),
          h("div.row", { style: { marginTop: "6px" } }, h("button.btn.small", { onclick: () => apply({ op: "replace", sector: o.sector, index: o.index,
            res: rmesh.value.trim() || undefined, app: rapp.value.trim() || undefined }) }, "Replace"))) : null));
  }

  // ---------------------------------------------------------------- edits
  async function apply(action, quiet) {
    try {
      const r = await api.post(`/projects/${pid}/world/edits`, action);
      for (const n of r.notes || []) toast(n, "");
      if (!quiet && r.actions && r.actions.length) toast(r.actions.map((a) => a.text).join("; "), "ok");
      await reload();
      return r;
    } catch (e) {
      toast(e.message, "err");
      return null;
    }
  }

  async function deleteSel() {
    if (!S.sel.length) return;
    const targets = S.sel.map((o) => ({ sector: o.sector, index: o.index, element: o.element == null ? undefined : o.element }));
    await apply({ op: "delete", targets });
    S.sel = [];
    renderInspector();
    draw();
  }

  async function moveSel(delta, dyaw) {
    if (S.sel.length !== 1) return;
    const o = S.sel[0];
    if (!delta.some((v) => v) && !dyaw) return;
    await apply({ op: "move", sector: o.sector, index: o.index, element: o.element == null ? undefined : o.element, delta, dyaw: dyaw || undefined });
  }

  async function addAt(x, y) {
    const res = addForm.res.value.trim();
    if (!res) { toast("Set the resource to add in the panel first", "err"); return; }
    if (!isFinite(x) || !isFinite(y)) return;
    const z = Number(fz.value || 0);
    await apply({ op: "add", res, pos: [x, y, z], yaw: Number(addForm.yaw.value) || 0, app: addForm.app.value.trim() || undefined,
      collision: addForm.col.value });
  }

  async function undo(redo) {
    try {
      const r = await api.post(`/projects/${pid}/world/${redo ? "redo" : "undo"}`);
      const what = (r.undone || r.redone || []).map((a) => a.text).join("; ");
      if (what) toast((redo ? "Redone: " : "Undone: ") + what);
      await reload();
    } catch (e) { toast(e.message, "err"); }
  }

  async function clearArea() {
    const x = Number(fx.value), y = Number(fy.value);
    if (!isFinite(x) || !isFinite(y) || fx.value === "") { toast("Give a spot first", "err"); return; }
    const radius = h("input", { type: "number", value: "8", min: "1", max: "150", step: "1" });
    const kinds = KINDS.slice(0, 6).map(([k, l]) => {
      const c = h("input", { type: "checkbox", checked: ["mesh", "instanced", "entity", "decal"].includes(k) });
      return [k, h("label.row", c, h("span", l)), c];
    });
    const r = await modal("Clear an area", h("div.col",
      h("p.muted", `Delete Night City's objects within a radius of ${num(x, 1)}, ${num(y, 1)}, with their collision. You see the list before anything is added.`),
      h("label.field", "Radius (m)", radius), h("div.row", kinds.map((k) => k[1]))),
    [{ label: "Cancel", value: null }, { label: "Show what goes", value: "ok", kind: "primary" }]);
    if (r !== "ok") return;
    const action = { op: "clear_area", x, y, radius: Number(radius.value), kinds: kinds.filter((k) => k[2].checked).map((k) => k[0]) };
    let dry;
    try { dry = await api.post(`/projects/${pid}/world/edits`, { ...action, dry_run: true }); } catch (e) { toast(e.message, "err"); return; }
    if (!dry.edits || !dry.edits.length) { toast("Nothing of those kinds there"); return; }
    const sm = dry.summary || {};
    const ok = await modal("Clear an area", h("div.col",
      h("p", `${sm.deleted || 0} objects and ${sm.elementsDeleted || 0} parts go (collision included).`),
      h("div.log", { style: { maxHeight: "240px" } }, (dry.actions || []).slice(0, 200).map((a) => h("div", a.text)))),
    [{ label: "Cancel", value: null }, { label: "Delete them", value: "ok", kind: "danger" }]);
    if (ok === "ok") await apply(action);
  }

  // ---------------------------------------------------------------- build / game / deploy
  async function build() {
    try {
      const job = await api.post(`/projects/${pid}/build`);
      const log = h("div.log", { style: { maxHeight: "260px" } });
      const bar = h("div.progress.indef", h("div"));
      const done = modal("Building the world edit", h("div.col", bar, log), [{ label: "Close", value: null }]);
      const j = await waitJob(job.id, (jj) => { clear(log).append(...logLines(jj.allLines)); log.scrollTop = log.scrollHeight; });
      bar.className = "progress";
      bar.firstChild.style.width = "100%";
      if (j.state === "done") toast(`Build ${j.result.n}: ${j.result.bytes} bytes`, "ok");
      await reload();
      return done;
    } catch (e) { toast(e.message, "err"); }
  }

  async function gameInstall(uninstall) {
    try {
      const job = await api.post(`/projects/${pid}/world/game`, { uninstall });
      const j = await waitJob(job.id);
      toast(j.state === "done" ? (uninstall ? "Removed from your game" : "In your game: start it and go there") : (j.error || j.state), j.state === "done" ? "ok" : "err");
      reload();
    } catch (e) { toast(e.message, "err"); }
  }

  async function deploy() {
    const servers = store.state.settings.servers;
    if (!servers.length) { toast("Add a server first (Servers)", "err"); return; }
    const srv = h("select", servers.map((s) => h("option", { value: s.id }, s.label)));
    srv.value = store.state.settings.deploy.default_server || servers[0].id;
    const mode = h("select", [["stage", "install; restart when nobody is online"], ["restart", "install and restart now (nobody online)"], ["files", "install files only"]]
      .map(([v, l]) => h("option", { value: v }, l)));
    mode.value = store.state.settings.deploy.mode;
    const r = await modal("Install on a server", h("div.col", h("label.field", "Server", srv), h("label.field", "How", mode),
      h("p.help", "The edit is a required mod: players get it at their next launch after the server restarts.")),
    [{ label: "Cancel", value: null }, { label: "Install", value: "ok", kind: "primary" }]);
    if (r !== "ok") return;
    try {
      const job = await api.post(`/projects/${pid}/deploy`, { server: srv.value, mode: mode.value });
      const log = h("div.log", { style: { maxHeight: "300px" } });
      modal("Installing", log, [{ label: "Close", value: null }]);
      await waitJob(job.id, (jj) => { clear(log).append(...logLines(jj.allLines)); log.scrollTop = log.scrollHeight; });
      reload();
    } catch (e) { toast(e.message, "err"); }
  }

  async function removeFromServer() {
    const servers = store.state.settings.servers;
    if (!servers.length) { toast("No servers in the Studio", "err"); return; }
    const last = [...S.state.deployments].reverse().find((d) => !d.removed);
    const srv = h("select", servers.map((s) => h("option", { value: s.id }, s.label)));
    srv.value = last && servers.some((s) => s.id === last.server) ? last.server : servers[0].id;
    const restart = h("input", { type: "checkbox" });
    const r = await modal(`Remove ${S.state.display || S.state.name}`, h("div.col", h("label.field", "Server", srv),
      h("label.row", restart, h("span", "restart now if nobody is online (otherwise players keep the change until the next start)")),
      h("p.help", `The resource ${S.state.name} and its resources.load entry go; the server keeps a backup. The edit stays here in the Studio.`)),
    [{ label: "Cancel", value: null }, { label: "Remove", value: "ok", kind: "danger" }]);
    if (r !== "ok") return;
    try {
      const job = await api.post(`/servers/${srv.value}/world/remove`, { resource: S.state.name, restart: restart.checked });
      const log = h("div.log", { style: { maxHeight: "300px" } });
      modal("Removing", log, [{ label: "Close", value: null }]);
      const j = await waitJob(job.id, (jj) => { clear(log).append(...logLines(jj.allLines)); log.scrollTop = log.scrollHeight; });
      if (j.state === "done") toast(`Removed from ${srv.selectedOptions[0].textContent}`, "ok");
      reload();
    } catch (e) { toast(e.message, "err"); }
  }

  // ---------------------------------------------------------------- the plan (canvas)
  function visible() { return S.objects.filter((o) => S.shown.has(o.kind) || (!KINDS.some(([k]) => k === o.kind) && S.shown.has("other"))); }
  function dist(o, q) { return q ? Math.hypot(o.pos[0] - q.x, o.pos[1] - q.y) : 0; }

  function frame(x, y, r) {
    const w = canvas.clientWidth || 800, hh = canvas.clientHeight || 600;
    S.view = { cx: x, cy: y, scale: Math.max(0.5, Math.min(60, Math.min(w, hh) / (r * 2.3))) };
  }
  function centerOn(o) { S.view.cx = o.pos[0]; S.view.cy = o.pos[1]; draw(); }
  function toScreen(x, y) {
    const w = canvas.clientWidth, hh = canvas.clientHeight;
    return [w / 2 + (x - S.view.cx) * S.view.scale, hh / 2 - (y - S.view.cy) * S.view.scale];
  }
  function toWorld(sx, sy) {
    const w = canvas.clientWidth, hh = canvas.clientHeight;
    return [S.view.cx + (sx - w / 2) / S.view.scale, S.view.cy - (sy - hh / 2) / S.view.scale];
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, hh = canvas.clientHeight;
    if (!w || !hh) return;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hh * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
    }
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = "#0b0c0e";
    g.fillRect(0, 0, w, hh);
    // grid: 10 m, bold every 50 m
    const step = S.view.scale < 1.2 ? 50 : 10;
    const [x0, y1] = toWorld(0, 0), [x1, y0] = toWorld(w, hh);
    g.lineWidth = 1;
    for (let gx = Math.floor(x0 / step) * step; gx <= x1; gx += step) {
      const [sx] = toScreen(gx, 0);
      g.strokeStyle = gx % 50 === 0 ? "#22252a" : "#16181b";
      g.beginPath(); g.moveTo(Math.round(sx) + 0.5, 0); g.lineTo(Math.round(sx) + 0.5, hh); g.stroke();
    }
    for (let gy = Math.floor(y0 / step) * step; gy <= y1; gy += step) {
      const [, sy] = toScreen(0, gy);
      g.strokeStyle = gy % 50 === 0 ? "#22252a" : "#16181b";
      g.beginPath(); g.moveTo(0, Math.round(sy) + 0.5); g.lineTo(w, Math.round(sy) + 0.5); g.stroke();
    }
    g.fillStyle = "#4b4f57";
    g.font = "10.5px 'IBM Plex Mono', monospace";
    for (let gx = Math.ceil(x0 / 50) * 50; gx <= x1; gx += 50) { const [sx] = toScreen(gx, 0); g.fillText(String(gx), sx + 3, hh - 6); }
    for (let gy = Math.ceil(y0 / 50) * 50; gy <= y1; gy += 50) { const [, sy] = toScreen(0, gy); g.fillText(String(gy), 4, sy - 3); }
    // the area
    if (S.query) {
      const [qx, qy] = toScreen(S.query.x, S.query.y);
      g.setLineDash([4, 4]);
      g.strokeStyle = "#3a3e45";
      g.beginPath(); g.arc(qx, qy, S.query.r * S.view.scale, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      g.strokeStyle = "#5a5f68";
      g.beginPath(); g.moveTo(qx - 6, qy); g.lineTo(qx + 6, qy); g.moveTo(qx, qy - 6); g.lineTo(qx, qy + 6); g.stroke();
    }
    // objects
    const edited = editedKeys();
    const selKeys = new Set(S.sel.map(key));
    const sz = Math.max(2, Math.min(7, S.view.scale * 0.9));
    for (const o of visible()) {
      const [sx, sy] = toScreen(o.pos[0], o.pos[1]);
      if (sx < -10 || sy < -10 || sx > w + 10 || sy > hh + 10) continue;
      const k = key(o);
      if (edited.get(k) === "delete") continue;           // drawn as a red cross below
      const c = COLORS[o.kind] || COLORS.other;
      g.strokeStyle = c; g.fillStyle = c;
      if (o.kind === "collision") {
        g.strokeRect(sx - sz, sy - sz, sz * 2, sz * 2);
      } else if (o.kind === "instanced" || o.kind === "foliage" || o.kind === "decal") {
        g.beginPath(); g.arc(sx, sy, Math.max(1.6, sz * 0.55), 0, Math.PI * 2); g.fill();
      } else if (o.kind === "entity") {
        g.beginPath(); g.moveTo(sx, sy - sz); g.lineTo(sx + sz, sy + sz); g.lineTo(sx - sz, sy + sz); g.closePath(); g.fill();
      } else {
        g.globalAlpha = 0.9;
        g.fillRect(sx - sz * 0.7, sy - sz * 0.7, sz * 1.4, sz * 1.4);
        g.globalAlpha = 1;
      }
      // heading tick
      if (S.view.scale > 5 && o.yaw != null && (o.kind === "mesh" || o.kind === "entity" || selKeys.has(k))) {
        const a = (o.yaw * Math.PI) / 180;
        g.strokeStyle = c;
        g.beginPath(); g.moveTo(sx, sy); g.lineTo(sx - Math.sin(a) * sz * 2.2, sy - Math.cos(a) * sz * 2.2); g.stroke();
      }
    }
    // the edits
    for (const e of (S.state && S.state.edits) || []) {
      if (e.role === "collision" && !S.shown.has("collision")) continue;
      const companion = e.role === "collision" || e.role === "proxy";
      if (e.op === "delete" && e.was && e.was.pos) {
        const [sx, sy] = toScreen(e.was.pos[0], e.was.pos[1]);
        g.strokeStyle = companion ? "rgba(226,87,77,.45)" : "#e2574d";
        g.lineWidth = 1.5;
        g.beginPath(); g.moveTo(sx - 4, sy - 4); g.lineTo(sx + 4, sy + 4); g.moveTo(sx + 4, sy - 4); g.lineTo(sx - 4, sy + 4); g.stroke();
        g.lineWidth = 1;
      } else if (e.op === "move" && e.was && e.was.pos && e.pos) {
        const [ax, ay] = toScreen(e.was.pos[0], e.was.pos[1]);
        const [bx, by] = toScreen(e.pos[0], e.pos[1]);
        g.strokeStyle = companion ? "rgba(79,176,114,.4)" : "#4fb072";
        g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
        g.setLineDash([]);
        g.beginPath(); g.arc(bx, by, 4, 0, Math.PI * 2); g.stroke();
      } else if (e.op === "add" && e.pos) {
        const [sx, sy] = toScreen(e.pos[0], e.pos[1]);
        g.strokeStyle = "#4fb072";
        g.lineWidth = 2;
        g.beginPath(); g.moveTo(sx - 6, sy); g.lineTo(sx + 6, sy); g.moveTo(sx, sy - 6); g.lineTo(sx, sy + 6); g.stroke();
        g.lineWidth = 1;
        const a = ((e.yaw || 0) * Math.PI) / 180;
        g.beginPath(); g.moveTo(sx, sy); g.lineTo(sx - Math.sin(a) * 12, sy - Math.cos(a) * 12); g.stroke();
      }
    }
    // the selection, its collision, a drag in progress
    if (S.selInfo && S.sel.length === 1) {
      g.strokeStyle = "#6aa5e6";
      for (const c of S.selInfo.collision) {
        const [sx, sy] = toScreen(c.pos[0], c.pos[1]);
        g.beginPath(); g.arc(sx, sy, 9, 0, Math.PI * 2); g.stroke();
      }
    }
    for (const o of S.sel) {
      const [sx, sy] = toScreen(o.pos[0], o.pos[1]);
      g.strokeStyle = "#e8b931"; g.lineWidth = 2;
      g.beginPath(); g.arc(sx, sy, Math.max(7, sz + 4), 0, Math.PI * 2); g.stroke();
      g.lineWidth = 1;
    }
    if (S.drag && S.drag.kind === "move") {
      const [ax, ay] = toScreen(S.drag.o.pos[0], S.drag.o.pos[1]);
      const [bx, by] = toScreen(S.drag.to[0], S.drag.to[1]);
      g.strokeStyle = "#e8b931"; g.setLineDash([4, 3]);
      g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke(); g.setLineDash([]);
      g.beginPath(); g.arc(bx, by, 7, 0, Math.PI * 2); g.stroke();
    }
    // scale bar
    const bar = S.view.scale > 6 ? 10 : S.view.scale > 1.5 ? 50 : 100;
    g.fillStyle = "#a3a7ae"; g.strokeStyle = "#a3a7ae";
    g.beginPath(); g.moveTo(w - 20 - bar * S.view.scale, hh - 22); g.lineTo(w - 20, hh - 22); g.stroke();
    g.fillText(`${bar} m`, w - 20 - bar * S.view.scale, hh - 28);
    g.fillText("N ↑", w - 34, 18);
  }

  function nearest(sx, sy, maxPx = 10) {
    let best = null, bd = maxPx * maxPx;
    const edited = editedKeys();
    for (const o of visible()) {
      if (edited.get(key(o)) === "delete") continue;
      const [ox, oy] = toScreen(o.pos[0], o.pos[1]);
      const d = (ox - sx) ** 2 + (oy - sy) ** 2;
      if (d < bd) { bd = d; best = o; }
    }
    return best;
  }

  function setTool(t) {
    S.tool = t;
    for (const b of toolSeg.querySelectorAll("button")) b.classList.toggle("on", b.dataset.tool === t);
    canvas.style.cursor = t === "add" ? "crosshair" : t === "move" ? "move" : "default";
  }

  canvas.addEventListener("mousedown", (e) => {
    canvas.focus();
    const r = canvas.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    if (e.button === 1 || e.button === 2) { S.drag = { kind: "pan", sx, sy, cx: S.view.cx, cy: S.view.cy }; e.preventDefault(); return; }
    if (S.tool === "add") { const [x, y] = toWorld(sx, sy); addAt(x, y); return; }
    const o = nearest(sx, sy);
    if (S.tool === "move" && o && S.sel.length === 1 && key(o) === key(S.sel[0])) {
      S.drag = { kind: "move", o, sx, sy, to: [...o.pos] };
      return;
    }
    if (o) { select(o, e.shiftKey); return; }
    S.drag = { kind: "pan", sx, sy, cx: S.view.cx, cy: S.view.cy, click: true };
  });
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
  S.disposers.push(() => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); });
  function onMove(e) {
    const r = canvas.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    if (S.drag && S.drag.kind === "pan") {
      if (Math.abs(sx - S.drag.sx) + Math.abs(sy - S.drag.sy) > 3) S.drag.click = false;
      S.view.cx = S.drag.cx - (sx - S.drag.sx) / S.view.scale;
      S.view.cy = S.drag.cy + (sy - S.drag.sy) / S.view.scale;
      draw();
      return;
    }
    if (S.drag && S.drag.kind === "move") {
      const [x, y] = toWorld(sx, sy);
      S.drag.to = [x, y, S.drag.o.pos[2]];
      draw();
      return;
    }
    if (e.target !== canvas) { tip.classList.add("hidden"); return; }
    const o = nearest(sx, sy);
    if (o !== S.hover) {
      S.hover = o;
      if (o) {
        clear(tip).append(h("b", o.label), h("div.faint", o.res ? o.res.split("\\").pop() : o.type),
          h("div.mono", `${num(o.pos[0], 1)}, ${num(o.pos[1], 1)}, ${num(o.pos[2], 1)}`));
        tip.classList.remove("hidden");
      } else tip.classList.add("hidden");
    }
    if (o) { tip.style.left = `${sx + 14}px`; tip.style.top = `${sy + 10}px`; }
    const [wx, wy] = toWorld(sx, sy);
    if (!S.busy) status.textContent = `${num(wx, 1)}, ${num(wy, 1)}` + (S.query ? `  ·  ${S.query.total.toLocaleString()} objects within ${S.query.r} m` : "");
  }
  async function onUp() {
    const d = S.drag;
    S.drag = null;
    if (!d) return;
    if (d.kind === "pan" && d.click) select(null);
    if (d.kind === "move") {
      const delta = [d.to[0] - d.o.pos[0], d.to[1] - d.o.pos[1], 0];
      if (Math.hypot(delta[0], delta[1]) > 0.05) await moveSel(delta.map((v) => Math.round(v * 100) / 100), 0);
      else draw();
    }
  }
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("dblclick", (e) => {
    const r = canvas.getBoundingClientRect();
    const [x, y] = toWorld(e.clientX - r.left, e.clientY - r.top);
    fx.value = x.toFixed(1); fy.value = y.toFixed(1);
    find({ keepView: true });
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    const [wx, wy] = toWorld(sx, sy);
    S.view.scale = Math.max(0.2, Math.min(80, S.view.scale * (e.deltaY < 0 ? 1.18 : 1 / 1.18)));
    const [nx, ny] = toWorld(sx, sy);
    S.view.cx += wx - nx; S.view.cy += wy - ny;
    draw();
  }, { passive: false });
  canvas.addEventListener("keydown", (e) => {
    if (e.target !== canvas) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); undo(false); }
    else if ((e.ctrlKey || e.metaKey) && k === "y") { e.preventDefault(); undo(true); }
    else if (k === "delete" || k === "backspace") { e.preventDefault(); deleteSel(); }
    else if (k === "escape") select(null);
    else if (k === "s") setTool("select");
    else if (k === "m") setTool("move");
    else if (k === "a") setTool("add");
    else if (k === "f" && S.sel.length) centerOn(S.sel[0]);
    else if ((k === "q" || k === "e") && S.sel.length === 1) moveSel([0, 0, 0], k === "q" ? 15 : -15);
  });
  const ro = new ResizeObserver(() => draw());
  ro.observe(canvas);
  S.disposers.push(() => ro.disconnect());
  for (const el of [fx, fy, fz, ftext]) el.addEventListener("keydown", (e) => { if (e.key === "Enter") find(); });

  // live: a build or an agent's edit elsewhere
  const off = store.on((ev) => {
    if (ev.type === "job" && ev.job.project === pid && ["done", "failed"].includes(ev.job.state)) reload().catch(() => {});
    if (ev.type === "mcp" && ev.kind === "result" && ev.call && /^world_|build_world|new_world/.test(ev.call.tool)) reload().catch(() => {});
  });
  S.disposers.push(off);

  setTool("select");
  (async () => {
    try {
      await reload();
      const idx = S.state.index || {};
      if (!idx.ready) {
        stage.prepend(h("div.banner", { style: { margin: "10px 12px 0" } }, h("b.warn", "Night City is not read yet"),
          h("span", "Its sector list is read from your game once."), h("div.grow"), h("a.btn.small", { href: "#/world" }, "Read it")));
      }
      const c = S.state.center;
      if (c) {
        fx.value = c.x; fy.value = c.y; fz.value = c.z;
        if (idx.ready) find();
      } else {
        frame(0, 0, 200);
        draw();
      }
    } catch (e) {
      clear(stage).append(h("div.card.err", { style: { margin: "16px" } }, "Could not open this world edit: ", e.message));
    }
  })();
  return { dispose: () => S.disposers.forEach((d) => d()) };
}
