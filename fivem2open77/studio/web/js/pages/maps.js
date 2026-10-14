// Maps: every map project, and importing a FiveM map / MLO (or a whole city: Liberty V is converted
// a district at a time).
import { api } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, fmtAgo, fmtBytes, icon } from "../ui.js";
import { importZone, jobCard, follow, sourceBody, desktop } from "../imports.js";

export function mapsPage(root) {
  const list = h("div");
  const status = h("div");
  const drop = importZone({
    what: "a FiveM map or MLO",
    help: "Resources with .ymap / .ytyp / .ydr / .ybn files, up to a whole city (it is converted a district at a time). "
      + "A big folder is read where it is and a big archive is extracted once. Escrow-protected (.fxap) resources cannot be converted.",
    placeholder: "or a folder / archive on this PC, e.g. C:\\Users\\me\\Downloads\\libertyv",
    onSource: start,
  });

  async function start(src) {
    const card = jobCard("Importing", null);
    clear(status).append(card.el);
    let pid = null;
    try {
      const body = await sourceBody(src, card);
      card.title(`Importing ${src.path || src.file.name}`);
      const r = await api.post("/projects", Object.assign(body, { kind: "map" }));
      pid = r.project.id;
      let j = await follow(card, r.job.id);
      if (j.state !== "done") return failed(card, pid, j, "This map could not be imported");
      const res = j.result || {};
      if (res.how === "link") {
        card.extra.append(h("p.help", h("b", "Read where it is: "), `the project reads ${src.path} directly (${fmtBytes(res.bytes)}, `
          + `${(res.files || 0).toLocaleString()} files, nothing copied). Keep the folder there.`));
      } else if (res.how === "extract") {
        card.extra.append(h("p.help", `Extracted once into the Studio (${fmtBytes(res.bytes)} archive).`));
      }
      card.title("Looking at the map");
      j = await follow(card, res.next_job);
      if (j.state !== "done") return failed(card, pid, j, "This map could not be read");
      card.finish("done");
      if (j.result && j.result.city) toast(`${j.result.display_name} is a whole city: pick a district to convert`, "ok");
      else toast("Map imported", "ok");
      location.hash = `#/map/${pid}/overview`;
    } catch (e) {
      card.finish("failed");
      card.extra.append(h("div.err", { style: { marginTop: "8px" } }, e.message));
      if (pid) load();
    }
  }

  function failed(card, pid, j, what) {
    card.finish(j.state);
    card.extra.append(h("div", { style: { marginTop: "8px" } }, h("b.err", `${what}: `), j.error || j.state),
      h("div.row", { style: { marginTop: "10px" } },
        h("button.btn", { onclick: () => { location.hash = `#/map/${pid}`; } }, "Open anyway"),
        h("button.btn.ghost", { onclick: async () => { await api.del(`/projects/${pid}`); clear(status); load(); } }, "Delete it")));
    load();
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
      const thumb = h("div.thumb", p.thumb ? "" : (m.city ? icon("map") : "—"));
      if (p.thumb) thumb.style.backgroundImage = `url(/api/projects/${p.id}/thumb.png?t=${Math.floor((p.updated || 0))})`;
      const dep = p.deployments && p.deployments.length ? p.deployments[p.deployments.length - 1] : null;
      const state = p.import === "importing" ? h("span.pill.run", "importing")
        : p.import === "failed" ? h("span.pill.err", "import failed")
          : m.city ? h("span.pill.warn", "pick a district")
            : lb ? (lb.ok ? h("span.pill.ok", `build ${lb.n}`) : h("span.pill.err", `build ${lb.n} failed`)) : h("span.pill", "not built");
      body.append(h("tr.link", { onclick: () => { location.hash = `#/map/${p.id}/overview`; } },
        h("td.thumbcell", thumb),
        h("td", h("div.title", p.name || "(unnamed)"), h("div.sub", m.city ? `a whole city: ${(m.objects || 0).toLocaleString()} objects, ${m.districts} districts`
          : m.name ? h("code", m.name) : (p.source || ""))),
        h("td.num", m.size ? `${Math.round(m.size[0])} × ${Math.round(m.size[1])} m` : "—"),
        h("td", state),
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
  if (!desktop) {
    drop.append(h("div.help", { style: { marginTop: "8px" } }, "In a browser a folder cannot be dropped: type its path. The Studio's own window takes folders too."));
  }
  load().then(setupBanner).catch((e) => toast(e.message, "err"));
  return {};
}
