// Cars: every project, importing a FiveM car, and car packs (one download, dozens of cars: each car
// becomes a project; the pack is built and installed on a server in one go).
import { api } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, fmtAgo, icon, modal, confirmBox } from "../ui.js";
import { importZone, jobCard, follow, sourceBody, desktop } from "../imports.js";

const CLASS = (c) => (c || "").replace(/^VC_/, "").replace(/_/g, " ").toLowerCase();

export function projectsPage(root) {
  const grid = h("div");
  const status = h("div");
  const drop = importZone({
    what: "a FiveM car or a car pack",
    help: "One car, or a pack of many: you pick its cars and each one becomes a project. Escrow-protected (.fxap) resources cannot be converted.",
    placeholder: "or a folder / archive on this PC, e.g. C:\\Users\\me\\Downloads\\FiveM-Civ-Car-Pack-2.zip",
    onSource: start,
  });
  let packs = [];

  // ---------------------------------------------------------------- import
  async function start(src) {
    const card = jobCard("Importing", null);
    clear(status).append(card.el);
    try {
      const body = await sourceBody(src, card);
      card.title(`Importing ${src.path || src.file.name}`);
      const r = await api.post("/packs", body);
      const j = await follow(card, r.job.id);
      if (j.state !== "done") {
        card.finish(j.state);
        card.extra.append(h("div", { style: { marginTop: "8px" } }, h("b.err", "This resource could not be imported: "), j.error || j.state));
        return;
      }
      const pack = j.result;
      card.finish("done", `${pack.cars.length} car${pack.cars.length === 1 ? "" : "s"}`);
      if (pack.cars.length === 1) return importCars(pack, [pack.cars[0].model]);
      clear(status).append(picker(pack));
    } catch (e) {
      card.finish("failed");
      card.extra.append(h("div.err", { style: { marginTop: "8px" } }, e.message));
    }
  }

  // the cars of a pack, to pick from (every car not imported yet is ticked)
  function picker(pack) {
    const rows = [];
    const filter = h("input", { type: "search", placeholder: `filter ${pack.cars.length} cars...`, style: { minWidth: "220px" } });
    const countEl = h("span.muted");
    const go = h("button.btn.primary", { onclick: () => {
      const models = rows.filter((r) => r.cb.checked && !r.cb.disabled).map((r) => r.car.model);
      if (!models.length) return toast("tick at least one car", "err");
      importCars(pack, models);
    } }, "Import");
    const sync = () => {
      const n = rows.filter((r) => r.cb.checked && !r.cb.disabled).length;
      countEl.textContent = `${n} of ${rows.filter((r) => !r.cb.disabled).length} ticked`;
      go.textContent = n ? `Import ${n} car${n === 1 ? "" : "s"}` : "Import";
      go.disabled = !n;
    };
    const body = h("tbody");
    for (const c of pack.cars) {
      const have = (c.projects || []).length > 0;
      const cb = h("input", { type: "checkbox", checked: !have, disabled: have, onchange: sync });
      const tr = h("tr", h("td", cb), h("td", h("div.title", c.display || c.model), h("div.sub", h("code", c.model))),
        h("td.muted", c.make || "—"), h("td.muted", CLASS(c.class) || "—"),
        h("td.muted", c.sub ? h("code", c.sub.split("/").slice(-2).join("/")) : "the whole pack"),
        h("td", have ? h("a.pill.ok", { href: `#/project/${c.projects[0]}/overview` }, "imported") : (c.warnings ? h("span.pill.warn", `${c.warnings} note(s)`) : "")));
      rows.push({ car: c, cb, tr });
      body.append(tr);
    }
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      for (const r of rows) r.tr.classList.toggle("hidden", !!q && !`${r.car.display} ${r.car.model} ${r.car.make} ${r.car.class}`.toLowerCase().includes(q));
    });
    const setAll = (v) => { for (const r of rows) if (!r.cb.disabled && !r.tr.classList.contains("hidden")) r.cb.checked = v; sync(); };
    sync();
    return h("div.card",
      h("div.row", h("h3", { style: { margin: 0 } }, `${pack.name}: ${pack.cars.length} cars`), h("div.grow"), filter),
      h("p.help", "Each ticked car becomes a project of its own (they all read the pack's files, kept once in the Studio). "
        + "You can add the others later from the pack's row below."),
      h("div.row", { style: { gap: "6px", margin: "6px 0" } }, h("button.btn.small", { onclick: () => setAll(true) }, "Tick all"),
        h("button.btn.small", { onclick: () => setAll(false) }, "Untick all"), countEl, h("div.grow"),
        h("button.btn.ghost", { onclick: () => { clear(status); load(); } }, "Later"), go),
      h("div.table-wrap", { style: { maxHeight: "52vh", overflow: "auto" } },
        h("table.tbl", h("thead", h("tr", h("th", ""), h("th", "Car"), h("th", "Make"), h("th", "Class"), h("th", "Folder"), h("th", ""))), body)));
  }

  async function importCars(pack, models) {
    const card = jobCard(`Reading ${models.length} car${models.length === 1 ? "" : "s"} of ${pack.name}`,
      "Each car's parts, textures and name are read and a thumbnail drawn (a few seconds a car). The cars appear below as they are done.");
    clear(status).append(card.el);
    try {
      const r = await api.post(`/packs/${pack.id}/import`, { models });
      if (!r.job) { card.finish("done", "already imported"); return load(); }
      load();
      const tick = setInterval(load, 8000);
      let j;
      try { j = await follow(card, r.job.id); } finally { clearInterval(tick); }
      card.finish(j.state, j.result ? `${j.result.read} read${j.result.failed ? `, ${j.result.failed} failed` : ""}` : "");
      if (j.state === "done" && r.projects.length === 1) {
        toast("Car imported", "ok");
        location.hash = `#/project/${r.projects[0].id}/overview`;
        return;
      }
      if (j.state === "done") toast(`${j.result.read} car(s) imported`, "ok");
      load();
    } catch (e) {
      card.finish("failed");
      card.extra.append(h("div.err", { style: { marginTop: "8px" } }, e.message));
    }
  }

  // ---------------------------------------------------------------- a pack's actions
  async function runPackJob(title, call) {
    const card = jobCard(title, null);
    clear(status).append(card.el);
    status.scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      const job = await call();
      const j = await follow(card, job.id);
      card.finish(j.state, j.error || "");
      load();
      return j;
    } catch (e) {
      card.finish("failed");
      card.extra.append(h("div.err", { style: { marginTop: "8px" } }, e.message));
      return null;
    }
  }

  async function deployPack(pack, built) {
    const servers = (store.state.settings || {}).servers || [];
    if (!servers.length) return toast("add your Open77 server in Servers first", "err");
    const srv = h("select", servers.map((s) => h("option", { value: s.id }, `${s.label} (${s.host})`)));
    const def = store.state.settings.deploy.default_server;
    if (def) srv.value = def;
    const names = h("div.row.wrap");
    const fill = () => {
      const s = servers.find((x) => x.id === srv.value);
      clear(names).append(...s.names.map((n) => h("label.row", h("input", { type: "checkbox", value: n, checked: true }), h("code", n))));
    };
    srv.addEventListener("change", fill);
    fill();
    const mode = h("select", [["stage", "install, restart when nobody is online"], ["restart", "install and restart now (only when nobody is online)"],
      ["files", "install files only (I restart it myself)"]].map(([v, l]) => h("option", { value: v }, l)));
    mode.value = store.state.settings.deploy.mode || "stage";
    const hub = h("input", { type: "checkbox" });
    const body = h("div",
      h("p.help", `${built} built car${built === 1 ? "" : "s"} of ${pack.name} in one bundle, installed together (one restart). Cars without a build are left out.`),
      h("div.form", h("label.field", "Server", srv), h("label.field", "When", mode)),
      h("div", { style: { margin: "10px 0" } }, h("div.muted", "Server folders"), names),
      h("label.row", hub, h("span", "also install / update the car hub (f2o77_hub)")));
    const ok = await modal(`Install ${pack.name}`, body, [{ label: "Cancel", value: false }, { label: "Install", value: true, kind: "primary" }]);
    if (ok !== true) return;
    const chosen = [...names.querySelectorAll("input:checked")].map((i) => i.value);
    if (!chosen.length) return toast("choose at least one server folder", "err");
    runPackJob(`Install ${pack.name} on ${srv.selectedOptions[0].textContent}`,
      () => api.post(`/packs/${pack.id}/deploy`, { server: srv.value, names: chosen, mode: mode.value, include_hub: hub.checked }));
  }

  // ---------------------------------------------------------------- the list
  function carRow(p) {
    const lb = p.last_build;
    const thumb = h("div.thumb", p.thumb ? "" : "—");
    if (p.thumb) thumb.style.backgroundImage = `url(/api/projects/${p.id}/thumb.png?t=${Math.floor((p.updated || 0))})`;
    const dep = p.deployments && p.deployments.length ? p.deployments[p.deployments.length - 1] : null;
    const state = p.import === "importing" ? h("span.pill.run", "importing")
      : p.import === "failed" ? h("span.pill.err", "import failed")
        : lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built");
    return h("tr.link.car-row", { onclick: () => { location.hash = `#/project/${p.id}/overview`; } },
      h("td.thumbcell", thumb),
      h("td", h("div.title", p.name || "(unnamed)"), h("div.sub", p.car ? h("code", p.car.record) : (p.models && p.models[0] ? h("code", p.models[0]) : (p.source || "")))),
      h("td.muted", p.car && p.car.class ? CLASS(p.car.class) : "—"),
      h("td", state),
      h("td.muted", dep ? `${dep.server} · ${fmtAgo(dep.time)}` : "—"),
      h("td.muted.num", fmtAgo(p.updated)));
  }

  function table(rows) {
    return h("table.tbl.list",
      h("thead", h("tr", h("th", ""), h("th", "Car"), h("th", "Class"), h("th", "Build"), h("th", "Installed on"), h("th.num", "Updated"))),
      h("tbody", rows));
  }

  function packCard(pack, cars) {
    const built = cars.filter((p) => p.last_build && p.last_build.ok).length;
    const unbuilt = cars.filter((p) => !p.last_build).length;
    const more = (pack.cars || []).filter((c) => !(c.projects || []).length);
    const open = h("details", { open: cars.length <= 12 },
      h("summary.help", { style: { cursor: "pointer", margin: "6px 0" } }, `${cars.length} car${cars.length === 1 ? "" : "s"}`),
      cars.length ? table(cars.map(carRow)) : h("div.help", "No car of this pack is imported yet."));
    return h("div.card.table-card.pack-card",
      h("div.row", { style: { padding: "12px 14px 0", gap: "10px", flexWrap: "wrap" } },
        icon("layers"), h("div", h("div.title", pack.name), h("div.sub", `${cars.length} of ${(pack.cars || []).length} cars imported · ${built} built`
          + (pack.linked ? ` · read from ${pack.linked}` : ""))),
        h("div.grow"),
        more.length ? h("button.btn.small", { onclick: () => { clear(status).append(picker(pack)); status.scrollIntoView({ behavior: "smooth" }); } },
          icon("plus"), `Add cars (${more.length} more)`) : null,
        cars.length ? h("button.btn.small", { title: "Build every car of the pack, one after another", onclick: () =>
          runPackJob(`Build ${pack.name}`, () => api.post(`/packs/${pack.id}/build`, {})) }, icon("build"), "Build all") : null,
        unbuilt && unbuilt < cars.length ? h("button.btn.small", { onclick: () =>
          runPackJob(`Build the unbuilt cars of ${pack.name}`, () => api.post(`/packs/${pack.id}/build`, { only_unbuilt: true })) }, `Build the ${unbuilt} unbuilt`) : null,
        built ? h("button.btn.small.primary", { onclick: () => deployPack(pack, built) }, icon("deploy"), "Install all...") : null,
        h("button.btn.small.ghost", { title: "Delete the pack and its cars' projects", onclick: async () => {
          const txt = cars.length ? `Delete ${pack.name} and its ${cars.length} car project(s) from the Studio? Servers and your game are not touched.`
            : `Delete ${pack.name} from the Studio?`;
          if (!(await confirmBox("Delete pack", txt, "Delete", "danger"))) return;
          try { await api.del(`/packs/${pack.id}?projects=1`); load(); } catch (e) { toast(e.message, "err"); }
        } }, icon("trash"))),
      h("div", { style: { padding: "0 6px 6px" } }, open));
  }

  async function load() {
    const [st, pk] = await Promise.all([refreshState(), api.get("/packs").catch(() => [])]);
    packs = pk || [];
    clear(grid);
    const cars = st.projects.filter((x) => (x.kind || "car") === "car");
    count.textContent = cars.length ? `${cars.length} car${cars.length === 1 ? "" : "s"}` : "";
    const byPack = new Map(packs.map((p) => [p.id, []]));
    const loose = [];
    for (const p of cars) {
      const lid = p.pack && p.pack.id;
      if (lid && byPack.has(lid)) byPack.get(lid).push(p);
      else loose.push(p);
    }
    for (const pack of packs) {
      const mine = byPack.get(pack.id);
      if (pack.state !== "ready") continue;
      if ((pack.cars || []).length < 2) {        // one car is not a pack: it goes with the others
        loose.push(...mine);
        continue;
      }
      grid.append(packCard(pack, mine), h("div", { style: { height: "12px" } }));
    }
    loose.sort((a, b) => (b.updated || 0) - (a.updated || 0));
    if (loose.length) grid.append(h("div.card.table-card", table(loose.map(carRow))));
    if (!cars.length && !grid.children.length) grid.append(h("div.empty", "No cars yet. Drop a FiveM vehicle resource (or a whole car pack) above to start."));
  }

  const setup = h("div");
  function setupBanner() {
    clear(setup);
    const t = store.state && store.state.tools;
    if (t && !t.ready) {
      setup.append(h("div.banner", h("b.warn", "Setup needed"),
        h("span", "The converter needs WolvenKit and the donor files from your game before it can build."),
        h("div.grow"), h("a.btn.small", { href: "#/settings/tools" }, "Open setup")));
    }
  }

  const count = h("span.sub");
  root.append(h("div.page-head", h("h1", "Cars"), count, h("div.grow"),
    h("button.btn.ghost.icon-only", { onclick: load, title: "Refresh" }, icon("refresh"))),
    h("p.muted", { style: { maxWidth: "820px" } }, "FiveM vehicles become Cyberpunk cars: meshes, lights, neon, handling and an engine sound, installed on your servers with the car hub."),
    setup, drop, status, h("div", { style: { height: "16px" } }), grid);
  if (!desktop) {
    drop.append(h("div.help", { style: { marginTop: "8px" } }, "In a browser a folder cannot be dropped: type its path. The Studio's own window takes folders too."));
  }
  load().then(setupBanner).catch((e) => toast(e.message, "err"));
  return {};
}

