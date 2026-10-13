// One car: overview, 3D, parts, lights, handling, sound, build, deploy, AI agent.
import { api, waitJob } from "../api.js";
import { store, onEvent } from "../store.js";
import { h, clear, toast, modal, confirmBox, prompt, fmtBytes, fmtTime, fmtAgo, pill, logLines, debounce } from "../ui.js";
import { viewerTab } from "./viewer.js";
import { agentTab } from "./agent.js";

const TABS = [["overview", "Overview"], ["3d", "3D"], ["parts", "Parts"], ["lights", "Lights"], ["neon", "Neon"],
  ["handling", "Handling"], ["sound", "Sound"], ["build", "Build"], ["deploy", "Deploy"], ["agent", "AI agent"]];

export function projectPage(root, args) {
  const pid = args[0];
  const tab = args[1] || "overview";
  const ctx = { pid, project: null, inspect: null, profile: null, disposers: [], root };
  const head = h("div.page-head");
  const tabs = h("div.tabs", TABS.map(([k, label]) => h("a", { href: `#/project/${pid}/${k}`, class: k === tab ? "active" : "" }, label)));
  const body = h("div", h("div.empty", "Loading..."));
  root.append(head, tabs, body);

  ctx.reload = async (what = "all") => {
    if (what === "all" || what === "project") {
      ctx.project = await api.get(`/projects/${pid}`);
      ctx.profile = ctx.project.profile || {};
      renderHead();
    }
    if (what === "all" || what === "inspect") {
      ctx.inspect = await api.get(`/projects/${pid}/inspect`);
    }
  };
  ctx.patch = async (patch, { quiet } = {}) => {
    try {
      const r = await api.patch(`/projects/${pid}/profile`, patch);
      ctx.profile = r.profile;
      if (!quiet) toast("Saved", "ok");
      return r;
    } catch (e) {
      toast((e.errors && e.errors.length ? e.errors.join("; ") : e.message), "err");
      throw e;
    }
  };
  ctx.version = Date.now();

  function renderHead() {
    const p = ctx.project;
    const car = p.car || {};
    clear(head).append(
      h("a.btn.ghost.small", { href: "#/projects" }, "← Cars"),
      h("div", h("h1", p.name || "(unnamed)"),
        h("div.row.muted", { style: { fontSize: "13px" } }, car.record ? h("code", car.record) : null,
          car.class ? h("span", car.class.replace("VC_", "").toLowerCase()) : null,
          h("span.faint", p.source))),
      h("div.grow"),
      h("button.btn.ghost", { onclick: async () => {
        const n = await prompt("Rename", "Project name", p.name);
        if (n) { await api.patch(`/projects/${pid}`, { name: n }); ctx.reload("project"); }
      } }, "Rename"),
      h("button.btn.danger", { onclick: async () => {
        if (await confirmBox("Delete project", `Delete ${p.name} and all its builds from the Studio? Servers are not touched.`, "Delete", "danger")) {
          await api.del(`/projects/${pid}`);
          location.hash = "#/projects";
        }
      } }, "Delete"),
      h("button.btn.cyan", { onclick: () => { location.hash = `#/project/${pid}/agent`; } }, "Ask AI"),
      h("button.btn.primary", { onclick: () => startBuild(ctx) }, "Build"));
  }

  (async () => {
    try {
      await ctx.reload("project");
      clear(body).append(h("div.empty", "Reading the car..."));
      await ctx.reload("inspect");
      const fn = { overview, "3d": viewerTab, parts, lights, neon: neonTab, handling, sound, build: buildTab, deploy: deployTab, agent: agentTab }[tab] || overview;
      clear(body);
      fn(body, ctx);
    } catch (e) {
      clear(body).append(h("div.card", h("b.err", "Could not read this car: "), e.message));
    }
  })();
  return { dispose: () => ctx.disposers.forEach((d) => d()) };
}

// ------------------------------------------------------------------ neon
const NEON_SIDES = [["left", "left sill"], ["right", "right sill"], ["front", "under the nose"], ["back", "under the tail"]];

function neonTab(root, ctx) {
  const d = ctx.inspect.neon || { palette: [], kit: null, enabled: false };
  const conf = Object.assign({ enabled: true, sides: ["left", "right", "front", "back"], colors: "gta", default: "on",
    brightness: 1, spread: 2, tubes: false, height_offset: 0 }, ctx.profile.neon || {});
  const kit = d.kit;
  const enabled = h("input", { type: "checkbox", checked: conf.enabled });
  const showTubes = h("input", { type: "checkbox", checked: !!conf.tubes });
  const sides = NEON_SIDES.map(([k, label]) => [k, h("input", { type: "checkbox", checked: conf.sides.includes(k) }), label]);
  // colours: the GTA palette (all by default) plus custom "#rrggbb" ones
  const custom = (Array.isArray(conf.colors) ? conf.colors : []).filter((c) => typeof c === "object" || /^#?[0-9a-f]{6}$/i.test(c));
  const chosen = new Set(conf.colors === "gta" || !Array.isArray(conf.colors) ? d.palette.map((c) => c.key)
    : conf.colors.filter((c) => typeof c === "string").map((c) => c.toLowerCase().replace(/[^a-z0-9]+/g, "_")));
  const swatches = d.palette.map((c) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(c.key) });
    return [c, box, h("label.choice", box, h("span.swatch", { style: { background: c.hex } }), h("span", c.label))];
  });
  const customRows = h("div");
  const addCustom = (name = "", hex = "#00ffcc") => {
    const n = h("input", { type: "text", value: name, placeholder: "name", style: { width: "140px" } });
    const col = h("input", { type: "color", value: hex.startsWith("#") ? hex : "#" + hex });
    const row = h("div.row", n, col, h("button.btn.small.ghost", { onclick: () => row.remove() }, "remove"));
    row._get = () => (n.value.trim() ? { name: n.value.trim(), color: col.value } : null);
    customRows.append(row);
  };
  for (const c of custom) addCustom(typeof c === "object" ? c.name : "", typeof c === "object" ? c.color : c);
  const def = h("select", h("option", { value: "on" }, "on (the first colour)"),
    h("option", { value: "off" }, "off (kit fitted, lights off)"),
    (kit ? kit.colors : d.palette).map((c) => h("option", { value: c.key }, c.label)));
  def.value = conf.default || "on";
  const bright = h("input", { type: "range", min: 0.2, max: 4, step: 0.1, value: conf.brightness });
  const brightO = h("output", Number(conf.brightness).toFixed(1) + "x");
  bright.oninput = () => { brightO.textContent = Number(bright.value).toFixed(1) + "x"; };
  const spread = h("input", { type: "range", min: 0.5, max: 5, step: 0.1, value: conf.spread });
  const spreadO = h("output", Number(conf.spread).toFixed(1) + " m");
  spread.oninput = () => { spreadO.textContent = Number(spread.value).toFixed(1) + " m"; };
  const lift =h("input", { type: "range", min: -10, max: 10, step: 0.5, value: Math.round((conf.height_offset || 0) * 1000) / 10 });
  const liftO = h("output", `${lift.value} cm`);
  lift.oninput = () => { liftO.textContent = `${lift.value} cm`; };

  let preview = kit ? (def.value !== "off" && def.value !== "on" ? def.value : kit.colors[0].key) : null;
  const img = h("img.sheet", { alt: "neon kit from below" });
  const showImg = () => { if (preview) img.src = `/api/projects/${ctx.pid}/neon.png?color=${preview}&v=${ctx.version}`; };
  showImg();
  const previewBar = kit ? h("div.row.wrap", { style: { gap: "6px", marginBottom: "8px" } },
    kit.colors.map((c) => h("button.btn.small", { title: c.label, style: { borderColor: c.hex }, onclick: () => { preview = c.key; showImg(); } },
      h("span.swatch", { style: { background: c.hex } }), c.label))) : null;

  async function save() {
    const keys = swatches.filter(([, box]) => box.checked).map(([c]) => c.key);
    const extra = [...customRows.children].map((r) => r._get()).filter(Boolean);
    const allGta = keys.length === d.palette.length && !extra.length;
    const patch = { neon: {
      enabled: enabled.checked,
      sides: sides.filter(([, box]) => box.checked).map(([k]) => k),
      colors: allGta ? "gta" : [...keys, ...extra],
      default: def.value, brightness: Number(bright.value), spread: Number(spread.value),
      tubes: showTubes.checked, height_offset: Number(lift.value) / 100,
    } };
    await ctx.patch(patch);
    ctx.version = Date.now();
    await ctx.reload("inspect");
    clear(root);
    neonTab(root, ctx);
  }

  const tubes = kit ? Object.entries(kit.tubes) : [];
  root.append(h("div.split",
    h("div",
      h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "How it mounts"), h("div.grow"),
        h("span.muted", "from below / below front / left / below rear / the road at night, from above and the front corner")),
        kit ? previewBar : null,
        kit ? img : h("div.empty", conf.enabled ? "No tube fits under this car (see below)." : "The kit is turned off."),
        h("div.help", { style: { marginTop: "8px" } },
          "The tubes run just under the body: from the model's neon bones when it has them (",
          d.gta_bones.length ? d.gta_bones.join(", ") : "this one has none", "), otherwise measured from the underside. ",
          kit && kit.tubes_visible ? "They show in game as glowing strips. "
            : "Like GTA V's neon they are not drawn in game (the dots mark where the light comes from): only the glow shows. ",
          "Soft lights under the sills wash the road around the car in the kit's colour, the way a real LED kit's light scatters on the road; ",
          "they shine with the car's lights (the car hub switches them on with a colour) ",
          "(bottom pictures: about how far and how bright; the game's exposure, the surface and the weather decide the rest). ",
          "Brightness and Glow reach change it.")),
      kit ? h("div.card", h("h3", "Tubes"), h("table.tbl", h("thead", h("tr", h("th", "side"), h("th", "length"), h("th", "height"), h("th", "placed by"))),
        h("tbody", tubes.map(([side, t]) => h("tr", h("td", side), h("td", `${t.length.toFixed(2)} m`),
          h("td", `${(t.clearance * 100).toFixed(0)} cm off the road`),
          h("td.muted", t.source))))),
        h("div.help", `${kit.lights} soft lights under the car` + (kit.light_height
          ? `, ${Math.round(kit.light_height[0] * 100)}-${Math.round(kit.light_height[1] * 100)} cm off the road.` : "."))) : null,
      (kit && kit.warnings.length) || (!kit && (ctx.inspect.warnings || []).some((w) => w.startsWith("neon"))) ?
        h("div.card", h("h3", h("span.warn", "⚠"), "Notes"),
          [...(kit ? kit.warnings : ctx.inspect.warnings.filter((w) => w.startsWith("neon")))].map((w) => h("div.help", w))) : null),
    h("div",
      h("div.card", h("h3", "Neon kit"),
        h("label.row", enabled, h("span", "fit a GTA V style neon kit (every colour is an appearance of the car)")),
        h("label.row", showTubes, h("span", "show the LED tubes as glowing strips (off: light only, like GTA V)")),
        h("div.help", "Sides"), h("div.row.wrap", sides.map(([, box, label]) => h("label.choice", box, h("span", label)))),
        h("div.form", { style: { marginTop: "10px" } },
          h("label.field", "When the car spawns", def),
          h("label.field", "Brightness", h("div.row", bright, brightO)),
          h("label.field", "Glow reach", h("div.row", spread, spreadO)),
          h("label.field", "Height (+ up, - down)", h("div.row", lift, liftO))),
        h("div.row", { style: { marginTop: "12px" } }, h("div.grow"), h("button.btn.primary", { onclick: save }, "Save"))),
      h("div.card", h("h3", "Colours"),
        h("div.help", "GTA V's LS Customs colours. Untick the ones you do not want; add your own below."),
        h("div.swatches", swatches.map(([, , el]) => el)),
        customRows,
        h("div.row", { style: { marginTop: "8px" } }, h("button.btn.small", { onclick: () => addCustom() }, "Add a colour"), h("div.grow"),
          h("button.btn.primary.small", { onclick: save }, "Save"))),
      h("div.card", h("h3", "In game"),
        h("div.kv", h("code", "/neon"), h("span", "list the colours"),
          h("code", "/neon hot pink"), h("span", "switch (driver only; everyone sees it)"),
          h("code", "/neon next"), h("span", "cycle"), h("code", "/neon off"), h("span", "lights off")),
        h("div.help", { style: { marginTop: "8px" } }, "Needs the car hub 1.1 or newer on the server (installed with the car); ",
          "1.1.3 or newer switches the car's lights on with the neon.")))));
}

// ------------------------------------------------------------------ build helper
export async function startBuild(ctx) {
  try {
    const job = await api.post(`/projects/${ctx.pid}/build`);
    toast("Build started");
    location.hash = `#/project/${ctx.pid}/build`;
    return job;
  } catch (e) {
    toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
  }
}

function fact(label, value) { return h("div.fact", h("span", label), h("b", value ?? "-")); }

// ------------------------------------------------------------------ overview
function overview(root, ctx) {
  const d = ctx.inspect;
  const p = ctx.project;
  let mode = "material";
  const img = h("img.sheet", { src: `/api/projects/${ctx.pid}/sheet.png?mode=${mode}&v=${ctx.version}`, alt: "renders" });
  const modeBtn = (m, label) => h("button.btn.small" + (m === mode ? ".cyan" : ""), { onclick: (e) => {
    mode = m;
    img.src = `/api/projects/${ctx.pid}/sheet.png?mode=${m}&v=${ctx.version}`;
    for (const b of e.target.parentNode.querySelectorAll("button")) b.classList.remove("cyan");
    e.target.classList.add("cyan");
  } }, label);
  const lb = (p.data.builds || []).slice(-1)[0];
  const deps = (p.data.deployments || []);
  const steps = [
    ["Read the FiveM car", true],
    ["Built", lb && lb.ok],
    ["Checks passed", lb && lb.qa && lb.qa.ok],
    ["Installed on a server", deps.length > 0],
  ];
  const name = h("input", { type: "text", value: ctx.profile.display_name || d.display_name, style: { flex: 1 } });
  const prim = ctx.profile.paint && ctx.profile.paint.primary && ctx.profile.paint.primary !== "auto" ? ctx.profile.paint.primary : d.paint.primary;
  const color = h("input", { type: "color", value: prim });
  const colorAuto = h("input", { type: "checkbox", checked: !(ctx.profile.paint && ctx.profile.paint.primary && ctx.profile.paint.primary !== "auto") });
  root.append(
    h("div.split",
      h("div", h("div.card", h("div.row", { style: { marginBottom: "10px" } }, h("h3", { style: { margin: 0 } }, "How it will look"), h("div.grow"),
        modeBtn("material", "Colours"), modeBtn("parts", "Parts")), img,
        h("div.help", { style: { marginTop: "8px" } }, "Parts view: doors blue, hood orange, trunk red, windows light blue, wheels silver, lights yellow."))),
      h("div",
        h("div.card", h("h3", "The car"),
          h("div.facts",
            fact("Model", d.model), fact("Class", d.class.replace("VC_", "")), fact("Doors", d.doors),
            fact("Length", `${d.dimensions.length.toFixed(2)} m`), fact("Width", `${d.dimensions.width.toFixed(2)} m`),
            fact("Donor", d.donor.label.split(" (")[0]), fact("Engine sound", d.sound.label),
            fact("GTA sound", d.gta_sound || "-"), fact("Parts", Object.keys(d.parts).length),
            fact("Textures", d.textures.used), fact("Lights", d.lights.length), fact("Top speed", `${Math.round(d.handling.cyberpunk.top_speed * 3.6)} km/h`),
            fact("Seating", d.seating ? d.seating.template : "-"))),
        d.seating ? seatsCard(ctx) : null,
        h("div.card", h("h3", "Name and paint"),
          h("label.field", "Name players see in game", h("div.row", name, h("button.btn", { onclick: async () => {
            await ctx.patch({ display_name: name.value.trim() || null });
            await ctx.reload("all");
          } }, "Save"))),
          h("div.row", { style: { marginTop: "12px" } }, h("span.muted", "Paint colour"), color,
            h("label.row", colorAuto, h("span", "from the FiveM car (" + d.paint.primary + ")")),
            h("div.grow"), h("button.btn", { onclick: async () => {
              await ctx.patch({ paint: { primary: colorAuto.checked ? "auto" : color.value } });
            } }, "Save"))),
        h("div.card", h("h3", "Progress"),
          steps.map(([label, ok]) => h("div.row", h("span", { class: ok ? "ok" : "faint" }, ok ? "✔" : "○"), label)),
          h("div.row", { style: { marginTop: "12px" } },
            h("button.btn.primary", { onclick: () => startBuild(ctx) }, lb ? "Build again" : "Build"),
            lb && lb.ok ? h("a.btn", { href: `#/project/${ctx.pid}/deploy` }, "Deploy...") : null,
            h("a.btn.cyan", { href: `#/project/${ctx.pid}/agent` }, "Let the AI do it"))),
        d.warnings.length || (d.resource_warnings || []).length ? h("div.card", h("h3", h("span.warn", "⚠"), "Warnings"),
          [...(d.resource_warnings || []), ...d.warnings].map((w) => h("div.help", { style: { padding: "3px 0" } }, w))) : null)));
}

// ------------------------------------------------------------------ seats
const SEAT_LABELS = { seat_front_left: "driver", seat_front_right: "front passenger", seat_back_left: "rear left", seat_back_right: "rear right" };
const TEMPLATE_LABELS = { sport1: "sport1 (hypercar: low, reclined)", sport2: "sport2 (sports car)", standard2: "standard2 (ordinary car)",
  standard25: "standard25 (pickup / van)", standard3: "standard3 (SUV / truck)" };

function seatsCard(ctx) {
  const st = ctx.inspect.seating;
  const conf = Object.assign({ template: "auto", lower: 0 }, ctx.profile.seats || {});
  const sel = h("select", h("option", { value: "auto" }, `automatic (now ${st.template})`),
    Object.entries(TEMPLATE_LABELS).map(([k, l]) => h("option", { value: k }, l)));
  sel.value = conf.template;
  const lower = h("input", { type: "range", min: 0, max: 20, step: 1, value: Math.round((conf.lower || 0) * 100) });
  const lowerO = h("output", `${lower.value} cm`);
  lower.oninput = () => { lowerO.textContent = `${lower.value} cm`; };
  const rows = Object.entries(st.seats).map(([k, s]) => h("tr", h("td", SEAT_LABELS[k] || k),
    h("td", s.headroom == null ? "open top" : `${Math.round(s.headroom * 100)} cm`),
    h("td", s.lowered ? `${Math.round(s.lowered * 100)} cm lower` : "-"),
    h("td.muted", s.exit_moved ? `exit spot moved ${s.exit_moved}` : "donor exit spot")));
  return h("div.card", h("h3", "Seats"),
    h("div.help", `Posture: ${TEMPLATE_LABELS[st.template] || st.template} — ${st.reason}.`),
    h("table.tbl", h("thead", h("tr", h("th", "seat"), h("th", "head room"), h("th", "seat"), h("th", "getting out"))), h("tbody", rows)),
    h("div.help", st.unmount
      ? `Getting out: the player stands ${st.exit_distance.toFixed(2)} m from the centre line (Cyberpunk cars: 1.65 m), clear of this wide body.`
      : `Getting out: Cyberpunk's usual ${Number(st.exit_distance || 1.65).toFixed(2)} m from the centre line has room beside this body.`),
    st.warnings.length ? st.warnings.map((w) => h("div.help.warn", w)) : null,
    h("div.form", { style: { marginTop: "10px" } }, h("label.field", "Posture", sel), h("label.field", "Lower every seat by", h("div.row", lower, lowerO))),
    h("div.row", { style: { marginTop: "8px" } }, h("div.grow"), h("button.btn.small", { onclick: async () => {
      await ctx.patch({ seats: { template: sel.value, lower: Number(lower.value) / 100 } });
      await ctx.reload("inspect");
      location.reload();
    } }, "Save")));
}

// ------------------------------------------------------------------ parts
const ROLE_LABELS = { auto: "automatic", body: "body", interior: "interior", skip: "hide", "glass:front": "windscreen", "glass:rear": "rear window",
  "lid:bonnet": "bonnet (lid)", "lid:boot": "boot (lid)", steering: "steering wheel",
  "door:fl": "door front left", "door:fr": "door front right", "door:bl": "door rear left", "door:br": "door rear right",
  "window:fl": "window front left", "window:fr": "window front right", "window:bl": "window rear left", "window:br": "window rear right" };

function parts(root, ctx) {
  const d = ctx.inspect;
  const roles = Object.keys(ROLE_LABELS);
  const lidsSel = h("select", ["auto", "semantic", "swap"].map((m) => h("option", { value: m }, m)));
  lidsSel.value = d.lids.mode;
  const plan = d.lids.plan && Object.keys(d.lids.plan).length
    ? Object.entries(d.lids.plan).map(([a, b]) => `${a} → ${b}`).join(", ") : "no change";
  const onlyGeo = h("input", { type: "checkbox", checked: true });
  const filter = h("input", { type: "search", placeholder: "filter bones", style: { width: "180px" } });
  const tbody = h("tbody");
  const busy = h("span.muted");

  function rows() {
    clear(tbody);
    const f = filter.value.trim().toLowerCase();
    for (const b of d.bones) {
      if (onlyGeo.checked && !b.verts && !b.override) continue;
      if (f && !b.name.includes(f) && !(b.part || "").includes(f)) continue;
      const sel = h("select", roles.map((r) => h("option", { value: r }, ROLE_LABELS[r])));
      sel.value = b.override || "auto";
      sel.addEventListener("change", async () => {
        busy.textContent = "updating...";
        try {
          await ctx.patch({ parts: { bone_roles: { [b.name]: sel.value === "auto" ? null : sel.value } } }, { quiet: true });
          await ctx.reload("inspect");
          Object.assign(d, ctx.inspect);
          ctx.version = Date.now();
          rows();
          toast(`${b.name}: ${ROLE_LABELS[sel.value]}`, "ok");
        } finally { busy.textContent = ""; }
      });
      tbody.append(h("tr", h("td", h("code", b.name)), h("td.faint", b.parent || ""), h("td.muted", b.auto),
        h("td", sel), h("td", b.part ? h("code", b.part) : h("span.faint", "-")), h("td.num", b.verts || "")));
    }
  }
  filter.addEventListener("input", debounce(rows, 150));
  onlyGeo.addEventListener("change", rows);

  const extras = h("div.row");
  for (const x of d.extras || []) {
    const cb = h("input", { type: "checkbox", checked: !!((ctx.profile.parts || {}).extras || {})[x] });
    cb.addEventListener("change", async () => {
      await ctx.patch({ parts: { extras: { [x]: cb.checked ? true : null } } });
      await ctx.reload("inspect");
      Object.assign(d, ctx.inspect);
      rows();
    });
    extras.append(h("label.row", cb, h("code", x)));
  }
  root.append(
    h("div.grid2",
      h("div.card", h("h3", "Hood and trunk"),
        h("p.help", "The donor (Porsche 911) has its luggage lid in front (trunk_a) and engine lid at the rear (hood_a). " +
          "auto puts each GTA lid on the donor lid at the same end, so the right one opens."),
        h("div.row", h("span.muted", "Mode"), lidsSel, h("span.muted", "plan: " + plan), h("div.grow"),
          h("button.btn", { onclick: async () => { await ctx.patch({ parts: { lids: lidsSel.value } }); location.reload(); } }, "Save"))),
      h("div.card", h("h3", "GTA extras"),
        h("p.help", "Optional parts of the GTA model (spoilers, roof, bars). Off by default; tick the ones the car should have."),
        (d.extras || []).length ? extras : h("div.faint", "this car has no extras"))),
    h("div.card", h("div.row", { style: { marginBottom: "10px" } }, h("h3", { style: { margin: 0 } }, "GTA bones → Cyberpunk parts"),
      busy, h("div.grow"), filter, h("label.row", onlyGeo, h("span.muted", "only bones with geometry"))),
      h("p.help", "Every piece of the model moves with the bone it hangs from. Change a role when a part opens, moves or hides wrongly " +
        "(a handle that should open with its door: door; a broken piece: hide)."),
      h("div.scroll", h("table.tbl", h("thead", h("tr", h("th", "GTA bone"), h("th", "parent"), h("th", "automatic"), h("th", "role"),
        h("th", "Cyberpunk part"), h("th.num", "vertices"))), tbody))));
  rows();
}

// ------------------------------------------------------------------ lights
function lights(root, ctx) {
  const d = ctx.inspect;
  const conf = ctx.profile.lights || {};
  const types = ["Head", "Brake", "LeftBlinker", "RightBlinker", "Reverse", "Interior", "Utility"];
  const tbody = h("tbody");
  const toHex = (c) => "#" + c.map((x) => Math.round(x).toString(16).padStart(2, "0")).join("");
  const all = [...d.lights];
  for (const name of Object.keys(conf)) if (!all.find((l) => l.name === name)) all.push({ name, type: conf[name].type || "Head", part: "-", color: [255, 255, 255], skipped: true });
  for (const l of all) {
    const c = conf[l.name] || {};
    const type = h("select", types.map((t) => h("option", { value: t }, t)));
    type.value = c.type || l.type;
    const color = h("input", { type: "color", value: c.color || toHex(l.color) });
    const skip = h("input", { type: "checkbox", checked: !!c.skip });
    const save = async () => {
      const patch = { type: type.value, color: color.value, skip: skip.checked };
      await ctx.patch({ lights: { [l.name]: patch } }, { quiet: true });
      toast(`${l.name} saved`, "ok");
    };
    for (const el of [type, color, skip]) el.addEventListener("change", save);
    tbody.append(h("tr", h("td", h("code", l.name)), h("td", h("code", l.part)), h("td", type), h("td", color),
      h("td", h("label.row", skip, h("span.muted", "remove"))),
      h("td", conf[l.name] ? h("button.btn.ghost.small", { onclick: async () => { await ctx.patch({ lights: { [l.name]: null } }); location.reload(); } }, "reset") : "")));
  }
  root.append(h("div.card", h("h3", "Lights"),
    h("p.help", "Light meshes are grouped per corner; each entry drives one material zone (head, brake, blinker...) and, for head and brake lights, casts real light."),
    d.lights.length ? h("div.scroll", h("table.tbl", h("thead", h("tr", h("th", "light"), h("th", "mesh"), h("th", "type"), h("th", "colour"), h("th", ""), h("th", ""))), tbody))
      : h("div.faint", "no emissive lights found on this model")));
}

// ------------------------------------------------------------------ handling
function handling(root, ctx) {
  const d = ctx.inspect;
  const ho = Object.assign({ drive: "auto", gears: null, mass: null, steer: null, multipliers: {} }, ctx.profile.handling || {});
  const mult = Object.assign({ torque: 1, top_speed: 1, brake: 1, grip: 1, spring: 1, drag: 1 }, ho.multipliers || {});
  const base = d.handling.cyberpunk;
  const out = h("tbody");
  const drive = h("select", ["auto", "rwd", "fwd", "awd"].map((m) => h("option", { value: m }, m)));
  drive.value = ho.drive || "auto";
  const gears = h("select", ["auto", 4, 5, 6, 7].map((m) => h("option", { value: m }, m)));
  gears.value = ho.gears || "auto";
  const mass = h("input", { type: "number", min: 500, max: 4000, step: 10, placeholder: `${Math.round(base.mass)} (auto)`, value: ho.mass || "" });
  const steer = h("input", { type: "number", min: 25, max: 70, step: 1, placeholder: `${base.steer.toFixed(0)} (auto)`, value: ho.steer || "" });
  const sliders = {};
  const labels = { torque: "Acceleration (torque)", top_speed: "Top speed", brake: "Brakes", grip: "Grip", spring: "Suspension stiffness", drag: "Air drag" };
  const sliderBox = h("div");
  for (const k of Object.keys(labels)) {
    const r = h("input", { type: "range", min: Math.log(0.25), max: Math.log(4), step: 0.01, value: Math.log(mult[k]) });
    const o = h("output", mult[k].toFixed(2) + "×");
    r.addEventListener("input", () => { o.textContent = Math.exp(r.value).toFixed(2) + "×"; preview(); });
    sliders[k] = r;
    sliderBox.append(h("div.slider", h("span", labels[k]), r, o));
  }
  const current = () => {
    const m = {};
    for (const [k, r] of Object.entries(sliders)) m[k] = Math.round(Math.exp(r.value) * 100) / 100;
    return { drive: drive.value, gears: gears.value === "auto" ? null : Number(gears.value), mass: mass.value ? Number(mass.value) : null,
      steer: steer.value ? Number(steer.value) : null, multipliers: m };
  };
  const rowsSpec = [["mass", "Mass", (v) => `${Math.round(v)} kg`], ["layout", "Drive", (v) => v.toUpperCase()],
    ["torque", "Engine torque", (v) => `${Math.round(v)} Nm`], ["top_speed", "Top speed", (v) => `${Math.round(v * 3.6)} km/h`],
    ["gears", "Gears", (v) => v], ["brake", "Brake factor", (v) => v.toFixed(2)], ["grip", "Grip factor", (v) => v.toFixed(2)],
    ["spring", "Spring factor", (v) => v.toFixed(2)], ["drag", "Drag factor", (v) => v.toFixed(2)], ["steer", "Steering angle", (v) => `${v.toFixed(0)}°`]];
  function show(hp) {
    clear(out);
    for (const [k, label, f] of rowsSpec) {
      const changed = JSON.stringify(hp[k]) !== JSON.stringify(base[k]);
      out.append(h("tr", h("td", label), h("td.num.muted", f(base[k])), h("td.num" + (changed ? ".changed" : ""), f(hp[k]))));
    }
  }
  const preview = debounce(async () => {
    try { show(await api.post(`/projects/${ctx.pid}/handling/preview`, current())); } catch (e) { toast(e.message, "err"); }
  }, 250);
  for (const el of [drive, gears, mass, steer]) el.addEventListener("change", preview);
  const gta = d.handling.gta || {};
  root.append(h("div.split",
    h("div.card", h("h3", "Tuning"),
      h("p.help", "The FiveM handling.meta is mapped onto the donor's drive model and kept in a sane range. These adjust the result."),
      h("div.form", h("label.field", "Drive", drive), h("label.field", "Gears", gears), h("label.field", "Mass (kg)", mass),
        h("label.field", "Max steering angle", steer)),
      h("div", { style: { marginTop: "14px" } }, sliderBox),
      h("div.row", { style: { marginTop: "14px" } },
        h("button.btn", { onclick: () => {
          drive.value = "auto"; gears.value = "auto"; mass.value = ""; steer.value = "";
          for (const r of Object.values(sliders)) { r.value = 0; r.dispatchEvent(new Event("input")); }
          preview();
        } }, "Reset"), h("div.grow"),
        h("button.btn.primary", { onclick: async () => { await ctx.patch({ handling: current() }); } }, "Save"))),
    h("div",
      h("div.card", h("h3", "Cyberpunk drive model"), h("table.tbl.compare", h("thead", h("tr", h("th", ""), h("th.num", "from FiveM"), h("th.num", "with tuning"))), out)),
      h("div.card", h("h3", "FiveM handling.meta"), Object.keys(gta).length
        ? h("div.kv.mono", Object.entries(gta).filter(([k]) => /Mass|Drive|Brake|Traction|Steering|Gears|Suspension|Drag/.test(k)).map(([k, v]) => [h("span", k), h("span", Array.isArray(v) ? v.join(", ") : v)]))
        : h("div.faint", "no handling.meta entry: the donor's handling is used")))));
  show(base);
  if (ctx.profile.handling) preview();
}

// ------------------------------------------------------------------ sound
function sound(root, ctx) {
  const d = ctx.inspect;
  const chime = Object.assign({ enabled: false, file: null, volume: 0.8, range: 12 }, ctx.profile.chime || {});
  const engSel = h("select", { style: { minWidth: "320px" } });
  const auto = h("div.help");
  api.get("/sounds").then((r) => {
    engSel.append(h("option", { value: "auto" }, "automatic (from the GTA sound)"), h("option", { value: "donor" }, "the donor's (Porsche 911 Turbo)"));
    const groups = {};
    for (const s of r.sounds) (groups[s.kind] = groups[s.kind] || []).push(s);
    for (const [kind, items] of Object.entries(groups)) {
      engSel.append(h("optgroup", { label: kind }, items.map((s) => h("option", { value: s.name }, `${s.label}  (${s.name})`))));
    }
    engSel.value = (ctx.profile.sound && ctx.profile.sound.engine) || "auto";
  });
  auto.textContent = `Now: ${d.sound.label} (${d.sound.player}) - ${d.sound.source}.`;
  const audio = h("audio", { controls: true, src: chime.file ? `/api/projects/${ctx.pid}/chime?v=${Date.now()}` : "", class: chime.file ? "" : "hidden" });
  const en = h("input", { type: "checkbox", checked: chime.enabled });
  const vol = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: chime.volume });
  const volO = h("output", Number(chime.volume).toFixed(2));
  vol.addEventListener("input", () => { volO.textContent = Number(vol.value).toFixed(2); audio.volume = Number(vol.value); });
  const rng = h("input", { type: "range", min: 2, max: 60, step: 1, value: chime.range });
  const rngO = h("output", `${chime.range} m`);
  rng.addEventListener("input", () => { rngO.textContent = `${rng.value} m`; });
  const file = h("input", { type: "file", accept: ".mp3,.wav,audio/mpeg,audio/wav", class: "hidden" });
  const fileInfo = h("span.muted", chime.file ? chime.file.split(/[\\/]/).pop() : "no clip yet");
  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    try {
      const r = await api.upload(`/projects/${ctx.pid}/chime?name=${encodeURIComponent(f.name)}`, f);
      fileInfo.textContent = `${f.name} (${r.seconds.toFixed(1)} s)`;
      en.checked = true;
      audio.src = `/api/projects/${ctx.pid}/chime?v=${Date.now()}`;
      audio.classList.remove("hidden");
      toast("Chime saved", "ok");
    } catch (e) { toast(e.message, "err"); }
  });
  root.append(h("div.grid2",
    h("div.card", h("h3", "Engine sound"),
      h("p.help", "Cyberpunk gives a car another car's engine, wheel and bump sounds. GTA's sound for this car is " + (d.gta_sound || "not set") + "."),
      h("div.row", engSel, h("button.btn.primary", { onclick: async () => {
        await ctx.patch({ sound: { engine: engSel.value } });
        await ctx.reload("inspect");
        auto.textContent = `Now: ${ctx.inspect.sound.label} (${ctx.inspect.sound.player}) - ${ctx.inspect.sound.source}.`;
      } }, "Save")), auto),
    h("div.card", h("h3", "Door chime"),
      h("p.help", "Plays at the car while a door is open, for every player nearby (server network audio), and stops the moment it shuts."),
      h("label.row", en, h("span", "chime when a door opens")),
      h("div.row", { style: { margin: "10px 0" } }, h("button.btn", { onclick: () => file.click() }, "Choose mp3 / wav"), fileInfo, file),
      audio,
      h("div.slider", h("span", "Volume"), vol, volO), h("div.slider", h("span", "Heard up to"), rng, rngO),
      h("div.row", { style: { marginTop: "10px" } }, h("div.grow"), h("button.btn.primary", { onclick: async () => {
        await ctx.patch({ chime: { enabled: en.checked, volume: Number(vol.value), range: Number(rng.value) } });
      } }, "Save")))));
}

// ------------------------------------------------------------------ build
function buildTab(root, ctx) {
  const live = h("div");
  const hist = h("tbody");
  const detail = h("div");

  async function history() {
    const builds = await api.get(`/projects/${ctx.pid}/builds`);
    clear(hist);
    if (!builds.length) hist.append(h("tr", h("td", { colSpan: 7 }, h("span.faint", "no builds yet"))));
    for (const b of builds) {
      const size = (b.packages || []).reduce((a, p) => a + p.bytes, 0);
      hist.append(h("tr", h("td", `#${b.n}`), h("td", fmtTime(b.time)), h("td", b.ok ? h("span.pill.ok", "ok") : h("span.pill.err", "failed")),
        h("td", b.qa ? (b.qa.ok ? h("span.ok", "passed") : h("span.err", `${b.qa.failed.length} failed`)) : "-"),
        h("td.num", size ? fmtBytes(size) : ""), h("td.num", b.seconds ? `${b.seconds}s` : ""),
        h("td", b.ok ? h("div.row", h("button.btn.small", { onclick: () => showBuild(b.n) }, "details"),
          h("a.btn.small", { href: `/api/projects/${ctx.pid}/builds/${b.n}/download` }, "zip")) : h("span.err", { title: b.error || "" }, (b.error || "").slice(0, 60)))));
    }
  }

  async function showBuild(n) {
    const info = await api.get(`/projects/${ctx.pid}/builds/${n}`);
    const q = info.qa;
    clear(detail).append(h("div.card", h("h3", `Build #${n}`, h("span.muted", info.build.version)),
      h("div.kv", h("span", "record"), h("code", info.build.record), h("span", "resources"), h("span", (info.build.resources || []).join(", ")),
        h("span", "engine sound"), h("span", info.build.sound ? `${info.build.sound.label} (${info.build.sound.player})` : "-"),
        h("span", "packages"), h("span", (info.build.packages || []).map((p) => `${p.file} ${fmtBytes(p.bytes)}`).join(", "))),
      q ? h("div.checks", { style: { marginTop: "12px" } }, q.checks.map((c) => h("div.check" + (c.ok ? "" : "." + c.level), c.message))) : null,
      (info.build.warnings || []).length ? h("div", { style: { marginTop: "10px" } }, h("h4", "warnings"), info.build.warnings.map((w) => h("div.help", w))) : null,
      h("div.row", { style: { marginTop: "12px" } }, h("button.btn.small", { onclick: async () => { await api.post(`/projects/${ctx.pid}/builds/${n}/qa`); showBuild(n); } }, "Run checks again"),
        h("button.btn.small", { onclick: () => api.post(`/projects/${ctx.pid}/open`, { what: "build" }).then((r) => { if (!r.ok) toast(r.path); }) }, "Open folder"))));
  }

  function attach(job) {
    const bar = h("div.progress" + (job.progress == null ? ".indef" : ""), h("div", { style: { width: `${(job.progress || 0) * 100}%` } }));
    const step = h("span.muted", job.step || "");
    const log = h("div.log");
    const cancel = h("button.btn.danger.small", { onclick: () => api.post(`/jobs/${job.id}/cancel`) }, "Cancel");
    clear(live).append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, job.title), pill(job.state), step, h("div.grow"), cancel), bar,
      h("div", { style: { height: "10px" } }), log));
    waitJob(job.id, (j) => {
      bar.className = "progress" + (j.progress == null && j.state === "running" ? " indef" : "");
      bar.firstChild.style.width = `${(j.progress || (j.state === "done" ? 1 : 0)) * 100}%`;
      step.textContent = j.step || "";
      clear(log).append(...logLines(j.allLines));
      log.scrollTop = log.scrollHeight;
    }).then((j) => {
      cancel.remove();
      live.querySelector(".pill").replaceWith(pill(j.state));
      history();
      if (j.state === "done" && j.result) showBuild(j.result.n);
    });
  }

  const running = [...store.jobs.values()].find((j) => j.project === ctx.pid && j.kind === "build" && ["queued", "running"].includes(j.state));
  root.append(h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "Build"), h("div.grow"),
    h("button.btn.primary", { onclick: async () => {
      try { attach(await api.post(`/projects/${ctx.pid}/build`)); } catch (e) { toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err"); }
    } }, "Build now")),
    h("p.help", "WolvenKit writes the game files from the car and the donor; a build takes a few minutes and is checked afterwards.")),
    live, h("div.card", h("h3", "Builds"), h("table.tbl", h("thead", h("tr", h("th", ""), h("th", "when"), h("th", "build"), h("th", "checks"), h("th.num", "size"), h("th.num", "time"), h("th", ""))), hist)), detail);
  history();
  if (running) attach(running);
}

// ------------------------------------------------------------------ deploy
function deployTab(root, ctx) {
  const servers = store.state.settings.servers;
  const p = ctx.project;
  const builds = (p.data.builds || []).filter((b) => b.ok).reverse();
  if (!servers.length) {
    root.append(h("div.card", h("h3", "No servers yet"), h("p.muted", "Add your Open77 server (SSH) in Servers first."), h("a.btn.primary", { href: "#/servers" }, "Add a server")));
    return;
  }
  if (!builds.length) {
    root.append(h("div.card", h("h3", "Nothing built yet"), h("p.muted", "Build the car before installing it."), h("button.btn.primary", { onclick: () => startBuild(ctx) }, "Build")));
    return;
  }
  const srvSel = h("select", servers.map((s) => h("option", { value: s.id }, `${s.label} (${s.host})`)));
  const def = store.state.settings.deploy.default_server;
  if (def) srvSel.value = def;
  const names = h("div.row");
  const buildSel = h("select", builds.map((b) => h("option", { value: b.n }, `#${b.n} - ${fmtTime(b.time)} (${b.version})`)));
  const mode = h("div.col",
    h("label.choice", h("input", { type: "radio", name: "mode", value: "stage", checked: true }), h("span", h("b", "Install, restart when nobody is online"), h("span.help", " - the car hub restarts the server when it is empty (or at its daily time); players get the car on their next launch"))),
    h("label.choice", h("input", { type: "radio", name: "mode", value: "restart" }), h("span", h("b", "Install and restart now"), h("span.help", " - only when nobody is online"))),
    h("label.choice", h("input", { type: "radio", name: "mode", value: "files" }), h("span", h("b", "Install files only"), h("span.help", " - you restart the server yourself"))));
  const hub = h("input", { type: "checkbox" });
  const out = h("div");
  const statusBox = h("div");

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
    return j;
  }

  async function status() {
    const job = await api.post(`/servers/${srvSel.value}/status`, {});
    const j = await waitJob(job.id);
    clear(statusBox);
    if (j.state !== "done") { statusBox.append(h("div.card.err", j.error || "status failed")); return; }
    for (const s of j.result) {
      const hubSt = s.hub && s.hub.status;
      const car = (s.cars || []).find((c) => c.name === (p.car || {}).name);
      statusBox.append(h("div.card", h("h3", h("code", s.name), h("span.muted", s.version || ""), pill(s.unit === "active" ? "running" : s.unit)),
        h("div.kv", h("span", "players online"), h("span", (s.online || []).length),
          h("span", "this car"), h("span", car ? `${car.version} installed${car.in_load ? "" : " (not in resources.load)"}` : "not installed"),
          h("span", "converted cars"), h("span", (s.cars || []).map((c) => `${c.display || c.name} ${c.version}`).join(", ") || "none"),
          h("span", "car hub"), h("span", s.hub ? `${s.hub.version}${hubSt ? `, ${(hubSt.waiting || []).length} waiting` : " (not running yet)"}` +
            (hubSt && hubSt.restart && hubSt.restart.scheduled ? `, restart ${hubSt.restart.at ? "at " + new Date(hubSt.restart.at * 1000).toLocaleTimeString() : `in ${hubSt.restart.secondsRemaining}s`}` : "") : "not installed")),
        h("div.row", { style: { marginTop: "10px" } },
          !s.hub ? h("button.btn.small", { onclick: async () => { await runJob(await api.post(`/servers/${srvSel.value}/hub`, { names: [s.name], mode: "stage" }), out); status(); } }, "Install the car hub") : null,
          h("button.btn.small.danger", { onclick: async () => {
            if (!(await confirmBox("Restart now", `Restart ${s.name} now? It is refused while players are online.`, "Restart", "danger"))) return;
            await runJob(await api.post(`/servers/${srvSel.value}/restart`, { names: [s.name] }), out); status();
          } }, "Restart now"),
          h("button.btn.small.ghost", { onclick: async () => {
            const r = await api.get(`/servers/${srvSel.value}/logs?name=${encodeURIComponent(s.name)}&lines=300`);
            modal(`${s.name} log`, h("div.log", { style: { maxHeight: "60vh" } }, logLines(r.text.split("\n"))), [{ label: "Close" }]);
          } }, "Server log"))));
    }
  }

  root.append(h("div.split",
    h("div.card", h("h3", "Install on a server"),
      h("div.form", h("label.field", "Server", srvSel), h("label.field", "Build", buildSel)),
      h("div", { style: { margin: "10px 0" } }, h("div.muted", "Server folders"), names),
      mode,
      h("label.row", { style: { marginTop: "10px" } }, hub, h("span", "also install / update the car hub (f2o77_hub)")),
      h("div.row", { style: { marginTop: "14px" } }, h("button.btn", { onclick: status }, "Check server"), h("div.grow"),
        h("button.btn.primary", { onclick: async () => {
          const chosen = [...names.querySelectorAll("input:checked")].map((i) => i.value);
          if (!chosen.length) return toast("choose at least one server folder", "err");
          const m = root.querySelector("input[name=mode]:checked").value;
          try {
            const job = await api.post(`/projects/${ctx.pid}/deploy`, { server: srvSel.value, names: chosen, mode: m, build: Number(buildSel.value), include_hub: hub.checked });
            const j = await runJob(job, out);
            if (j.state === "done") status();
          } catch (e) { toast(e.message, "err"); }
        } }, "Install"))),
    h("div", statusBox)), out);
}
