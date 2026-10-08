// Cars: every project, and importing a new FiveM car.
import { api, uploadFile, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, fmtAgo, fmtBytes, pill } from "../ui.js";

export function projectsPage(root) {
  const grid = h("div.grid3");
  const status = h("div");
  const fileInput = h("input", { type: "file", accept: ".zip,.rar,.7z", class: "hidden", onchange: () => {
    if (fileInput.files[0]) importFile(fileInput.files[0]);
    fileInput.value = "";
  } });
  const drop = h("div.dropzone",
    h("div", { style: { fontSize: "30px", marginBottom: "6px" } }, "⇪"),
    h("div", h("b", "Drop a FiveM car here"), " (.zip, .rar, .7z) or ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); fileInput.click(); } }, "choose a file")),
    h("div.help", { style: { marginTop: "8px" } }, "Escrow-protected (.fxap) resources cannot be converted."),
    fileInput);
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) importFile(f);
  });

  const pathInput = h("input", { type: "text", placeholder: "or a folder / archive path on this PC, e.g. C:\\Users\\me\\Downloads\\mycar", style: { flex: 1 } });
  const pathRow = h("div.row", { style: { marginTop: "10px" } }, pathInput,
    h("button.btn", { onclick: () => pathInput.value.trim() && importPath(pathInput.value.trim()) }, "Import path"));

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
    if (!st.projects.length) {
      grid.append(h("div.empty", { style: { gridColumn: "1/-1" } }, "No cars yet. Drop a FiveM vehicle resource above to start."));
    }
    for (const p of st.projects) {
      const lb = p.last_build;
      const thumb = h("div.thumb", p.thumb ? "" : "no preview yet");
      if (p.thumb) thumb.style.backgroundImage = `url(/api/projects/${p.id}/thumb.png?t=${Math.floor((p.updated || 0))})`;
      grid.append(h("div.car-card", { onclick: () => { location.hash = `#/project/${p.id}/overview`; } }, thumb,
        h("div.info", h("b", p.name || "(unnamed)"),
          h("div.row.muted", { style: { fontSize: "12.5px", marginTop: "3px" } },
            p.car ? h("code", p.car.record) : h("span", p.source || ""),
            h("div.grow"),
            lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built")),
          h("div.faint", { style: { fontSize: "12px", marginTop: "4px" } },
            p.deployments && p.deployments.length ? `deployed to ${p.deployments[p.deployments.length - 1].server} ${fmtAgo(p.deployments[p.deployments.length - 1].time)}` : `updated ${fmtAgo(p.updated)}`))));
    }
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

  root.append(h("div.page-head", h("h1", "Cars"), h("div.grow"),
    h("button.btn.ghost", { onclick: load }, "Refresh")), setup, drop, pathRow, status, h("div", { style: { height: "18px" } }), grid);
  load().then(setupBanner).catch((e) => toast(e.message, "err"));
  return {};
}
