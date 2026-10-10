// Maps: every map project, and importing a FiveM map / MLO.
import { api, uploadFile, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, fmtAgo, fmtBytes, pill, logLines, icon } from "../ui.js";

export function mapsPage(root) {
  const list = h("div");
  const status = h("div");
  const fileInput = h("input", { type: "file", accept: ".zip,.rar,.7z", class: "hidden", onchange: () => {
    if (fileInput.files[0]) importFile(fileInput.files[0]);
    fileInput.value = "";
  } });
  const pathInput = h("input", { type: "text", placeholder: "or a folder / archive on this PC, e.g. C:\\Users\\me\\Downloads\\MyMLO.rar", style: { flex: 1, minWidth: "260px" } });
  const drop = h("div.dropzone",
    h("div.big", icon("upload")),
    h("div", h("b", "Drop a FiveM map or MLO here"), " (.zip, .rar, .7z) or ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); fileInput.click(); } }, "choose a file")),
    h("div.help", { style: { marginTop: "6px" } }, "Resources with .ymap / .ytyp / .ydr / .ybn files. Escrow-protected (.fxap) resources cannot be converted."),
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
      await create({ upload_id: up.upload_id, kind: "map" });
    } catch (e) {
      clear(status).append(h("div.card.err", e.message));
    }
  }

  async function importPath(path) {
    clear(status).append(h("div.card", "Copying ", h("code", path), "..."));
    try { await create({ path, kind: "map" }); } catch (e) { clear(status).append(h("div.card.err", e.message)); }
  }

  async function create(body) {
    const r = await api.post("/projects", body);
    const pid = r.project.id;
    const logBox = h("div.log", { style: { maxHeight: "160px" } });
    const bar = h("div.progress.indef", h("div"));
    const step = h("span.muted", "reading the map");
    clear(status).append(h("div.card", h("div.row", h("b", "Looking at the map..."), step, h("div.grow"), pill("running")),
      h("p.help", "The converter reads every map file, builds the parts and draws previews (about a minute for a big MLO)."), bar,
      h("div", { style: { height: "8px" } }), logBox));
    const j = await waitJob(r.job.id, (jj) => {
      clear(logBox).append(...logLines(jj.allLines));
      logBox.scrollTop = logBox.scrollHeight;
      if (jj.step) step.textContent = jj.step;
      if (jj.progress != null) { bar.className = "progress"; bar.firstChild.style.width = `${jj.progress * 100}%`; }
    });
    if (j.state !== "done") {
      clear(status).append(h("div.card", h("b.err", "This map could not be read: "), j.error || j.state,
        h("div.row", { style: { marginTop: "10px" } }, h("button.btn", { onclick: () => { location.hash = `#/map/${pid}`; } }, "Open anyway"))));
      return;
    }
    toast("Map imported", "ok");
    location.hash = `#/map/${pid}/overview`;
  }

  async function load() {
    const st = await refreshState();
    clear(list);
    const maps = st.projects.filter((p) => p.kind === "map");
    count.textContent = maps.length ? `${maps.length} map${maps.length === 1 ? "" : "s"}` : "";
    if (!maps.length) {
      list.append(h("div.empty", "No maps yet. Drop a FiveM map or MLO resource above to start."));
      return;
    }
    const body = h("tbody");
    for (const p of maps) {
      const lb = p.last_build;
      const m = p.map || {};
      const thumb = h("div.thumb", p.thumb ? "" : "—");
      if (p.thumb) thumb.style.backgroundImage = `url(/api/projects/${p.id}/thumb.png?t=${Math.floor((p.updated || 0))})`;
      const dep = p.deployments && p.deployments.length ? p.deployments[p.deployments.length - 1] : null;
      body.append(h("tr.link", { onclick: () => { location.hash = `#/map/${p.id}/overview`; } },
        h("td.thumbcell", thumb),
        h("td", h("div.title", p.name || "(unnamed)"), h("div.sub", m.name ? h("code", m.name) : (p.source || ""))),
        h("td.num", m.size ? `${Math.round(m.size[0])} × ${Math.round(m.size[1])} m` : "—"),
        h("td", lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built")),
        h("td.muted", dep ? `${dep.server} · ${fmtAgo(dep.time)}` : "—"),
        h("td.muted.num", fmtAgo(p.updated))));
    }
    list.append(h("div.card.table-card", h("table.tbl.list",
      h("thead", h("tr", h("th", ""), h("th", "Map"), h("th.num", "Size"), h("th", "Build"), h("th", "Installed on"), h("th.num", "Updated"))),
      body)));
  }

  const setup = h("div");
  function setupBanner() {
    clear(setup);
    const t = store.state && store.state.tools;
    if (t && !(t.wolvenkit.ok && t.dotnet.ok)) {
      setup.append(h("div.banner", h("b.warn", "Setup needed"),
        h("span", "Building maps needs WolvenKit (Settings > Tools). Looking at a map works without it."),
        h("div.grow"), h("a.btn.small", { href: "#/settings/tools" }, "Open setup")));
    }
  }

  const count = h("span.sub");
  root.append(h("div.page-head", h("h1", "Maps"), count, h("div.grow"),
    h("a.btn", { href: "#/live" }, icon("live"), "Live on servers"),
    h("button.btn.ghost.icon-only", { onclick: load, title: "Refresh" }, icon("refresh"))),
    h("p.muted", { style: { maxWidth: "820px" } }, "FiveM maps and MLOs become baked Cyberpunk sectors with collision, doors that open and glass, plus the in-game map manager (F9) that moves them."),
    setup, drop, status, h("div", { style: { height: "16px" } }), list);
  load().then(setupBanner).catch((e) => toast(e.message, "err"));
  return {};
}
