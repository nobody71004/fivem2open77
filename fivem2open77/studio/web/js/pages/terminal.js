// The Terminal page: the Studio's command line full size, with the tools at hand and what agents do.
import { api } from "../api.js";
import { store } from "../store.js";
import { h, clear, fmtAgo, pill } from "../ui.js";
import { mountTerminal, loadSpec } from "../term.js";

export function terminalPage(root, parts) {
  root.classList.add("term-page");
  const consoleBox = h("div", { style: { minHeight: 0, height: "100%" } });
  const filter = h("input", { type: "search", placeholder: "find a tool or command", spellcheck: false });
  const ref = h("div.ref");
  const live = h("div.agents-live");
  const side = h("div.term-side", { id: "term-side" },
    h("div.card", h("h3", "Quick"), h("div.chips",
      ["status", "jobs", "projects", "servers", "qc_report", "agent", "routes", "help"].map((c) =>
        h("button.btn.small", { type: "button", onclick: () => { closeDrawer(); t.run(c); } }, c)))),
    h("div.card", h("h3", "Agents at work"), live),
    h("div.card", h("h3", "Commands & tools"), filter, ref));
  const grid = h("div.term-grid", consoleBox, side);

  // The side panel: beside the console, or (a narrow window) over it while asked for.  Shown or not is
  // remembered for wide windows.
  const narrow = matchMedia("(max-width: 980px)");
  let wanted = true, drawer = false;
  try { wanted = localStorage.getItem("studio-term-side") !== "off"; } catch (_) { /* storage optional */ }
  const sideBtn = h("button.btn.small.ghost.side-toggle", { type: "button", "aria-controls": "term-side", onclick: () => {
    if (narrow.matches) drawer = !drawer;
    else {
      wanted = !wanted;
      try { localStorage.setItem("studio-term-side", wanted ? "on" : "off"); } catch (_) { /* storage optional */ }
    }
    layout();
  } }, "Side panel");
  function layout() {
    const shown = narrow.matches ? drawer : wanted;
    grid.classList.toggle("no-side", !narrow.matches && !wanted);
    grid.classList.toggle("side-open", narrow.matches && drawer);
    sideBtn.setAttribute("aria-expanded", String(shown));
    sideBtn.classList.toggle("on", shown);
  }
  function closeDrawer() {
    if (drawer) { drawer = false; layout(); }
  }
  side.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer) { closeDrawer(); t.focus(); } });
  narrow.addEventListener("change", layout);
  layout();

  root.append(h("div.page-head", h("h1", "Terminal"),
    h("span.sub", "every Studio tool is a command · what agents run shows here · Ctrl+` opens it anywhere"),
    h("div.grow"), sideBtn,
    h("button.btn.small", { onclick: () => t.run("help") }, "Help")), grid);

  const t = mountTerminal(consoleBox, { initial: parts && parts[0] === "run" && parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : null });
  setTimeout(() => t.focus(), 50);

  let spec = null;
  function renderRef() {
    clear(ref);
    if (!spec) return;
    const q = filter.value.trim().toLowerCase();
    const items = (spec.builtins || []).map((b) => ({ name: b.name, about: b.help, group: "Terminal", risk: null, insert: b.name + " " }))
      .concat((spec.tools || []).map((x) => ({ name: x.name, about: x.summary, group: x.group, risk: x.risk,
        insert: x.name + " " + x.params.filter((p) => p.required).map((p) => `${p.name}=`).join(" ") })));
    const groups = new Map();
    for (const it of items) {
      if (q && !it.name.toLowerCase().includes(q) && !(it.about || "").toLowerCase().includes(q) && !it.group.toLowerCase().includes(q)) continue;
      if (!groups.has(it.group)) groups.set(it.group, []);
      groups.get(it.group).push(it);
    }
    const order = ["Terminal", ...(spec.groups || [])];
    for (const g of [...groups.keys()].sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99)) {
      ref.append(h("div.grp", `${g} · ${groups.get(g).length}`));
      for (const it of groups.get(g)) {
        ref.append(h("div.it", { title: it.about || "", onclick: () => { closeDrawer(); t.insert(it.insert.trimEnd() + " "); } },
          h("span", it.name), h("span.d", it.about || ""), it.risk && it.risk !== "read" ? h("span.r." + it.risk, it.risk) : null));
      }
    }
    if (!ref.childNodes.length) ref.append(h("div.faint", "nothing matches"));
  }
  filter.addEventListener("input", renderRef);
  loadSpec(true).then((s) => { spec = s; renderRef(); }).catch(() => {});

  async function renderLive() {
    let mcp = null, agents = [];
    try { mcp = await api.get("/mcp"); } catch (_) { /* MCP off */ }
    try { agents = await api.get("/agent/sessions"); } catch (_) { /* no agent */ }
    clear(live);
    const sessions = (mcp && mcp.sessions) || [];
    for (const s of sessions) {
      const last = s.active[0] || s.calls[s.calls.length - 1];
      live.append(h("div.row", h("b", s.label), s.active.length ? pill("running") : null, h("div.grow"),
        last ? h("code", last.tool) : h("span.faint", "connected"), h("span.faint", ` ${fmtAgo(s.seen)}`)));
    }
    for (const a of agents.filter((x) => ["running", "starting", "waiting_user", "waiting_approval"].includes(x.status)).slice(0, 5)) {
      live.append(h("div.row", h("b", "Studio agent"), pill(a.status), h("div.grow"),
        h("a", { href: "#", title: "follow it in the terminal", onclick: (e) => { e.preventDefault(); t.run(`agent attach ${a.id}`); } },
          (a.goal || a.id).slice(0, 34))));
    }
    if (!live.childNodes.length) live.append(h("div.faint", "No agent is connected. Settings > Agents connects Claude, Codex, Cursor…"));
  }
  renderLive();
  const off = store.on((ev) => {
    if ((ev.type === "mcp" && ["connected", "closed", "call", "result"].includes(ev.kind)) || (ev.type === "agent" && ev.event && ev.event.kind === "status")) {
      clearTimeout(renderLive.t);
      renderLive.t = setTimeout(renderLive, 400);
    }
  });
  return { dispose: () => { off(); narrow.removeEventListener("change", layout); t.dispose(); } };
}
