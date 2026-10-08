// Studio shell: routing, shared state, live events.
import { h, clear, toast } from "./ui.js";
import { store, refreshState as loadState } from "./store.js";
import { projectsPage } from "./pages/projects.js";
import { projectPage } from "./pages/project.js";
import { serversPage } from "./pages/servers.js";
import { settingsPage } from "./pages/settings.js";
import { jobsPage } from "./pages/jobs.js";

let current = null;          // { dispose() }

async function refreshState() {
  const st = await loadState();
  updateBadges();
  return st;
}

function updateBadges() {
  const running = [...store.jobs.values()].filter((j) => j.state === "running" || j.state === "queued").length;
  const b = document.getElementById("jobs-badge");
  b.textContent = running;
  b.classList.toggle("hidden", !running);
  const s = document.getElementById("setup-badge");
  s.classList.toggle("hidden", !store.state || store.state.tools.ready);
}

function connectEvents() {
  const conn = document.getElementById("conn");
  const es = new EventSource("/api/events");
  es.onopen = () => { conn.textContent = "connected"; conn.className = "conn live"; };
  es.onerror = () => { conn.textContent = "reconnecting..."; conn.className = "conn down"; };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (ev.type === "job") {
      const before = store.jobs.get(ev.job.id);
      store.jobs.set(ev.job.id, ev.job);
      if (before && before.state !== ev.job.state && ["done", "failed"].includes(ev.job.state) && !ev.job.kind.startsWith("server-status")) {
        toast(`${ev.job.title}: ${ev.job.state === "done" ? "done" : "failed" + (ev.job.error ? " - " + ev.job.error : "")}`,
              ev.job.state === "done" ? "ok" : "err");
      }
      updateBadges();
    }
    store.emit(ev);
  };
}

function route() {
  const hash = location.hash.replace(/^#\/?/, "") || "projects";
  const parts = hash.split("/");
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.nav === (parts[0] === "project" ? "projects" : parts[0]));
  const main = clear(document.getElementById("main"));
  if (current && current.dispose) current.dispose();
  current = null;
  const pages = { projects: projectsPage, project: projectPage, servers: serversPage, settings: settingsPage, jobs: jobsPage };
  const page = pages[parts[0]] || projectsPage;
  try {
    current = page(main, parts.slice(1)) || null;
  } catch (e) {
    console.error(e);
    main.append(h("div.card.err", String(e)));
  }
}

async function boot() {
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
