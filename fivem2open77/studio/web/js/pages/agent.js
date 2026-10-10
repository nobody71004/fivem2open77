// The AI agent: a model (Claude or Ollama) drives the Studio's tools end to end, asking first
// before anything it is not allowed to do alone.
import { api } from "../api.js";
import { store, onEvent } from "../store.js";
import { h, clear, toast, pill } from "../ui.js";

const PRESETS = {
  car: [
    ["End to end", "Convert this car end to end: check the parts and sound, fix what is wrong, build it and check the output. Tell me what to check in game."],
    ["Check the mapping", "Only check the parts mapping (doors, hood, trunk, wheels, windows) with renders and fix what is wrong. Do not build."],
    ["Build and stage on my server", "Convert this car end to end, then install it on my server with mode stage and confirm it is queued."],
    ["Tune the handling", "Look at the handling numbers and tune them so the car drives like its real-life counterpart. Explain the changes, then build."],
  ],
  map: [
    ["End to end", "Look at this map, check the previews and the converter's notes, fix the options if something is wrong, build it and read the collision coverage and the checks. Tell me what to check in game."],
    ["Build and install on my server", "Build this map and install it on my server with the map baker, check the server's status and make sure the agent API is on so it can be moved. Tell me where it stands."],
    ["Move it", "On my server, move this map to a good spot: show me where it stands now, set the new spot I describe (or suggest one), save it as a preset, and apply it only after I confirm."],
    ["Check the collision", "Read the last build's collision coverage and gaps, explain what they mean for players, and change the options if a rebuild would do better."],
  ],
  studio: [
    ["Status of my servers", "Check every server: what runs there (cars, maps, the car hub, the map manager and baker, the agent API) and what needs attention."],
    ["Set up a server for maps", "Make my server ready for maps: check its status, turn on the agent API if it is off (ask me before restarting), and tell me what is installed."],
    ["Import a map", "Import the FiveM map at the path I give you, look at it, build it and tell me what you found."],
    ["Import a car", "Import the FiveM car at the path I give you, convert it end to end and tell me what to check in game."],
    ["Edit the world", "Start a world edit for the spot I give you: find Night City's objects there, change what I ask (delete, move, replace or add objects, their collision with them), build it and tell me what to check in game."],
  ],
  world: [
    ["What is here", "Show me Night City's objects around this edit's spot, grouped by kind, and what this edit changes so far."],
    ["Clear the spot", "Clear the props and street furniture within 8 m of this edit's spot: list them first (dry run), wait for my go, then delete them with their collision and build."],
    ["Build and install", "Build this world edit, then install it on my server with mode stage and confirm it is installed."],
  ],
};

// a Studio-wide session (no project): servers, imports, any project by name
export function studioAgentPage(root) {
  root.append(h("div.page-head", h("h1", "AI agent"), h("div.grow")),
    h("p.muted", "The agent can do what the Studio's buttons do: import FiveM cars and maps, look at them, change their settings, build, check, try maps in your game, install on your servers, move maps there and edit Night City itself. Open a car or a map for a session about that project."),
    h("div.banner.info", h("span", "Prefer Claude Code, Claude Desktop, Codex or Cursor? Connect them to the Studio (MCP): they get the same tools."),
      h("div.grow"), h("a.btn.small", { href: "#/settings/agents" }, "Connect an app")));
  const ctx = { pid: null, kind: "studio", disposers: [] };
  agentTab(root, ctx);
  return { dispose: () => ctx.disposers.forEach((d) => d()) };
}

export function agentTab(root, ctx) {
  const kind = ctx.kind || "car";
  const ai = store.state.settings.ai;
  const msgs = h("div.msgs");
  const input = h("textarea", { placeholder: "Tell the agent what to do (Enter to send, Shift+Enter for a new line)" });
  const sendBtn = h("button.btn.primary", "Start");
  const stopBtn = h("button.btn.danger.hidden", "Stop");
  const status = h("span");
  const autonomy = h("select", [["ask", "ask before every change"], ["auto_safe", "ask only before touching servers"], ["full", "never ask"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  autonomy.value = ai.autonomy;
  const provider = h("select", [["anthropic", "Claude (Anthropic API)"], ["ollama", "Ollama (local model)"]].map(([v, l]) => h("option", { value: v }, l)));
  provider.value = ai.provider === "none" ? "anthropic" : ai.provider;
  const sessions = h("div.col");
  let session = null;
  let seen = 0;
  let lastT = 0;
  const toolEls = new Map();

  const configured = (p) => (p === "anthropic" ? ai.anthropic_key_set : true);
  const setupNote = h("div");
  function checkSetup() {
    clear(setupNote);
    if (!configured(provider.value)) {
      setupNote.append(h("div.banner", h("span", "Add your Claude API key in Settings > AI, or pick Ollama to use a local model."),
        h("div.grow"), h("a.btn.small", { href: "#/settings/ai" }, "Settings")));
    }
  }
  provider.addEventListener("change", checkSetup);

  function scroll() { msgs.scrollTop = msgs.scrollHeight; }

  function addEvent(ev) {
    switch (ev.kind) {
      case "user": if (ev.text) msgs.append(h("div.msg.user", ev.text)); break;
      case "assistant": msgs.append(h("div.msg.assistant", ev.text)); break;
      case "note": msgs.append(h("div.msg.note", ev.text)); break;
      case "error": msgs.append(h("div.msg.error", ev.text)); break;
      case "question": msgs.append(h("div.msg.assistant", h("b", "Question: "), ev.text)); input.focus(); break;
      case "finished": msgs.append(h("div.msg.finished", h("b", "Done. "), ev.text)); break;
      case "tool_call": {
        const res = h("pre", "running...");
        const el = h("details.tool", h("summary", h("span.name", ev.tool), h("span.faint", summarize(ev.input)), h("div.grow"), h("span.st", pill("running"))),
          h("pre", JSON.stringify(ev.input, null, 1)), res);
        toolEls.set(ev.call_id, { el, res });
        msgs.append(el);
        break;
      }
      case "tool_result": {
        const t = toolEls.get(ev.call_id);
        if (t) {
          t.res.textContent = (ev.text || "") + (ev.images ? `\n(${ev.images} image(s) sent to the model)` : "");
          t.el.querySelector(".st").replaceChildren(ev.error ? h("span.pill.err", "error") : h("span.pill.ok", `${ev.seconds ?? 0}s`));
        }
        break;
      }
      case "approval": {
        const note = h("input", { type: "text", placeholder: "optional note to the agent", style: { flex: 1 } });
        const box = h("div.approval", h("b", `Allow ${ev.tool}?`), h("span.muted", ` (${ev.risk})`), h("pre", JSON.stringify(ev.input, null, 1)),
          h("div.row", note,
            h("button.btn.danger", { onclick: () => decide(false) }, "Decline"),
            h("button.btn.primary", { onclick: () => decide(true) }, "Allow")));
        async function decide(ok) {
          try {
            await api.post(`/agent/sessions/${session}/approve`, { call_id: ev.call_id, approve: ok, note: note.value || null });
            box.replaceWith(h("div.msg.note", `${ok ? "allowed" : "declined"}: ${ev.tool}`));
          } catch (e) { toast(e.message, "err"); }
        }
        msgs.append(box);
        break;
      }
      case "status": setStatus(ev.status); if (!["running", "starting"].includes(ev.status)) listSessions(); break;
      case "job": msgs.append(h("div.msg.note", `job: ${ev.title}`)); break;
      default: break;
    }
    scroll();
  }

  function summarize(input) {
    if (!input) return "";
    const s = JSON.stringify(input);
    return s.length > 90 ? s.slice(0, 90) + "..." : s === "{}" ? "" : s;
  }

  function setStatus(st) {
    clear(status).append(pill(st));
    const active = ["running", "starting", "waiting_approval", "waiting_user"].includes(st);
    stopBtn.classList.toggle("hidden", !active);
    sendBtn.textContent = session ? "Send" : "Start";
  }

  async function open(id) {
    session = null;                 // live events are ignored until the history is in
    toolEls.clear();
    clear(msgs).append(hint);
    const s = await api.get(`/agent/sessions/${id}`);
    for (const ev of s.events) addEvent(ev);
    lastT = s.events.length ? s.events[s.events.length - 1].t : 0;
    seen = s.total_events;
    session = id;
    setStatus(s.status);
  }

  async function send() {
    const text = input.value.trim();
    try {
      if (!session) {
        if (!text) return toast("say what the agent should do (or pick a preset)", "err");
        const s = await api.post("/agent/sessions", { project: ctx.pid || null, goal: text, autonomy: autonomy.value, provider: provider.value });
        input.value = "";
        await open(s.id);
        // events between the start and the history read are in the history; later ones come live
        listSessions();
      } else {
        if (!text) return;
        await api.post(`/agent/sessions/${session}/message`, { text });
        input.value = "";
      }
    } catch (e) { toast(e.message, "err"); }
  }
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  stopBtn.addEventListener("click", () => session && api.post(`/agent/sessions/${session}/stop`));

  async function sessionList() {
    const list = await api.get(ctx.pid ? `/agent/sessions?project=${ctx.pid}` : "/agent/sessions");
    return ctx.pid ? list : list.filter((s) => !s.project);
  }

  async function listSessions() {
    const list = await sessionList();
    clear(sessions);
    for (const s of list) {
      sessions.append(h("div.row", h("a", { href: "#", onclick: (e) => { e.preventDefault(); open(s.id); } }, (s.goal || "session").slice(0, 46)),
        h("div.grow"), pill(s.status)));
    }
    if (!list.length) sessions.append(h("span.faint", "no sessions yet"));
  }

  const off = onEvent((ev) => {
    if (ev.type !== "agent" || ev.session !== session || ev.event.t <= lastT) return;
    lastT = ev.event.t;
    addEvent(ev.event);
    seen += 1;
  });
  ctx.disposers.push(off);

  const hint = h("div.agent-hint", h("b", "Tell it what you want, in plain words."),
    h("div", kind === "studio" ? "For example: \u201cimport C:\\Downloads\\car.zip, convert it and install it on my test server\u201d."
      : kind === "map" ? "For example: \u201cbuild this map and put it at 480, 1800, 174 on my test server\u201d."
        : kind === "world" ? "For example: \u201cdelete the trash bins and the bench around this spot\u201d."
          : "For example: \u201cthe hood opens at the wrong end: fix it and build\u201d."),
    h("div.faint", "It shows each step here; what touches a server waits for your OK unless you allow it."));
  msgs.append(hint);
  root.append(setupNote, h("div.agent",
    h("div.chat", msgs, h("div.composer", input, h("div.col", sendBtn, stopBtn))),
    h("div.col",
      h("div.card", h("h3", "Agent"), status,
        h("label.field", "Model", provider),
        h("label.field", "Permissions", autonomy),
        h("div.help", "Reading and rendering never ask. Changes to the car ask in the first mode; anything on a server asks unless you choose never ask."),
        h("div.row", { style: { marginTop: "10px" } }, h("button.btn.small", { onclick: () => { session = null; clear(msgs).append(hint); setStatus("new"); input.focus(); } }, "New session"))),
      h("div.card", h("h3", "Presets"), (PRESETS[kind] || PRESETS.car).map(([label, text]) => h("button.btn.small", { style: { margin: "0 6px 6px 0" },
        onclick: () => { input.value = text; input.focus(); } }, label))),
      h("div.card", h("h3", "Sessions"), sessions))));
  checkSetup();
  listSessions().then(async () => {
    const list = await sessionList();
    if (list.length) open(list[0].id);
    else setStatus("new");
  });
}
