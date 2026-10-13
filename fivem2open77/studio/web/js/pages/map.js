// One map: overview, 3D, options, build, the local game, servers, placement, AI agent.
import { api, uploadFile, waitJob } from "../api.js";
import { store } from "../store.js";
import { h, clear, put, toast, modal, confirmBox, prompt, fmtBytes, fmtTime, fmtAgo, pill, logLines, icon, setCrumbs } from "../ui.js";
import { mapViewerTab } from "./mapviewer.js";
import { placePanel } from "./place.js";
import { agentTab } from "./agent.js";

const TABS = [["overview", "Overview"], ["3d", "3D"], ["doors", "Doors"], ["options", "Options"],
  ["assets", "NCWE assets"], ["build", "Build"],
  ["game", "In my game"], ["deploy", "Servers"], ["place", "Placement"], ["agent", "AI agent"]];

// a loose x.archive + x.archive.xl pair (no folder) becomes one import: x/x.archive + x/x.archive.xl
export function groupLoose(pairs) {
  const base = (n) => n.replace(/\.archive(\.xl)?$/i, "");
  const counts = {};
  for (const [n] of pairs) if (!n.includes("/") && /\.archive(\.xl)?$/i.test(n)) counts[base(n)] = (counts[base(n)] || 0) + 1;
  return pairs.map(([n, f]) => (!n.includes("/") && /\.archive(\.xl)?$/i.test(n) && counts[base(n)] > 1
    ? [`${base(n)}/${n}`, f] : [n, f]));
}

// what a drop carries: walk the drag's entries so a folder keeps its relative paths (dataTransfer.files
// flattens it).  Falls back to the flat file list where there is no entries API.
export function droppedFiles(dt) {
  const entries = [...(dt.items || [])].map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.length) {
    return Promise.resolve(groupLoose([...(dt.files || [])].map((f) => [f.webkitRelativePath || f.name, f])));
  }
  return new Promise((resolve) => {
    const out = [];
    let pending = 1;
    const done = () => { if (!--pending) resolve(groupLoose(out)); };
    const walk = (entry, prefix) => {
      if (entry.isFile) {
        pending++;
        entry.file((f) => { out.push([prefix + entry.name, f]); done(); }, done);
      } else if (entry.isDirectory) {
        pending++;
        const reader = entry.createReader();
        const readBatch = () => reader.readEntries((batch) => {
          if (!batch.length) return done();
          for (const e of batch) walk(e, prefix + entry.name + "/");
          readBatch();
        }, done);
        readBatch();
      }
    };
    for (const e of entries) walk(e, "");
    done();
  });
}

export function mapPage(root, args) {
  const pid = args[0];
  const tab = args[1] || "overview";
  const ctx = { pid, kind: "map", state: null, disposers: [], root, version: Date.now() };
  const head = h("div.page-head");
  const tabs = h("div.tabs", TABS.map(([k, label]) => h("a", { href: `#/map/${pid}/${k}`, class: k === tab ? "active" : "" }, label)));
  const body = h("div", h("div.empty", "Loading..."));
  root.append(head, tabs, body);

  ctx.reload = async () => {
    ctx.state = await api.get(`/projects/${pid}/map`);
    ctx.look = ctx.state.look;
    ctx.project = ctx.state.project;
    renderHead();
    return ctx.state;
  };
  ctx.patch = async (patch, { quiet } = {}) => {
    try {
      const r = await api.patch(`/projects/${pid}/map/options`, patch);
      if (!quiet) toast(r.look_job ? "Saved: looking at the map again with the new options" : "Saved", "ok");
      await ctx.reload();
      return r;
    } catch (e) {
      toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
      throw e;
    }
  };

  function renderHead() {
    const p = ctx.project;
    const m = p.map || {};
    const label = (TABS.find(([k]) => k === tab) || TABS[0])[1];
    setCrumbs([["Maps", "#/maps"], [p.name || "(unnamed)", `#/map/${pid}/overview`], [label]]);
    clear(head).append(
      h("div", h("h1", p.name || "(unnamed)"),
        h("div.row.meta", { style: { gap: "12px", marginTop: "2px" } }, m.name ? h("code", m.name) : null,
          m.size ? h("span", `${Math.round(m.size[0])} × ${Math.round(m.size[1])} × ${Math.round(m.size[2])} m`) : null,
          m.placements ? h("span", `${m.placements} placements`) : null, h("span.faint", p.source))),
      h("div.grow"),
      h("div.toolbar",
        h("button.btn.ghost", { onclick: async () => {
          const n = await prompt("Rename", "Project name (the map's in-game name is in Options)", p.name);
          if (n) { await api.patch(`/projects/${pid}`, { name: n }); ctx.reload(); }
        } }, "Rename"),
        h("button.btn.ghost", { title: "Delete this project", onclick: async () => {
          if (await confirmBox("Delete project", `Delete ${p.name} and all its builds from the Studio? Servers and your game are not touched.`, "Delete", "danger")) {
            await api.del(`/projects/${pid}`);
            location.hash = "#/maps";
          }
        } }, icon("trash"), "Delete"),
        h("span.sep"),
        h("button.btn", { onclick: () => { location.hash = `#/map/${pid}/agent`; } }, icon("agent"), "Ask the agent"),
        h("button.btn.primary", { onclick: () => startMapBuild(ctx) }, icon("build"), "Build")));
  }

  (async () => {
    try {
      await ctx.reload();
      const fn = { overview, "3d": viewer3d, doors: doorsTab, options: optionsTab, assets: assetsTab, build: buildTab, game: gameTab,
        deploy: deployTab, place: placeTab, agent: agentTab }[tab] || overview;
      clear(body);
      fn(body, ctx);
    } catch (e) {
      clear(body).append(h("div.card", h("b.err", "Could not open this map: "), e.message));
    }
  })();
  return { dispose: () => ctx.disposers.forEach((d) => d()) };
}

// ------------------------------------------------------------------ helpers
export async function startMapBuild(ctx) {
  try {
    await api.post(`/projects/${ctx.pid}/build`);
    toast("Build started");
    location.hash = `#/map/${ctx.pid}/build`;
  } catch (e) {
    toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
  }
}

// one click: rebuild (picking up changed NCWE imports) and deploy that fresh build where the map lives
export async function rebuildDeploy(ctx, target) {
  try {
    await api.post(`/projects/${ctx.pid}/rebuild_deploy`,
      target ? { server: target.server, names: target.names } : {});
    toast("Rebuilding and deploying");
    location.hash = `#/map/${ctx.pid}/build`;
  } catch (e) {
    toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
  }
}

export function staleBanner(ctx, stale) {
  return h("div.banner", { style: { padding: "10px 12px", margin: "0 0 10px", display: "grid", gap: "6px" } },
    h("b", "The last build is stale"),
    h("div", stale.reason),
    h("div.row",
      h("button.btn.primary.small", { onclick: () => startMapBuild(ctx) }, "Rebuild"),
      stale.target ? h("button.btn.small", { title: `Deploy the fresh build to ${stale.target.label}`,
        onclick: () => rebuildDeploy(ctx, stale.target) }, "Rebuild and deploy")
        : h("span.help", "deploy it to a server once to enable one-click rebuild and deploy")));
}

function fact(label, value) { return h("div.fact", h("span", label), h("b", value ?? "-")); }
const fmtSpot = (a) => (a ? `${Number(a.x).toFixed(1)}, ${Number(a.y).toFixed(1)}, ${Number(a.z).toFixed(1)}  yaw ${Number(a.yaw || 0).toFixed(0)}°` : "-");

// Run fn once the look is there; meanwhile show the look job (or offer to start one).
function withLook(root, ctx, fn) {
  if (ctx.look) return fn();
  const running = (ctx.state.jobs || []).find((j) => j.kind === "look");
  const bar = h("div.progress.indef", h("div"));
  const step = h("span.muted", running ? running.step || "starting" : "");
  const log = h("div.log", { style: { maxHeight: "160px" } });
  const card = h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "Looking at the map"), step, h("div.grow")),
    h("p.help", "The converter reads the map's files, builds its parts and draws previews. A big MLO takes about a minute; nothing is written to the game."),
    bar, h("div", { style: { height: "8px" } }), log);
  root.append(card);
  async function follow(id) {
    const j = await waitJob(id, (jj) => {
      clear(log).append(...logLines(jj.allLines.slice(-60)));
      log.scrollTop = log.scrollHeight;
      step.textContent = jj.step || "";
      if (jj.progress != null) { bar.className = "progress"; bar.firstChild.style.width = `${jj.progress * 100}%`; }
    });
    if (j.state === "done") {
      await ctx.reload();
      card.remove();
      fn();
    } else {
      step.textContent = "";
      card.append(h("div.err", { style: { marginTop: "8px" } }, j.error || j.state),
        h("div.row", { style: { marginTop: "8px" } }, h("button.btn", { onclick: start }, "Try again")));
    }
  }
  async function start() {
    try {
      const j = await api.post(`/projects/${ctx.pid}/map/look`, {});
      follow(j.id);
    } catch (e) { toast(e.message, "err"); }
  }
  if (running) follow(running.id);
  else {
    clear(log).append(h("span.faint", "Not looked at with the current options yet."));
    card.append(h("div.row", { style: { marginTop: "10px" } }, h("button.btn.primary", { onclick: start }, "Look now")));
    bar.className = "progress";
  }
}

// ------------------------------------------------------------------ overview
function overview(root, ctx) {
  withLook(root, ctx, () => {
    const L = ctx.look;
    const st = ctx.state;
    const opts = st.normalized || {};
    let view = "iso";
    let mode = "material";
    const img = h("img.sheet", { alt: "map preview" });
    const show = () => { img.src = `/api/projects/${ctx.pid}/map/render.png?view=${view}&mode=${mode}&k=${st.look_key}`; };
    show();
    const btns = (items, cur, set) => h("div.row", { style: { gap: "4px" } }, items.map(([k, label]) => h("button.btn.small" + (k === cur() ? ".cyan" : ""), {
      onclick: (e) => { set(k); show(); for (const b of e.target.parentNode.querySelectorAll("button")) b.classList.remove("cyan"); e.target.classList.add("cyan"); },
    }, label)));
    const lb = (st.builds || [])[0];
    const deps = st.deployments || [];
    const steps = [
      ["Looked at the map", true],
      ["A spot to bake at (or place it in game)", !!opts.place],
      ["Built", lb && lb.ok],
      ["Checks passed", lb && lb.qa && lb.qa.ok],
      ["Installed on a server", deps.length > 0],
    ];
    const mlos = L.scene.mlo_instances || [];
    const missing = Object.entries(L.scene.missing_archetypes || {});
    root.append(h("div.split",
      h("div",
        h("div.card", h("div.row", { style: { marginBottom: "10px" } }, h("h3", { style: { margin: 0 } }, "How it will look"), h("div.grow"),
          btns([["iso", "3/4"], ["iso_back", "3/4 back"], ["plan", "From above"]], () => view, (v) => { view = v; }),
          btns([["material", "Colours"], ["parts", "Parts"]], () => mode, (v) => { mode = v; })), img,
          h("div.help", { style: { marginTop: "8px" } }, "From above: north up, a line every 10 m (bold every 50 m), the white cross is where the map is anchored. ",
            "Parts: interiors beige, objects green, cells grey-green, doors blue, glass light blue.")),
        missing.length ? h("div.card", h("h3", "Left out: vanilla GTA objects"),
          h("p.help", `${L.scene.missing_total.toLocaleString()} entities of this map place GTA V's own props (trees, lamps, street furniture...). `,
            "Their models are not in the resource and Cyberpunk has none, so they are left out; the map's own buildings and props are all there."),
          h("details", h("summary.help", { style: { cursor: "pointer" } }, `the ${missing.length} most used (GTA names are hashed; only the resource's own models have names)`),
            h("div.row.wrap", { style: { gap: "6px", marginTop: "8px" } }, missing.slice(0, 40).map(([n, c]) => h("span.pill", `${n} ×${c}`))))) : null),
      h("div",
        h("div.card", h("h3", "The map"),
          h("div.facts",
            fact("Resource", L.map), fact("Shown as", L.display_name),
            fact("Size", `${Math.round(L.size[0])} × ${Math.round(L.size[1])} m`), fact("Height", `${Math.round(L.size[2])} m`),
            fact("Interiors (MLO)", mlos.length), fact("Entities", L.scene.world_entities),
            fact("Parts", L.parts.length), fact("Placements", L.placements),
            fact("Doors / gates", (L.doors || []).reduce((a, d) => a + (d.devices || 0), 0)),
            fact("Glass slabs", Object.values(L.glass || {}).reduce((a, b) => a + b, 0)),
            fact("Triangles", `${(L.triangles_placed / 1e6).toFixed(2)} M`), fact("Colliders", L.colliders.toLocaleString()),
            fact("Textures", L.textures), fact("Materials", L.materials))),
        spotCard(ctx),
        h("div.card", h("h3", "Progress"),
          steps.map(([label, ok]) => h("div.row", h("span", { class: ok ? "ok" : "faint" }, ok ? "✔" : "○"), label)),
          h("div.row", { style: { marginTop: "12px" } },
            h("button.btn.primary", { onclick: () => startMapBuild(ctx) }, lb ? "Build again" : "Build"),
            lb && lb.ok ? h("a.btn", { href: `#/map/${ctx.pid}/deploy` }, "Install on a server...") : null,
            h("a.btn.cyan", { href: `#/map/${ctx.pid}/agent` }, "Let the AI do it"))),
        (L.warnings || []).length ? h("div.card", h("h3", h("span.warn", "⚠"), "Notes from the converter"),
          L.warnings.slice(0, 40).map((w) => h("div.help", { style: { padding: "3px 0" } }, w))) : null)));
  });
}

function spotCard(ctx) {
  const p = (ctx.state.normalized || {}).place;
  const f = {};
  const num = (k, v, step) => (f[k] = h("input", { type: "number", step, value: v ?? "", style: { width: "100%" } }));
  const none = h("input", { type: "checkbox", checked: !p });
  const grid = h("div.form", { style: { gridTemplateColumns: "repeat(4, minmax(0, 1fr))" } },
    h("label.field", "x (east)", num("x", p && p.x, 0.1)), h("label.field", "y (north)", num("y", p && p.y, 0.1)),
    h("label.field", "z (up)", num("z", p && p.z, 0.1)), h("label.field", "yaw °", num("yaw", p && p.yaw, 1)));
  const sync = () => grid.classList.toggle("hidden", none.checked);
  none.addEventListener("change", sync);
  sync();
  return h("div.card", h("h3", "Where it stands"),
    h("p.help", "The Night City spot the build is baked at (metres; yaw turns it counter-clockwise seen from above). On a server you move it in game with F9 or in Placement; a redeploy keeps the spot it was moved to."),
    h("label.row", none, h("span", "no spot: players see it after it is placed in game and applied")), grid,
    h("div.row", { style: { marginTop: "10px" } }, h("div.grow"), h("button.btn", { onclick: async () => {
      if (none.checked) return ctx.patch({ place: null });
      const v = {};
      for (const k of ["x", "y", "z", "yaw"]) {
        if (f[k].value === "" && k !== "yaw") return toast(`${k} is needed`, "err");
        v[k] = Number(f[k].value || 0);
      }
      await ctx.patch({ place: v });
    } }, "Save spot")));
}

// ------------------------------------------------------------------ 3D
function viewer3d(root, ctx) {
  withLook(root, ctx, () => mapViewerTab(root, ctx));
}

// ------------------------------------------------------------------ doors
// The door mapping: every doorway of the map's interiors with what closes it, the doors found
// (by name, by standing in a doorway, by the mapping, or made for a doorway), objects that look
// like doors, and any object by name.  Changes are kept here until "Save", then the map is
// looked at again with them.
const WAY_MODES = [["auto", "as found"], ["open", "open: cut the collision out"], ["door-hinged", "make a door (swings)"],
  ["door-double", "make a double door"], ["door-sliding", "make a sliding door"], ["door-rolling", "make a garage door (rolls up)"]];
const OBJ_MODES = [["auto", "as found"], ["hinged", "door: swings"], ["sliding", "door: slides"], ["rolling", "garage door: rolls up"],
  ["open", "no collision (walk through)"], ["solid", "solid, never a door"], ["remove", "leave out"]];
const STATUS = { open: ["ok", "open"], door: ["run", "door"], closed: ["", "closed in GTA too"], blocked: ["err", "blocked"],
  window: ["", "window"] };
const OPENING = { HINGED: "swings", SLIDING_HORIZONTALLY: "slides", SLIDING_VERTICALLY: "rolls up" };
const FOUND = { name: "by its name", doorway: "stands in a doorway", mapped: "your mapping", generated: "made for the doorway" };

function doorsTab(root, ctx) {
  withLook(root, ctx, () => {
    const L = ctx.look;
    const saved = (ctx.state.normalized || {}).door_map || {};
    const map = { objects: Object.assign({}, saved.objects || {}), doorways: Object.assign({}, saved.doorways || {}) };
    const changed = () => JSON.stringify(map) !== JSON.stringify({ objects: saved.objects || {}, doorways: saved.doorways || {} });
    const saveBtn = h("button.btn.primary", { disabled: true, onclick: save }, "Save and look again");
    const resetBtn = h("button.btn.ghost", { disabled: true, onclick: () => { clear(root); doorsTab(root, ctx); } }, "Discard changes");
    const sync = () => { saveBtn.disabled = !changed(); resetBtn.disabled = !changed(); };
    const select = (modes, cur, onChange) => {
      const el = h("select", modes.map(([v, l]) => h("option", { value: v }, l)));
      el.value = cur || "auto";
      el.addEventListener("change", () => { onChange(el.value); sync(); });
      return el;
    };
    const setObj = (name, v) => { const k = name.toLowerCase(); if (v === "auto") delete map.objects[k]; else map.objects[k] = v; };
    const setWay = (key, v) => { if (v === "auto") delete map.doorways[key]; else map.doorways[key] = v; };
    async function save() {
      const dm = (Object.keys(map.objects).length || Object.keys(map.doorways).length) ? map : null;
      try {
        const r = await ctx.patch({ door_map: dm });
        if (r.look_job) location.hash = `#/map/${ctx.pid}/overview`;
        else { clear(root); doorsTab(root, ctx); }
      } catch (_) { /* toast shown */ }
    }

    const ways = L.doorways || [];
    const doors = L.doors || [];
    const cands = L.door_candidates || [];
    const counts = {};
    for (const w of ways) counts[w.status] = (counts[w.status] || 0) + 1;
    const wayRows = ways.map((w) => {
      const [cls, label] = STATUS[w.status] || ["", w.status];
      const who = (b) => (b.kind === "glass" ? "glass slab" : b.kind === "collision" ? "the interior's own collision"
        : b.source.startsWith("chunk_") ? `ground and small props (${b.source})` : b.source);
      const by = (w.blockers || []).map(who).slice(0, 3).join(", ");
      return h("tr" + (w.status === "blocked" ? ".hl" : ""), h("td", h("code", w.key)),
        h("td", (w.rooms || []).join(" ↔ ") + (w.outside ? "" : "")),
        h("td", `${w.width.toFixed(2)} × ${w.height.toFixed(2)} m`),
        h("td", h("span.pill" + (cls ? "." + cls : ""), label), w.status !== "window" && w.status !== "door" ? h("span.faint", ` ${w.passage.toFixed(1)} m free`) : null),
        h("td.faint", w.doors && w.doors.length ? w.doors.join(", ") : by || "-"),
        h("td", w.kind === "window" && !map.doorways[w.key] ? h("span.faint", "-") : select(WAY_MODES, map.doorways[w.key], (v) => setWay(w.key, v))));
    });
    const doorRows = doors.map((d) => {
      const src = Object.keys(d.sources || {}).find((n) => !n.startsWith("door_made_")) || "";
      return h("tr", h("td", h("code", d.part)), h("td", OPENING[d.opening] || d.opening),
        h("td", `${Number(d.width).toFixed(2)} × ${Number(d.height).toFixed(2)} m`), h("td", d.devices),
        h("td.faint", FOUND[d.found] || d.found || ""),
        h("td", src && d.found !== "generated" ? select(OBJ_MODES, map.objects[src.toLowerCase()], (v) => setObj(src, v)) : h("span.faint", "doorway mode")));
    });
    const candRows = cands.map((c) => h("tr.hl", h("td", h("code", c.name)), h("td", `${c.width.toFixed(2)} × ${c.height.toFixed(2)} m`),
      h("td", h("code", c.doorway)), h("td", `${c.distance.toFixed(1)} m`),
      h("td", select(OBJ_MODES, map.objects[c.name.toLowerCase()], (v) => setObj(c.name, v)))));

    // any object by name
    const objs = L.door_objects || [];
    const search = h("input", { type: "search", placeholder: `find one of the map's ${objs.length} objects...` });
    const found = h("tbody");
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const list = (q ? objs.filter((o) => o.name.toLowerCase().includes(q)) : objs.filter((o) => map.objects[o.name.toLowerCase()])).slice(0, 40);
      clear(found).append(...list.map((o) => h("tr", h("td", h("code", o.name)), h("td", `×${o.count}`),
        h("td.faint", o.door ? `door (${OPENING[o.door] || o.door})` : ""),
        h("td", select(OBJ_MODES, map.objects[o.name.toLowerCase()], (v) => setObj(o.name, v))))));
      if (!list.length) found.append(h("tr", h("td.faint", { colspan: 4 }, q ? "no object by that name" : "type part of a name (a door's model, a gate, a shutter...)")));
    };
    search.addEventListener("input", draw);
    draw();

    const table = (head, rows, empty) => (rows.length ? h("table.tbl", h("thead", h("tr", head.map((x) => h("th", x)))), h("tbody", rows))
      : h("div.help", empty));
    put(root,
      h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "Doors and doorways"), h("div.grow"), resetBtn, saveBtn),
        h("p.help", "Players get in and out through the doorways of the map's interiors. A door leaf standing in a doorway opens by itself ",
          "(a Door device) whatever its model is called; a doorway can be cut open, or given a door when the map's own door is one of GTA's models. ",
          "Changes apply to the next look and build."),
        h("div.row.wrap", { style: { gap: "6px" } }, Object.entries(counts).map(([k, n]) => {
          const [cls, label] = STATUS[k] || ["", k];
          return h("span.pill" + (cls ? "." + cls : ""), `${n} ${label}`);
        }))),
      h("div.card", h("h3", `Doorways (${ways.length})`),
        h("p.help", "Blocked: something the conversion made solid closes it (a door that did not open, a glass slab). Closed in GTA too: the FiveM map's own collision closes it (a shop window down to the floor)."),
        table(["Doorway", "Rooms", "Size", "Now", "Door / closed by", "Make it"], wayRows, "This map has no interiors with doorways (only MLOs list them).")),
      h("div.card", h("h3", `Doors that open (${doors.reduce((a, d) => a + (d.devices || 0), 0)})`),
        table(["Door", "Opens", "Leaf", "Placed", "Found", "Mapping of its object"], doorRows, "No doors yet: map an object as a door, or make one for a doorway.")),
      cands.length ? h("div.card", h("h3", "Look like doors but stay put"),
        h("p.help", "Thin, door-sized objects next to a doorway that are not doors now. Make one a door, or keep it solid."),
        table(["Object", "Size", "Next to", "Away", "Make it"], candRows, "")) : null,
      h("div.card", h("h3", "Any object"), search, h("table.tbl", { style: { marginTop: "8px" } }, found)));
  });
}

// ------------------------------------------------------------------ options
function optionsTab(root, ctx) {
  const raw = ctx.state.options || {};
  const o = ctx.state.normalized || Object.assign({}, ctx.state.defaults);
  const d = ctx.state.defaults;
  const L = ctx.look;
  const errs = ctx.state.option_errors || [];
  const el = {};
  const text = (k, ph) => (el[k] = h("input", { type: "text", value: raw[k] ?? "", placeholder: ph || "" }));
  const numIn = (k, step) => (el[k] = h("input", { type: "number", step, value: o[k] }));
  const vec = (k, ph) => (el[k] = h("input", { type: "text", value: o[k] ? o[k].join(", ") : "", placeholder: ph }));
  const check = (k) => (el[k] = h("input", { type: "checkbox", checked: !!o[k] }));

  // entity sets
  const esMode = h("select", [["default", "the defaults of each MLO"], ["all", "all of them"], ["none", "none"], ["pick", "these:"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  esMode.value = Array.isArray(o.entity_sets) ? "pick" : o.entity_sets;
  const sets = (L && L.entity_sets) || [];
  const esBoxes = sets.map((n) => [n, h("input", { type: "checkbox", checked: Array.isArray(o.entity_sets) && o.entity_sets.includes(n) })]);
  const esList = h("div.row.wrap", esBoxes.map(([n, cb]) => h("label.choice", cb, h("code", n))));
  const esSync = () => esList.classList.toggle("hidden", esMode.value !== "pick");
  esMode.addEventListener("change", esSync);
  esSync();

  // exclude: archetype names, with the map's own objects to pick from
  const exclude = h("textarea", { rows: 4, placeholder: "one archetype name per line" });
  exclude.value = (o.exclude || []).join("\n");
  const known = [...new Set((L ? L.parts : []).flatMap((p) => Object.keys(p.sources || {})))].sort();
  const pick = h("select", h("option", { value: "" }, `add one of the map's objects (${known.length})...`), known.map((n) => h("option", { value: n }, n)));
  pick.addEventListener("change", () => {
    if (!pick.value) return;
    const cur = exclude.value.split(/\s+/).filter(Boolean);
    if (!cur.includes(pick.value)) exclude.value = [...cur, pick.value].join("\n");
    pick.value = "";
  });

  const doors = h("select", [["devices", "open by themselves (Door devices)"], ["static", "stay as they are"]].map(([v, l]) => h("option", { value: v }, l)));
  doors.value = o.doors;
  const staticOpts = h("div.row", h("label.row", check("solid_doors"), h("span", "doors and shutters block")),
    h("label.row", check("open_gates"), h("span", "gates and barriers do not block")));
  const doorSync = () => staticOpts.classList.toggle("hidden", doors.value !== "static");
  doors.addEventListener("change", doorSync);
  doorSync();

  // quiet interiors: ambient-noise suppression volumes over the MLO floors (mlo/ambience.py)
  const quiet = h("select", [["off", "off (the city is as loud indoors as outdoors)"],
    ["bottom", "quiet the lower floor of every MLO"], ["all", "quiet every MLO floor"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  quiet.value = o.quiet_interiors || "off";

  const parseVec = (s) => {
    const parts = s.split(/[,\s]+/).filter(Boolean).map(Number);
    return parts.length ? parts : null;
  };

  async function save() {
    const patch = {
      name: el.name.value.trim().toLowerCase() || null,
      display_name: el.display_name.value.trim() || null,
      entity_sets: esMode.value === "pick" ? esBoxes.filter(([, cb]) => cb.checked).map(([n]) => n) : esMode.value,
      exclude: exclude.value.split(/\s+/).filter(Boolean),
      collision: el.collision.checked, coverage: el.coverage.checked, doors: doors.value,
      solid_doors: el.solid_doors.checked, open_gates: el.open_gates.checked,
      quiet_interiors: quiet.value,
      cell: Number(el.cell.value), ground_spacing: Number(el.ground_spacing.value),
      collision_budget: Number(el.collision_budget.value), instance_min_tris: Number(el.instance_min_tris.value),
      coords: parseVec(el.coords.value), anchor: parseVec(el.anchor.value),
    };
    if (patch.entity_sets === "pick" || (Array.isArray(patch.entity_sets) && !patch.entity_sets.length)) patch.entity_sets = "none";
    for (const k of Object.keys(patch)) {
      if (JSON.stringify(patch[k]) === JSON.stringify(d[k]) && raw[k] === undefined) delete patch[k];   // leave defaults out
    }
    try {
      const r = await ctx.patch(patch);
      if (r.look_job) location.hash = `#/map/${ctx.pid}/overview`;
      else { clear(root); optionsTab(root, ctx); }
    } catch (_) { /* toast shown */ }
  }

  put(root,
    errs.length ? h("div.card", h("b.err", "The saved options have problems: "), errs.join("; ")) : null,
    h("div.grid2",
      h("div.card", h("h3", "Names"),
        h("div.form",
          h("label.field", "Resource id on the server", text("name", L ? L.map : "from the resource name")),
          h("label.field", "Name players see", text("display_name", L ? L.display_name : ""))),
        h("p.help", "Changing the resource id makes it a different map on a server (the old one stays there).")),
      spotCard(ctx)),
    h("div.grid2",
      h("div.card", h("h3", "What is included"),
        h("label.field", "MLO entity sets", esMode), esList,
        sets.length ? null : h("div.help", "This map's MLOs have no optional entity sets."),
        h("label.field", { style: { marginTop: "10px" } }, "Leave out these objects", exclude), pick,
        h("div.help", "For props that do not belong (a placeholder, a broken object), by their GTA archetype name.")),
      h("div.card", h("h3", "Collision and doors"),
        h("label.row", check("collision"), h("span", "collision (off: you can see the map but walk through it)")),
        h("label.row", check("coverage"), h("span", "check how much of the FiveM collision the boxes reproduce (a minute on big maps)")),
        h("label.field", { style: { marginTop: "10px" } }, "Doors and gates", doors), staticOpts,
        h("label.field", { style: { marginTop: "10px" } }, "Night-city noise indoors", quiet),
        h("div.help", "Interior ambient areas (the game's own amb_interior zones): inside these volumes "
          + "the city ambience ducks away. Needed for basement and lower floors that sit under Night "
          + "City's outdoor sound zones."))),
    h("div.card", h("h3", "Advanced"),
      h("div.form",
        h("label.field", "Chunk cell (m)", numIn("cell", 1)), h("label.field", "Ground collision grid (m)", numIn("ground_spacing", 0.5)),
        h("label.field", "Collision budget per source", numIn("collision_budget", 100)),
        h("label.field", "Own mesh above (triangles)", numIn("instance_min_tris", 100)),
        h("label.field", "FiveM spawn point x, y, z", vec("coords", L && L.coords ? L.coords.map((v) => v.toFixed(2)).join(", ") : "from a coords .txt")),
        h("label.field", "GTA point the map is cut around x, y, z", vec("anchor", L ? L.anchor_gta.join(", ") : "around the spawn point"))),
      h("p.help", "The defaults suit most maps. A different spawn point or GTA point changes the map's own frame (where its anchor sits).")),
    h("div.row", { style: { marginTop: "14px" } }, h("button.btn.ghost", { onclick: async () => {
      if (await confirmBox("Reset options", "Back to the default options (the names and the spot stay)?", "Reset")) {
        const keep = { name: raw.name, display_name: raw.display_name, place: raw.place };
        await api.put(`/projects/${ctx.pid}/map/options`, Object.fromEntries(Object.entries(keep).filter(([, v]) => v != null)));
        await ctx.reload(); clear(root); optionsTab(root, ctx);
      }
    } }, "Reset to defaults"), h("div.grow"), h("button.btn.primary", { onclick: save }, "Save")));
}

// ------------------------------------------------------------------ NCWE asset imports
function assetsTab(root, ctx) {
  const list = h("div"), progress = h("div");
  const fileInput = h("input", { type: "file", multiple: true, class: "hidden",
    onchange: () => { add(picked(fileInput)); fileInput.value = ""; } });
  const dirInput = h("input", { type: "file", multiple: true, class: "hidden",
    onchange: () => { add(picked(dirInput)); dirInput.value = ""; } });
  dirInput.setAttribute("webkitdirectory", "");
  const drop = h("div.dropzone",
    h("div.big", icon("upload")),
    h("div", h("b", "Drop NCWE exports here"), " — an exported mod (.zip, .7z, .rar), a folder, or game archives (", h("code", ".archive"), ")"),
    h("div.help", { style: { marginTop: "6px" } }, "Imports ship with the map's next build as extra preload packages, so the props, lights and meshes you make in NCWE load wherever this map is installed. Open77 loads 8 packages per resource (the map's own included)."),
    h("div.row", { style: { marginTop: "12px", justifyContent: "center" } },
      h("button.btn", { onclick: () => fileInput.click() }, "Choose files"),
      h("button.btn", { onclick: () => dirInput.click() }, "Choose a folder")),
    fileInput, dirInput);
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => { droppedFiles(e.dataTransfer).then(add); });

  // the folder picker sets webkitRelativePath on its files; the plain picker is flat
  function picked(input) {
    return groupLoose([...input.files].map((f) => [f.webkitRelativePath || f.name, f]));
  }

  async function add(files) {
    if (!files.length) return;
    clear(progress);
    for (const [name, file] of files) {
      const bar = h("div.progress", h("div", { style: { width: "0%" } }));
      const state = h("span.faint", "uploading");
      const row = h("div.card", { style: { padding: "10px 14px" } },
        h("div.row", h("b", name), h("span.muted", fmtBytes(file.size)), h("div.grow"), state), bar);
      progress.append(row);
      try {
        await uploadFile(`/projects/${ctx.pid}/ncwe_assets?name=${encodeURIComponent(name)}`, file,
          (p) => { bar.firstChild.style.width = `${(p * 100).toFixed(0)}%`; });
        state.textContent = "imported";
        state.className = "ok";
      } catch (e) {
        state.textContent = e.message;
        state.className = "err";
      }
    }
    await load();
  }

  const shown = new Map();            // expanded contents rows, by import name

  function renderContents(c) {
    const box = h("div", { style: { display: "grid", gap: "6px" } });
    for (const a of c.archives) {
      box.append(h("div",
        h("b", a.name),
        h("span.muted", a.entries == null ? "" : ` — ${a.entries} file${a.entries === 1 ? "" : "s"} inside`),
        a.error ? h("span.warn", ` — ${a.error}`) : null));
      if (a.resources.length) {
        box.append(h("div.row", { style: { flexWrap: "wrap", gap: "6px", marginLeft: "14px" } },
          ...a.resources.map((r) => h("span.chip", { title: r.path },
            h("span", r.path.replace(/\\/g, "/").split("/").pop()),
            h("span.n", r.kind + (r.present === false ? " · not in archive" : ""))))));
      }
    }
    if (c.files.length) box.append(h("div.muted", "Other files: " + c.files.map((f) => f.path).join(", ")));
    const singular = { meshes: "mesh", props: "prop", textures: "texture", sounds: "sound", other: "other", entries: "entry" };
    const totals = Object.entries(c.totals).map(([k, v]) => `${v} ${v === 1 ? (singular[k] || k) : k}`).join(", ");
    if (totals) box.append(h("div.help", totals));
    for (const n of c.notes) box.append(h("div.help", n));
    return box;
  }

  function toggleContents(a, row) {
    const open = shown.get(a.name);
    if (open) { open.remove(); shown.delete(a.name); return; }
    const cell = h("td", { colSpan: 5 }, h("div", { style: { padding: "6px 8px 12px" } }, h("span.faint", "reading the import…")));
    const detail = h("tr", cell);
    shown.set(a.name, detail);
    row.after(detail);
    api.get(`/projects/${ctx.pid}/ncwe_assets/${encodeURIComponent(a.name)}`).then((r) => {
      clear(cell.firstChild);
      cell.firstChild.append(
        h("div.help", "Names come from the mod's .archive.xl files (game archives index their contents by hash)."),
        renderContents(r.contents));
    }).catch((e) => { clear(cell.firstChild); cell.firstChild.append(h("span.err", e.message)); });
  }

  async function load() {
    let r;
    try { r = await api.get(`/projects/${ctx.pid}/ncwe_assets`); } catch (e) { toast(e.message, "err"); return; }
    clear(list);
    shown.clear();
    if (!r.assets.length) {
      list.append(h("div.empty", "Nothing imported yet. Drop an NCWE export above."));
      return;
    }
    if (r.collisions && r.collisions.length) {
      list.append(h("div.banner", { style: { padding: "10px 12px", marginBottom: "10px", display: "grid", gap: "4px" } },
        h("b", "Archive name collisions — fix before building"),
        ...r.collisions.map((c) => h("div", c.problem))));
    }
    if (r.build_stale) list.append(staleBanner(ctx, r.build_stale));
    const body = h("tbody");
    for (const a of r.assets) {
      const tr = h("tr",
        h("td", h("div.title", a.name),
          a.archive_names && a.archive_names.length ? h("div.help", a.archive_names.join(", ")) : null),
        h("td.muted", a.kind),
        h("td.num", fmtBytes(a.bytes)),
        h("td", a.ready == null ? h("span.pill", "checked at build")
          : a.ready ? h("span.pill.ok", `${a.archives} archive${a.archives === 1 ? "" : "s"}`)
            : h("span.pill.err", "no game archives")),
        h("td", { style: { textAlign: "right", whiteSpace: "nowrap" } },
          h("button.btn.ghost.small", { title: "What this import contains (meshes, props, archives)",
            onclick: () => toggleContents(a, tr) }, "Contents"),
          h("button.btn.ghost.small", { title: "Remove this import", onclick: async () => {
            if (await confirmBox("Remove import", `Remove ${a.name} from this project? The next build will not ship it.`, "Remove", "danger")) {
              try { await api.del(`/projects/${ctx.pid}/ncwe_assets/${encodeURIComponent(a.name)}`); await load(); } catch (e) { toast(e.message, "err"); }
            }
          } }, icon("trash"))));
      body.append(tr);
    }
    list.append(h("div.card.table-card", h("table.tbl.list",
      h("thead", h("tr", h("th", "Import"), h("th", "Kind"), h("th.num", "Size"), h("th", "Ships with builds"), h("th", ""))),
      body)));
  }

  // the other direction: this map into the user's NCWE editor
  const outBox = h("div");
  async function toNcwe() {
    try {
      const job = await api.post(`/projects/${ctx.pid}/ncwe_import`, {});
      const log = h("div.log");
      clear(outBox).append(h("div.card", h("div.row", h("b", "Importing into NCWE"), pill(job.state)), log));
      const j = await waitJob(job.id, (jj) => clear(log).append(...logLines(jj.allLines)));
      if (j.state === "done" && j.result) {
        const r = j.result;
        toast(r.note || "Imported into NCWE", "ok");
        clear(log).append(h("div", r.note || ""),
          r.asset ? h("div.kv", h("span", "mesh"), h("code", r.asset),
            h("span", "placed"), h("span", `${(r.placed || []).length} object${(r.placed || []).length === 1 ? "" : "s"}`)) : null);
      } else log.append(h("div.err", j.error || j.state));
    } catch (e) { toast(e.message, "err"); }
  }
  const send = h("div.card", { style: { marginTop: "14px" } },
    h("h3", "Send this map to NCWE"),
    h("p.help", "Exports the map as a glb and imports it into your NCWE editor's custom mesh library (import_model), " +
      "placed in the NCWE world at the map's Night City spot when one is set in Options. NCWE must be open and " +
      "signed in (Settings > Agents > NCWE MCP executable)."),
    h("div.row", h("button.btn", { onclick: toNcwe }, "Import to NCWE")),
    outBox);
  root.append(drop, progress, list, send);
  load();
}

// ------------------------------------------------------------------ build
function buildTab(root, ctx) {
  const live = h("div");
  const hist = h("tbody");
  const detail = h("div");
  const staleBox = h("div");

  async function history() {
    const builds = await api.get(`/projects/${ctx.pid}/builds`);
    clear(hist);
    clear(staleBox);
    if (builds[0] && builds[0].stale) staleBox.append(staleBanner(ctx, builds[0].stale));
    if (!builds.length) hist.append(h("tr", h("td", { colSpan: 8 }, h("span.faint", "no builds yet"))));
    for (const b of builds) {
      const size = (b.packages || []).reduce((a, p) => a + p.bytes, 0);
      hist.append(h("tr", h("td", `#${b.n}`), h("td", fmtTime(b.time)),
        h("td", h("div.row", { style: { gap: "6px" } }, b.ok ? h("span.pill.ok", "ok") : h("span.pill.err", "failed"),
          b.stale ? h("span.pill.warn", { title: b.stale.reason }, "stale") : null)),
        h("td", b.qa ? (b.qa.ok ? h("span.ok", "passed") : h("span.err", `${b.qa.failed.length} failed`)) : "-"),
        h("td.muted", b.baked ? fmtSpot(b.baked.anchor) : (b.ok ? "not baked" : "")),
        h("td.num", size ? fmtBytes(size) : ""), h("td.num", b.seconds ? `${Math.round(b.seconds)}s` : ""),
        h("td", b.ok ? h("div.row", h("button.btn.small", { onclick: () => showBuild(b.n) }, "details"),
          h("a.btn.small", { href: `/api/projects/${ctx.pid}/builds/${b.n}/download` }, "zip")) : h("span.err", { title: b.error || "" }, (b.error || "").slice(0, 70)))));
    }
  }

  async function showBuild(n) {
    const info = await api.get(`/projects/${ctx.pid}/builds/${n}`);
    const b = info.build;
    const r = info.report || {};
    const bd = r.build || {};
    const cov = (bd.coverage || {});
    const col = cov.collision || {};
    const vis = cov.visual_floors || {};
    const q = info.qa;
    clear(detail).append(h("div.card", h("h3", `Build #${n}`, h("span.muted", b.version)),
      h("div.kv", h("span", "resource"), h("code", b.name), h("span", "baked at"), h("span", b.baked ? fmtSpot(b.baked.anchor) : "not baked"),
        h("span", "packages"), h("span", (b.packages || []).map((p) => `${p.file} ${fmtBytes(p.bytes)}`).join(", ")),
        h("span", "parts / placements"), h("span", `${b.parts ?? "-"} / ${b.placements ?? "-"}`),
        h("span", "doors and gates"), h("span", `${b.doors || 0} Door devices`), h("span", "glass"), h("span", `${b.glass || 0} slabs in open windows`)),
      cov.collision ? h("div", { style: { marginTop: "12px" } }, h("h4", "Collision coverage"),
        h("div.facts", fact("Walkable", `${col.walkable.covered_pct}%`), fact("Walls", `${col.walls.covered_pct}%`),
          fact("Visible floors solid", `${vis.solid_pct ?? "-"}%`), fact("Boxes", (col.colliders || 0).toLocaleString())),
        (col.holes || []).length ? h("div", h("div.help", { style: { margin: "8px 0 4px" } }, "Biggest gaps (map metres; add the spot for Night City coordinates):"),
          h("table.tbl", h("thead", h("tr", h("th", "x"), h("th", "y"), h("th", "z"), h("th.num", "m²"), h("th", "FiveM collision"))),
            h("tbody", col.holes.slice(0, 8).map((x) => h("tr", h("td", x.x), h("td", x.y), h("td", x.z), h("td.num", x.m2), h("td.muted", (x.sources || []).join(", "))))))) : null) : null,
      (bd.doors || []).length ? h("div", { style: { marginTop: "12px" } }, h("h4", "Doors and gates"),
        h("table.tbl", h("thead", h("tr", h("th", "model"), h("th", "opens"), h("th.num", "width"), h("th.num", "devices"))),
          h("tbody", bd.doors.map((dd) => h("tr", h("td", h("code", dd.part.replace(/^door_/, ""))), h("td", dd.opening.toLowerCase().replace(/_/g, " ")),
            h("td.num", `${dd.width} m`), h("td.num", dd.devices)))))) : null,
      q ? h("div.checks", { style: { marginTop: "12px" } }, q.checks.map((c) => h("div.check" + (c.ok ? "" : "." + c.level), c.message))) : null,
      (b.warnings || []).length ? h("div", { style: { marginTop: "10px" } }, h("h4", "warnings"), b.warnings.map((w) => h("div.help", w))) : null,
      h("div.row", { style: { marginTop: "12px" } }, h("button.btn.small", { onclick: async () => { await api.post(`/projects/${ctx.pid}/builds/${n}/qa`); showBuild(n); } }, "Run checks again"),
        h("button.btn.small", { onclick: () => api.post(`/projects/${ctx.pid}/open`, { what: "build" }).then((x) => { if (!x.ok) toast(x.path); }) }, "Open folder"),
        h("a.btn.small", { href: `#/map/${ctx.pid}/game` }, "Try it in my game"), h("a.btn.small", { href: `#/map/${ctx.pid}/deploy` }, "Install on a server"))));
  }

  function attach(job) {
    const bar = h("div.progress" + (job.progress == null ? ".indef" : ""), h("div", { style: { width: `${(job.progress || 0) * 100}%` } }));
    const step = h("span.muted", job.step || "");
    const log = h("div.log");
    const cancel = h("button.btn.danger.small", { onclick: () => api.post(`/jobs/${job.id}/cancel`) }, "Cancel");
    clear(live).append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, job.title), h("span.st", pill(job.state)), step, h("div.grow"), cancel), bar,
      h("div", { style: { height: "10px" } }), log));
    waitJob(job.id, (j) => {
      bar.className = "progress" + (j.progress == null && j.state === "running" ? " indef" : "");
      bar.firstChild.style.width = `${(j.progress || (j.state === "done" ? 1 : 0)) * 100}%`;
      step.textContent = j.step || "";
      clear(log).append(...logLines(j.allLines));
      log.scrollTop = log.scrollHeight;
    }).then((j) => {
      cancel.remove();
      live.querySelector(".st").replaceChildren(pill(j.state));
      history();
      ctx.reload();
      if (j.state === "done" && j.result) showBuild(j.result.n);
    });
  }

  const running = [...store.jobs.values()].find((j) => j.project === ctx.pid && j.kind === "build" && ["queued", "running"].includes(j.state));
  const opts = ctx.state.normalized || {};
  root.append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "Build"), h("div.grow"),
    h("button.btn.primary", { onclick: async () => {
      try { attach(await api.post(`/projects/${ctx.pid}/build`)); } catch (e) { toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err"); }
    } }, "Build now")),
    h("p.help", "WolvenKit writes the meshes, textures, collision and Door devices, then the map is baked at its spot ",
      opts.place ? h("b", fmtSpot(opts.place)) : h("b", "(none set: it is built but not baked)"),
      ". A big MLO takes a few minutes; the result is checked afterwards.")),
    staleBox, live, h("div.card", h("h3", "Builds"), h("table.tbl", h("thead", h("tr", h("th", ""), h("th", "when"), h("th", "build"), h("th", "checks"),
      h("th", "baked at"), h("th.num", "size"), h("th.num", "time"), h("th", ""))), hist)), detail);
  history();
  if (running) attach(running);
}

// ------------------------------------------------------------------ the local game
function gameTab(root, ctx) {
  const g = ctx.state.game || {};
  const builds = (ctx.state.builds || []).filter((b) => b.ok);
  const out = h("div");
  const buildSel = h("select", builds.map((b) => h("option", { value: b.n }, `#${b.n} - ${fmtTime(b.time)}${b.baked ? "" : " (not baked)"}`)));
  async function run(body) {
    try {
      const job = await api.post(`/projects/${ctx.pid}/map/game`, body);
      const log = h("div.log");
      clear(out).append(h("div.card", log));
      const j = await waitJob(job.id, (jj) => clear(log).append(...logLines(jj.allLines)));
      if (j.state === "done") { toast(body.uninstall ? "Removed from the game" : "Installed in the game", "ok"); await ctx.reload(); clear(root); gameTab(root, ctx); }
      else log.append(h("div.e", j.error || j.state));
    } catch (e) { toast(e.message, "err"); }
  }
  root.append(h("div.split",
    h("div.card", h("h3", "Try it in your own game"),
      h("p.help", "Writes the build's packages into your game's archive\\pc\\mod (only this map's fivem2open77_map_ files), for a direct test boot of the game. ",
        "Players of a server get the map from the server instead: there the packages download with the launcher."),
      g.game ? h("div.kv", h("span", "game"), h("code", g.game), h("span", "installed"),
        h("span", g.files && g.files.length ? `${g.files.length} file(s)${g.build ? ` from build #${g.build}` : ""}` : "nothing of this map")) :
        h("div.banner", h("span", "Cyberpunk 2077 was not found."), h("div.grow"), h("a.btn.small", { href: "#/settings/tools" }, "Settings")),
      builds.length ? h("div.row", { style: { marginTop: "12px" } }, h("span.muted", "Build"), buildSel,
        h("button.btn.primary", { disabled: !g.game, onclick: () => run({ build: Number(buildSel.value) }) }, "Install in my game"),
        g.files && g.files.length ? h("button.btn.danger", { onclick: () => run({ uninstall: true }) }, "Remove from my game") : null)
        : h("p.muted", "Build the map first."),
      g.files && g.files.length ? h("div.help", { style: { marginTop: "10px" } }, g.files.join(", ")) : null),
    h("div.card", h("h3", "What you see in game"),
      h("div.help", "The map stands at the spot it was baked at. Doors and gates open as you walk up to them; open windows have glass. ",
        "Moving it needs the map manager on a server (F9 in game, or Placement here), which rebuilds it for the new spot."))), out);
}

// ------------------------------------------------------------------ servers
function deployTab(root, ctx) {
  const servers = store.state.settings.servers;
  const builds = (ctx.state.builds || []).filter((b) => b.ok);
  if (!servers.length) {
    root.append(h("div.card", h("h3", "No servers yet"), h("p.muted", "Add your Open77 server (SSH) in Servers first."), h("a.btn.primary", { href: "#/servers" }, "Add a server")));
    return;
  }
  if (!builds.length) {
    root.append(h("div.card", h("h3", "Nothing built yet"), h("p.muted", "Build the map before installing it."), h("button.btn.primary", { onclick: () => startMapBuild(ctx) }, "Build")));
    return;
  }
  const srvSel = h("select", servers.map((s) => h("option", { value: s.id }, `${s.label} (${s.host})`)));
  const def = store.state.settings.deploy.default_server;
  const lastDep = (ctx.state.deployments || []).slice(-1)[0];
  if (lastDep && servers.find((s) => s.id === lastDep.server)) srvSel.value = lastDep.server;
  else if (def) srvSel.value = def;
  const names = h("div.row");
  const buildSel = h("select", builds.map((b) => h("option", { value: b.n }, `#${b.n} - ${fmtTime(b.time)} (${b.version})${b.baked ? "" : " not baked"}`)));
  const keep = h("input", { type: "checkbox", checked: true });
  const restart = h("input", { type: "checkbox", checked: true });
  const baker = h("input", { type: "checkbox", checked: true });
  const out = h("div");
  const statusBox = h("div");
  const toolsOk = store.state.tools.linux_dotnet && store.state.tools.linux_dotnet.ok;

  function fillNames() {
    const s = servers.find((x) => x.id === srvSel.value);
    clear(names).append(...s.names.map((n) => h("label.row", h("input", { type: "checkbox", value: n, checked: true }), h("code", n))));
  }
  srvSel.addEventListener("change", () => { fillNames(); clear(statusBox); });
  fillNames();

  async function runJob(job, box) {
    const log = h("div.log");
    clear(box).append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, job.title), h("div.grow"), h("span.st", pill(job.state))), log));
    const j = await waitJob(job.id, (jj) => { clear(log).append(...logLines(jj.allLines)); log.scrollTop = log.scrollHeight; });
    box.querySelector(".st").replaceChildren(pill(j.state));
    if (j.state !== "done" && j.error) log.append(h("div.e", j.error));
    return j;
  }

  async function status() {
    clear(statusBox).append(h("div.card", h("span.muted", "asking the server...")));
    const job = await api.post(`/servers/${srvSel.value}/status`, {});
    const j = await waitJob(job.id);
    clear(statusBox);
    if (j.state !== "done") { statusBox.append(h("div.card.err", j.error || "status failed")); return; }
    const mapName = (ctx.project.map || {}).name;
    for (const s of j.result) {
      // the same FiveM map already there under another resource id: installing would add a second copy
      const L = ctx.look || {};
      const twin = (s.maps || []).find((m) => m.name !== mapName && m.display && [L.converter_display, L.display_name].includes(m.display));
      const warn = twin ? h("div.banner", { style: { marginBottom: "8px" } }, h("b.warn", "Already there?"),
        h("span", `${s.name} has a map "${twin.display}" as ${twin.name}. This project installs as ${mapName}: a second copy. `,
          "To update that one instead, set the resource id to ", h("code", twin.name), " in Options."),
        h("div.grow"), h("button.btn.small", { onclick: async () => {
          await ctx.patch({ name: twin.name });
          status();
        } }, `Use ${twin.name}`)) : null;
      statusBox.append(serverMapCard(s, srvSel.value, mapName, { onChange: status, runJob: (job2) => runJob(job2, out), pid: ctx.pid, extra: warn }));
    }
  }

  root.append(h("div.split",
    h("div.card", h("h3", "Install on a server"),
      h("div.form", h("label.field", "Server", srvSel), h("label.field", "Build", buildSel)),
      h("div", { style: { margin: "10px 0" } }, h("div.muted", "Server folders"), names),
      h("label.choice", keep, h("span", h("b", "Keep the spot it was moved to in game"), h("span.help", " - a map already there is rebuilt at its current spot (off: the build's spot)"))),
      h("label.choice", restart, h("span", h("b", "Restart the server when nobody is online"), h("span.help", " - players get the map at their next launch; otherwise at the next server start"))),
      h("label.choice", baker, h("span", h("b", "Install / update the map baker"), h("span.help", " - rebuilds the map on the server when it is moved in game (.NET + WolvenKit for Linux, ~70 MB the first time)"))),
      toolsOk ? null : h("div.help.warn", "No Linux .NET runtime on this PC (Settings > Tools): the baker cannot be installed from here."),
      h("div.row", { style: { marginTop: "14px" } }, h("button.btn", { onclick: status }, "Check server"), h("div.grow"),
        h("button.btn.primary", { onclick: async () => {
          const chosen = [...names.querySelectorAll("input:checked")].map((i) => i.value);
          if (!chosen.length) return toast("choose at least one server folder", "err");
          try {
            const job = await api.post(`/projects/${ctx.pid}/deploy`, { server: srvSel.value, names: chosen, build: Number(buildSel.value),
              keep_placement: keep.checked, restart: restart.checked, baker: baker.checked });
            const j = await runJob(job, out);
            if (j.state === "done") { await ctx.reload(); status(); }
          } catch (e) { toast(e.message, "err"); }
        } }, "Install"))),
    h("div", statusBox)), out);
}

// A server folder's maps, manager, baker and agent API, with what can be done about them.
export function serverMapCard(s, sid, mapName, { onChange, runJob, pid, extra } = {}) {
  const mm = s.mapManager;
  const mb = s.mapBaker || {};
  const http = s.http || {};
  const map = (s.maps || []).find((m) => m.name === mapName);
  const run = async (path, body, confirmText) => {
    if (confirmText && !(await confirmBox("Are you sure?", confirmText, "Go"))) return;
    try {
      const job = await api.post(path, body);
      const j = runJob ? await runJob(job) : await waitJob(job.id);
      if (j.state !== "done") toast(j.error || "failed", "err");
      if (onChange) onChange();
    } catch (e) { toast(e.message, "err"); }
  };
  return h("div.card", h("h3", h("code", s.name), h("span.muted", s.version || ""), pill(s.unit === "active" ? "running" : s.unit),
    h("span.muted", `${(s.online || []).length} online`)),
    extra || null,
    h("div.kv",
      mapName ? h("span", "this map") : null,
      mapName ? h("span", map ? `${map.version} at ${fmtSpot(map.anchor)}${map.in_load ? "" : " (not in resources.load)"}` : "not installed") : null,
      h("span", "maps"), h("span", (s.maps || []).map((m) => m.name).join(", ") || "none"),
      h("span", "map manager"), h("span", mm ? `${mm.version}${mm.in_load ? "" : " (not in resources.load)"}${mm.token ? "" : ", not started yet"}` : "not installed"),
      h("span", "map baker"), h("span", mb.toolkit ? `${mb.service}, ${mb.toolkit}${mb.hook ? ", start hook" : ", NO start hook"}` : "not installed"),
      h("span", "staged moves"), h("span", (mb.staged || []).join(", ") || "-"),
      h("span", "agent API"), h("span", http.enabled ? h("span.ok", `on (${http.listenUrl})`) : h("span.warn", "off: the Studio and agents cannot move maps"))),
    h("div.row", { style: { marginTop: "10px" } },
      !http.enabled ? h("button.btn.small.primary", { onclick: () => run(`/servers/${sid}/agent-api`, { names: [s.name], enable: true, restart: (s.online || []).length === 0 },
        `Turn on the HTTP handler listener (httpHandlers, loopback only) on ${s.name}? The server restarts now if nobody is online, otherwise at its next start.`) }, "Turn on the agent API") : null,
      h("a.btn.small", { href: `#/live/${sid}/${encodeURIComponent(s.name)}${mapName ? "/" + mapName : ""}` }, "Live maps"),
      pid ? h("a.btn.small", { href: `#/map/${pid}/place` }, "Placement") : null,
      h("button.btn.small.danger", { onclick: () => run(`/servers/${sid}/restart`, { names: [s.name] }, `Restart ${s.name} now? It is refused while players are online.`) }, "Restart now"),
      h("button.btn.small.ghost", { onclick: async () => {
        const r = await api.get(`/servers/${sid}/logs?name=${encodeURIComponent(s.name)}&lines=300`);
        modal(`${s.name} log`, h("div.log", { style: { maxHeight: "60vh" } }, logLines(r.text.split("\n"))), [{ label: "Close" }]);
      } }, "Server log")));
}

// ------------------------------------------------------------------ placement
function placeTab(root, ctx) {
  const m = ctx.project.map || {};
  if (!m.name) {
    root.append(h("div.card", h("h3", "Not looked at yet"), h("p.muted", "The map needs its name first: open Overview.")));
    return;
  }
  const dep = (ctx.state.deployments || []).slice(-1)[0];
  const d = placePanel(root, { mapName: m.name, look: ctx.look, pid: ctx.pid, lookKey: ctx.state.look_key,
    server: dep ? dep.server : store.state.settings.deploy.default_server, folder: dep ? (dep.names || [])[0] : null });
  ctx.disposers.push(d);
}
