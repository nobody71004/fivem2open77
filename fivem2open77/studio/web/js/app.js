// Studio shell: the window (title bar, section rail, status bar), routing, live events and the
// command palette (Ctrl+K).
import { h, clear, toast, icon, setCrumbs } from "./ui.js";
import { store, refreshState as loadState } from "./store.js";
import { homePage } from "./pages/home.js";
import { projectsPage } from "./pages/projects.js";
import { projectPage } from "./pages/project.js";
import { serversPage } from "./pages/servers.js";
import { settingsPage } from "./pages/settings.js";
import { jobsPage } from "./pages/jobs.js";
import { mapsPage } from "./pages/maps.js";
import { mapPage } from "./pages/map.js";
import { livePage } from "./pages/live.js";
import { worldPage } from "./pages/world.js";
import { studioAgentPage } from "./pages/agent.js";
import { initFx, fxRoute } from "./fx.js";

let current = null;          // { dispose() }

const SECTIONS = {
  home: "Home", projects: "Cars", project: "Cars", maps: "Maps", map: "Maps", world: "World", live: "Live maps",
  servers: "Servers", agent: "Agent", jobs: "Activity", settings: "Settings",
};
const NAV_OF = { project: "projects", map: "maps" };
const agentsLive = new Map();      // MCP session -> { label, tool } while a call runs

async function refreshState() {
  const st = await loadState();
  updateChrome();
  return st;
}

function activeJobs() {
  return [...store.jobs.values()].filter((j) => j.state === "running" || j.state === "queued");
}

function updateChrome() {
  const running = activeJobs();
  const b = document.getElementById("jobs-badge");
  b.textContent = running.length;
  b.classList.toggle("hidden", !running.length);
  const s = document.getElementById("setup-badge");
  s.classList.toggle("hidden", !store.state || store.state.tools.ready);
  // title bar: what runs
  const tb = document.getElementById("tb-jobs");
  clear(tb);
  if (running.length) {
    tb.className = "busy";
    tb.append(h("span.spin"), h("span", running.length === 1 ? running[0].title : `${running.length} jobs running`));
  } else {
    tb.className = "";
    tb.append(icon("activity", "sm"), h("span", "Idle"));
  }
  // status bar: the newest running job with its progress, the tools, the version
  const sj = clear(document.getElementById("sb-job"));
  const j = running[0];
  if (j) {
    sj.append(h("span", j.title + (j.step ? ` — ${j.step}` : "")));
    if (typeof j.progress === "number" && j.progress > 0) {
      sj.append(h("div.bar", h("div", { style: { width: `${Math.round(j.progress * 100)}%` } })));
    }
  }
  const ag = clear(document.getElementById("sb-agents"));
  const running2 = [...agentsLive.values()];
  if (running2.length) {
    ag.classList.remove("hidden");
    ag.append(h("span.spin"), h("span", `${running2[0].label}: ${running2[0].tool.replace(/_/g, " ")}`));
  } else ag.classList.add("hidden");
  const st = store.state;
  if (st) {
    const tools = clear(document.getElementById("sb-tools"));
    const t = st.tools || {};
    tools.append(h("span.dot" + (t.ready ? ".ok" : ".opt")), h("span", t.ready ? "Tools ready" : "Tools need setup"));
    tools.title = t.ready ? "WolvenKit, .NET and the game are found" : "Open Settings to finish the setup";
    document.getElementById("sb-version").textContent = `Studio ${st.version} · converter ${st.converter}`;
  }
}

function connectEvents() {
  const conn = document.getElementById("conn");
  const es = new EventSource("/api/events");
  es.onopen = () => { conn.textContent = "Connected"; conn.className = "conn item live"; };
  es.onerror = () => { conn.textContent = "Reconnecting"; conn.className = "conn item down"; };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (ev.type === "mcp") {
      if (ev.kind === "call") agentsLive.set(ev.session, { label: ev.label, tool: ev.call.tool });
      if (ev.kind === "result" || ev.kind === "closed") agentsLive.delete(ev.session);
      if (ev.kind === "connected") toast(`${ev.label} connected to the Studio`, "");
      if (ev.kind === "approval") toast(`${ev.label} wants to run ${ev.approval.tool}: Settings > Agents`, "err");
      updateChrome();
    }
    if (ev.type === "job") {
      const before = store.jobs.get(ev.job.id);
      store.jobs.set(ev.job.id, ev.job);
      if (before && before.state !== ev.job.state && ["done", "failed"].includes(ev.job.state) && !ev.job.kind.startsWith("server-status")) {
        toast(`${ev.job.title}: ${ev.job.state === "done" ? "done" : "failed" + (ev.job.error ? " — " + ev.job.error : "")}`,
              ev.job.state === "done" ? "ok" : "err");
      }
      updateChrome();
    }
    store.emit(ev);
  };
}

function route() {
  const hash = location.hash.replace(/^#\/?/, "") || "home";
  const parts = hash.split("/");
  const nav = NAV_OF[parts[0]] || parts[0];
  for (const a of document.querySelectorAll("#rail a[data-nav]")) a.classList.toggle("active", a.dataset.nav === nav);
  const main = clear(document.getElementById("main"));
  main.className = "";
  if (current && current.dispose) current.dispose();
  current = null;
  // a page names its own item; until it does the trail shows the section
  setCrumbs([[SECTIONS[parts[0]] || "Cars", `#/${nav}`]]);
  const pages = { home: homePage, projects: projectsPage, project: projectPage, maps: mapsPage, map: mapPage, live: livePage,
    world: worldPage, agent: studioAgentPage, servers: serversPage, settings: settingsPage, jobs: jobsPage };
  const page = pages[parts[0]] || homePage;
  try {
    current = page(main, parts.slice(1)) || null;
  } catch (e) {
    console.error(e);
    main.append(h("div.card.err", String(e)));
  }
  fxRoute(main);
}

// ------------------------------------------------------------------ command palette
function paletteItems() {
  const items = Object.entries({ home: "Home", projects: "Cars", maps: "Maps", world: "World edits", live: "Live maps", servers: "Servers",
    agent: "Agent", jobs: "Activity", settings: "Settings" }).map(([k, v]) => ({ what: v, kind: "section", href: `#/${k}` }));
  const st = store.state;
  if (st) {
    for (const p of st.projects || []) {
      const href = p.kind === "map" ? `#/map/${p.id}/overview` : p.kind === "world" ? `#/world/${p.id}` : `#/project/${p.id}/overview`;
      items.push({ what: p.name || p.id, kind: p.kind === "world" ? "world edit" : p.kind, href });
    }
    for (const s of (st.settings && st.settings.servers) || []) items.push({ what: s.label || s.host, kind: "server", href: "#/servers" });
  }
  items.push({ what: "Import a FiveM car", kind: "action", href: "#/projects" });
  items.push({ what: "Import a FiveM map or MLO", kind: "action", href: "#/maps" });
  items.push({ what: "Edit the world around a spot", kind: "action", href: "#/world" });
  items.push({ what: "Connect an AI agent (MCP)", kind: "action", href: "#/settings/agents" });
  return items;
}

function openPalette() {
  if (document.querySelector(".palette")) return;
  const input = h("input", { type: "text", placeholder: "Go to a car, map, server or section…", "aria-label": "Search" });
  const results = h("div.results");
  let rows = [], on = 0;
  const back = h("div.modal-back", { onclick: (e) => { if (e.target === back) close(); } },
    h("div.modal.palette", input, results));
  function close() { back.remove(); }
  function go(r) { close(); if (r) location.hash = r.href; }
  function render() {
    const q = input.value.trim().toLowerCase();
    const all = paletteItems();
    rows = q ? all.filter((r) => r.what.toLowerCase().includes(q) || r.kind.includes(q)) : all;
    on = Math.min(on, Math.max(0, rows.length - 1));
    clear(results);
    if (!rows.length) results.append(h("div.none", "Nothing matches."));
    rows.slice(0, 60).forEach((r, i) => results.append(h("div.res" + (i === on ? ".on" : ""),
      { onclick: () => go(r), onmousemove: () => { if (on !== i) { on = i; render(); } } },
      icon({ section: "chevron-right", map: "map", car: "car", server: "server", action: "command", "world edit": "world" }[r.kind] || "chevron-right"),
      h("span.what", r.what), h("span.kind", r.kind))));
  }
  input.addEventListener("input", () => { on = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { on = Math.min(rows.length - 1, on + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { on = Math.max(0, on - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter") { go(rows[on]); e.preventDefault(); }
    else if (e.key === "Escape") { close(); e.preventDefault(); }
  });
  document.getElementById("modal-root").append(back);
  render();
  input.focus();
}

async function loadIcons() {
  try {
    const r = await fetch("/img/icons.svg");
    const text = await r.text();
    const box = document.createElement("div");
    box.innerHTML = text;
    const svg = box.querySelector("svg");
    if (svg) document.body.prepend(svg);
  } catch (_) { /* the labels still say what each button is */ }
}

async function boot() {
  initFx();
  await loadIcons();
  document.getElementById("cmdk").addEventListener("click", openPalette);
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  });
  try {
    await refreshState();
  } catch (e) {
    clear(document.getElementById("main")).append(
      h("div.card", h("h2", "Not signed in"), h("p.muted", "Open the Studio from its window or from the link it printed when it started.")));
    return;
  }
  connectEvents();
  window.addEventListener("hashchange", route);
  route();
  setInterval(() => refreshState().catch(() => {}), 30000);
}

boot();
