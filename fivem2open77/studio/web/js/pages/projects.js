// Cars: every project, and importing a new FiveM car.
import { api, uploadFile, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, fmtAgo, fmtBytes, pill, icon } from "../ui.js";

export function projectsPage(root) {
  const grid = h("div");
  const status = h("div");
  const fileInput = h("input", { type: "file", accept: ".zip,.rar,.7z", class: "hidden", onchange: () => {
    if (fileInput.files[0]) importFile(fileInput.files[0]);
    fileInput.value = "";
  } });
  const pathInput = h("input", { type: "text", placeholder: "or a folder / archive on this PC, e.g. C:\\Users\\me\\Downloads\\mycar", style: { flex: 1, minWidth: "260px" } });
  const drop = h("div.dropzone",
    h("div.big", icon("upload")),
    h("div", h("b", "Drop a FiveM car here"), " (.zip, .rar, .7z) or ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); fileInput.click(); } }, "choose a file")),
    h("div.help", { style: { marginTop: "6px" } }, "Escrow-protected (.fxap) resources cannot be converted."),
    h("div.row", { style: { marginTop: "12px", justifyContent: "center" } }, pathInput,
      h("button.btn", { onclick: () => pathInput.value.trim() && importPath(pathInput.value.trim()) }, "Import path")),
    fileInput);
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) importFile(f);
  });

  async function importFile(file) {
    const bar = h("div.progress", h("div", { style: { width: "0%" } }));
    clear(status).append(h("div.card", h("div.row", h("b", `Uploading ${file.name}`), h("span.muted", fmtBytes(file.size))), bar));
    try {
      const up = await uploadFile(`/uploads?name=${encodeURIComponent(file.name)}`, file, (p) => { bar.firstChild.style.width = `${(p * 100).toFixed(0)}%`; });
      await create({ upload_id: up.upload_id });
    } catch (e) {
      clear(status).append(h("div.card.err", e.message));
    }
  }

  async function importPath(path) {
    clear(status).append(h("div.card", "Copying ", h("code", path), "..."));
    try { await create({ path }); } catch (e) { clear(status).append(h("div.card.err", e.message)); }
  }

  async function create(body) {
    const r = await api.post("/projects", body);
    const pid = r.project.id;
    const logBox = h("div.log", { style: { maxHeight: "140px" } });
    clear(status).append(h("div.card", h("div.row", h("b", "Reading the car..."), h("div.grow"), pill("running")), logBox));
    const j = await waitJob(r.job.id, (jj) => { clear(logBox).append(...jj.allLines.map((l) => h("div", l.text))); });
    if (j.state !== "done") {
      clear(status).append(h("div.card", h("b.err", "This resource could not be read: "), j.error || j.state,
        h("div.row", { style: { marginTop: "10px" } }, h("button.btn", { onclick: () => { location.hash = `#/project/${pid}`; } }, "Open anyway"))));
      return;
    }
    toast("Car imported", "ok");
    location.hash = `#/project/${pid}/overview`;
  }

  async function load() {
    const st = await refreshState();
    clear(grid);
    const cars = st.projects.filter((x) => (x.kind || "car") === "car");
    count.textContent = cars.length ? `${cars.length} car${cars.length === 1 ? "" : "s"}` : "";
    if (!cars.length) {
      grid.append(h("div.empty", "No cars yet. Drop a FiveM vehicle resource above to start."));
      return;
    }
    const body = h("tbody");
    for (const p of cars) {
      const lb = p.last_build;
      const thumb = h("div.thumb", p.thumb ? "" : "—");
      if (p.thumb) thumb.style.backgroundImage = `url(/api/projects/${p.id}/thumb.png?t=${Math.floor((p.updated || 0))})`;
      const dep = p.deployments && p.deployments.length ? p.deployments[p.deployments.length - 1] : null;
      body.append(h("tr.link.car-row", { onclick: () => { location.hash = `#/project/${p.id}/overview`; } },
        h("td.thumbcell", thumb),
        h("td", h("div.title", p.name || "(unnamed)"), h("div.sub", p.car ? h("code", p.car.record) : (p.source || ""))),
        h("td.muted", p.car && p.car.class ? p.car.class.replace("VC_", "").toLowerCase() : "—"),
        h("td", lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built")),
        h("td.muted", dep ? `${dep.server} · ${fmtAgo(dep.time)}` : "—"),
        h("td.muted.num", fmtAgo(p.updated))));
    }
    grid.append(h("div.card.table-card", h("table.tbl.list",
      h("thead", h("tr", h("th", ""), h("th", "Car"), h("th", "Class"), h("th", "Build"), h("th", "Installed on"), h("th.num", "Updated"))),
      body)));
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
  load().then(setupBanner).catch((e) => toast(e.message, "err"));
  return {};
}
