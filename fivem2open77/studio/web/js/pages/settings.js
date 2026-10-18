// Settings: tools (game, WolvenKit, .NET, 7-Zip, donor files), AI, build options.
import { api, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, put, toast, logLines, pill, fmtAgo, confirmBox, icon } from "../ui.js";
import { fxLevel, setFxLevel } from "../fx.js";

export function settingsPage(root, args) {
  const section = args[0] || "tools";
  const tabs = h("div.tabs", [["tools", "Tools"], ["ai", "AI"], ["agents", "Agents (MCP)"], ["build", "Build"], ["look", "Appearance"]].map(([k, l]) =>
    h("a", { href: `#/settings/${k}`, class: k === section ? "active" : "" }, l)));
  const body = h("div");
  root.append(h("div.page-head", h("h1", "Settings")), tabs, body);
  const ret = ({ tools, ai, agents, build, look }[section] || tools)(body);
  return ret || {};
}

// ------------------------------------------------------------------ appearance (this computer only)
function look(body) {
  const levels = [
    ["full", "FULL", "The living background, the cursor's light trail, sparks and links, target lock, decoding titles, tilting cards."],
    ["lite", "LITE", "A still background that answers the cursor, target lock and lit panels. Nothing animates on its own."],
    ["off", "OFF", "No effects: plain panels, the system cursor only."],
  ];
  const grid = h("div.fx-levels");
  function draw() {
    clear(grid).append(...levels.map(([k, title, text]) => {
      const inp = h("input", { type: "radio", name: "fx", value: k, checked: fxLevel() === k });
      const el = h("label.fx-level" + (fxLevel() === k ? ".on" : ""), inp, h("b", title), h("span", text));
      inp.addEventListener("change", () => { setFxLevel(k); draw(); toast(`Effects: ${k}`, "ok"); });
      return el;
    }));
  }
  draw();
  body.append(h("div.card", h("h3", icon("command"), "Effects"),
    h("p.help", "How much of the Studio moves. Kept on this computer. With the system's reduce-motion setting on, the Studio starts at Lite. ",
      "The 3D views (Night City in Live maps, the car and map viewers) keep the cursor clean either way."),
    grid));
}

// ------------------------------------------------------------------ agents outside the Studio (MCP)
const STATE_LABEL = { registered: ["ok", "connected"], outdated: ["warn", "needs update"], not_registered: ["", "not connected"],
  not_installed: ["", "not installed"], unreadable: ["err", "config unreadable"] };

function copyText(t) {
  navigator.clipboard.writeText(t).then(() => toast("Copied", "ok"), () => toast("Could not copy", "err"));
}

function agents(body) {
  let info = null;
  const top = h("div.col"), clients = h("div"), conn = h("div"), sessions = h("div");
  body.append(h("div.col", { style: { gap: "12px" } }, top, clients, conn, sessions));

  async function load() {
    info = await api.get("/mcp");
    renderTop(); renderClients(); renderConn(); renderSessions();
  }

  function renderTop() {
    const m = info.settings;
    const on = h("input", { type: "checkbox", checked: m.enabled });
    const ncwePath = h("input", { type: "text", value: store.state.settings.paths.ncwe_mcp || "",
      placeholder: "C:\\Games\\NCWE\\NCWE.Mcp.exe", spellcheck: false });
    const ncweLaunch = h("input", { type: "checkbox", checked: m.ncwe_launch });
    const appr = h("select", h("option", { value: "client" }, "the agent's app asks you before each call"),
      h("option", { value: "studio" }, "installs and restarts also wait for a click here"));
    appr.value = m.approval;
    const secs = h("input", { type: "number", min: 10, max: 600, value: m.call_seconds, style: { width: "90px" } });
    const shell = h("input", { type: "checkbox", checked: !!m.shell });
    const tshell = h("select", [["auto", "automatic (PowerShell on Windows)"], ["powershell", "PowerShell"], ["cmd", "Command Prompt (cmd)"],
      ["bash", "bash"], ["sh", "sh"]].map(([v, l]) => h("option", { value: v }, l)));
    tshell.value = (store.state.settings.terminal || {}).shell || "auto";
    put(clear(top), info.pending.length ? h("div.card", { style: { borderColor: "var(--accent-line)" } },
      h("h3", icon("alert"), "Waiting for you"),
      info.pending.map((p) => h("div.approval", { style: { marginBottom: "8px" } },
        h("div.row", h("b", p.label), h("span.muted", "wants to run"), h("code", p.tool), h("div.grow"),
          h("button.btn.small", { onclick: () => decide(p.id, false) }, "Decline"),
          h("button.btn.primary.small", { onclick: () => decide(p.id, true) }, "Allow")),
        h("pre", JSON.stringify(p.args, null, 1))))) : null,
    h("div.card", h("h3", icon("agent"), "Agents outside the Studio"),
      h("p.help", "Claude Code, Claude Desktop, Codex, Cursor or VS Code can drive the Studio through MCP: the same tools as the Studio's own agent ",
        `(${info.tools} of them: import and build cars and maps, install them on your servers, move maps live, edit Night City). `,
        "What they do shows up here and in Activity."),
      h("div.form", { style: { marginTop: "8px" } },
        h("label.field", "MCP server", h("label.row", on, h("span", "on (agents can connect)"))),
        h("label.field", "Before an agent changes a server", appr),
        h("label.field", "A long call answers \u201crunning\u201d after (s)", secs),
        h("label.field", "System commands", h("label.row", shell, h("span", "Let agents run system commands in the Studio's terminal (sh, !, cli)")))),
      h("p.help", "Agents drive everything else without your screen: the terminal (every tool as a command), the Studio's API, ",
        "its own AI agent and its pages (studio_ui). System commands and the converter's command line stay yours unless you tick the box: ",
        "they can do anything this Windows user can. Servers, secrets, approvals and these settings stay yours either way."),
      h("div.row", { style: { marginTop: "12px" } }, h("div.grow"),
        h("button.btn.primary", { onclick: async () => {
          await save({ mcp: { enabled: on.checked, approval: appr.value, call_seconds: Number(secs.value), shell: shell.checked } });
          load();
        } }, "Save"))),
    h("div.card", h("h3", icon("terminal"), "Terminal"),
      h("p.help", "The Studio's command line (Terminal, or Ctrl+` on any page): every tool is a command, plus jobs, the AI agent, the API, ",
        "the converter's command line and system commands. ", h("code", "python -m fivem2open77 term"), " opens it in a command prompt."),
      h("div.form", h("label.field", "System commands run in", tshell)),
      h("div.row", { style: { marginTop: "12px" } }, h("a.btn", { href: "#/terminal" }, "Open the terminal"), h("div.grow"),
        h("button.btn.primary", { onclick: async () => { if (await save({ terminal: { shell: tshell.value } })) load(); } }, "Save"))),
    h("div.card", h("h3", icon("world"), "NCWE world editor · 60 native tools"),
      h("p.help", "The built-in agent and every connected MCP client can use the complete NCWE beta.3 toolset as ncwe_* tools: assets, screenshots, raycasts, rooms, lights, sound, collision, roads, elevators, prefabs, imports and exports. NCWE edits its own project, not the converted MLO automatically."),
      h("div.form", h("label.field", "NCWE MCP executable", ncwePath),
        h("label.field", "Editor startup", h("label.row", ncweLaunch, h("span", "Allow NCWE to launch when it is not running")))),
      h("p.help", "Default: attach only. Open NCWE and sign in with your accepted beta account. Studio does not bypass NCWE access checks or bundle its binaries. Editor mutations and exports use the same approval gate as server changes."),
      h("div.row", h("a.btn", { href: "#/agent" }, "Open agent"), h("div.grow"),
        h("button.btn.primary", { onclick: async () => {
          if (await save({ paths: { ncwe_mcp: ncwePath.value.trim() }, mcp: { ncwe_launch: ncweLaunch.checked } })) load();
        } }, "Save NCWE"))));
  }

  async function decide(id, approve) {
    try { await api.post("/mcp/approve", { id, approve }); } catch (e) { toast(e.message, "err"); }
    load();
  }

  function renderClients() {
    const rows = info.clients.map((c) => {
      const [cls, label] = STATE_LABEL[c.state] || ["", c.state];
      const act = async (remove) => {
        try {
          const r = await api.post("/mcp/connect", { client: c.client, remove });
          if (r.manual) {
            toast(r.note, "");
            copyText(r.manual);
          } else toast(`${c.label}: ${remove ? "removed" : "connected"}. ${r.note || ""}`, "ok");
        } catch (e) { toast(e.message, "err"); }
        load();
      };
      return h("tr",
        h("td", h("b", c.label), c.store_app ? h("span.faint", { title: "This app is the Microsoft Store build: it cannot start the Studio, keep the Studio open while you use it there" }, " · Microsoft Store") : null),
        h("td", h("span.pill" + (cls ? "." + cls : ""), label)),
        h("td.muted", h("code", c.file || "")),
        h("td", { style: { textAlign: "right", whiteSpace: "nowrap" } },
          c.state === "registered" ? h("button.btn.ghost.small", { onclick: () => act(true) }, "Remove")
            : h("button.btn.small" + (c.state === "not_installed" ? "" : ".primary"), { onclick: () => act(false) },
              c.state === "outdated" ? "Update" : "Connect")));
    });
    clear(clients).append(h("div.card.table-card", h("div.panel-head", { style: { margin: 0 } }, h("h3", "Connect an app")),
      h("table.tbl", h("thead", h("tr", h("th", "App"), h("th", "State"), h("th", "Its settings file"), h("th", ""))), h("tbody", rows)),
      h("p.help", { style: { padding: "10px 16px 12px", margin: 0 } },
        "Connect writes an entry named fivem2open77 into the app's own settings (the old file is kept next to it as .f2o77-backup). ",
        "The app starts the Studio when it is not running; a Microsoft Store build of an app cannot, so keep the Studio open while you use one.")));
  }

  function renderConn() {
    const cmd = [info.command.command, ...info.command.args].map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(" ");
    const tok = h("code", "••••••••••••••••");
    clear(conn).append(h("div.card", h("h3", "By hand"),
      h("div.kv",
        h("span", "command (stdio)"), h("div.row", h("code", cmd), h("button.btn.ghost.small", { onclick: () => copyText(cmd) }, icon("copy"))),
        h("span", "address (HTTP, this run)"), h("div.row", h("code", info.url), h("button.btn.ghost.small", { onclick: () => copyText(info.url) }, icon("copy"))),
        h("span", "token"), h("div.row", tok,
          h("button.btn.ghost.small", { onclick: async () => { const t = await api.get("/mcp/token"); tok.textContent = t.token; } }, icon("eye"), "Show"),
          h("button.btn.ghost.small", { onclick: async () => { const t = await api.get("/mcp/token"); copyText(t.token); } }, icon("copy")),
          h("button.btn.ghost.small", { onclick: async () => {
            if (await confirmBox("New token", "Apps connected with the stdio command keep working (they read the new one). Anything set up with the old token by hand stops working.", "Make a new token", "danger")) {
              await api.post("/mcp/token/reset"); toast("New token", "ok"); load();
            }
          } }, "Reset"))),
      h("p.help", { style: { marginTop: "10px" } }, "The address changes each time the Studio starts: apps should use the command, which finds the Studio by itself.")));
  }

  function renderSessions() {
    clear(sessions).append(h("div.card", h("h3", "Connected agents"),
      !info.sessions.length ? h("div.faint", "No agent is connected.") :
        info.sessions.map((s) => h("div", { style: { borderTop: "1px solid var(--line)", padding: "10px 0" } },
          h("div.row", h("b", s.label), h("span.faint", `${s.client.name} ${s.client.version}`), h("span.faint", `protocol ${s.protocol}`),
            s.active.length ? pill("running") : null, h("div.grow"), h("span.muted", `seen ${fmtAgo(s.seen)}`),
            h("button.btn.ghost.small", { title: "End this session", onclick: async () => { await api.del(`/mcp/sessions/${s.id}`); load(); } }, icon("close"))),
          h("table.tbl", { style: { marginTop: "6px" } }, h("tbody",
            [...s.active.map((c) => ({ ...c, running: true })), ...s.calls.slice().reverse()].slice(0, 12).map((c) => h("tr",
              h("td", { style: { width: "28%" } }, h("code", c.tool)),
              h("td", c.running ? pill("running") : c.error ? h("span.pill.err", "error") : h("span.pill.ok", "ok")),
              h("td.muted", { style: { maxWidth: "520px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, c.text || JSON.stringify(c.args || {})),
              h("td.num.faint", c.running ? "" : `${c.seconds}s`)))))))));
  }

  const off = store.on((ev) => { if (ev.type === "mcp" && ["connected", "result", "approval", "approval_done", "closed"].includes(ev.kind)) load().catch(() => {}); });
  load().catch((e) => clear(top).append(h("div.card.err", e.message)));
  return { dispose: off };
}

async function save(patch) {
  try {
    await api.patch("/settings", patch);
    await refreshState();
    toast("Saved", "ok");
    return true;
  } catch (e) {
    toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
    return false;
  }
}

function tools(body) {
  const st = store.state;
  const t = st.tools;
  const p = st.settings.paths;
  const inputs = {};
  const item = (key, label, tool, setting, optional) => {
    const input = h("input", { type: "text", value: p[setting] || "", placeholder: tool.path || "not found", style: { width: "100%" } });
    inputs[setting] = input;
    return h("div.setup-item", h("span.dot" + (tool.ok ? ".ok" : optional ? ".opt" : "")), h("b", label),
      h("div", input, h("div.help", tool.ok ? `using ${tool.path}` : tool.help)), h("span", tool.ok ? h("span.ok", "ok") : h("span.err", "missing")));
  };
  const donor = t.donor;
  const donorLog = h("div");
  const donorList = Object.entries(donor.donors).map(([k, d]) => h("div.row", h("span.dot" + (d.ready ? ".ok" : "")), d.label,
    h("span.faint", `${d.files}/${d.needed} files`)));
  body.append(
    h("div.card", h("h3", "Tools"),
      item("game", "Cyberpunk 2077", t.game, "game_dir"),
      t.game.found && t.game.found.length > 1 ? h("div.help", "found: " + t.game.found.map((g) => `${g.path} (${g.where})`).join(", ")) : null,
      item("wolvenkit", "WolvenKit CLI", t.wolvenkit, "wolvenkit"),
      item("dotnet", ".NET 8 runtime", t.dotnet, "dotnet"),
      item("sevenzip", "7-Zip", t.sevenzip, "sevenzip", true),
      t.linux_dotnet ? item("linux_dotnet", ".NET 8 for Linux (map baker)", t.linux_dotnet, "linux_dotnet", true) : null,
      h("div.setup-item", h("span.dot" + (donor.ok ? ".ok" : "")), h("b", "Donor files"),
        h("div", h("input", { type: "text", value: p.cache_dir || "", placeholder: donor.path, style: { width: "100%" },
          oninput: (e) => { inputs.cache_dir = e.target; } }), donorList, h("div.help", donor.help)),
        h("button.btn.small" + (donor.ok ? "" : ".primary"), { disabled: !t.game.ok || !t.wolvenkit.ok, onclick: async () => {
          const job = await api.post("/tools/donor");
          const log = h("div.log", { style: { maxHeight: "200px" } });
          clear(donorLog).append(log);
          const j = await waitJob(job.id, (jj) => clear(log).append(...logLines(jj.allLines)));
          if (j.state === "done") { await refreshState(); location.reload(); }
        } }, donor.ok ? "Extract again" : "Extract now")),
      donorLog,
      h("div.row", { style: { marginTop: "12px" } },
        h("button.btn", { onclick: async () => { await api.get("/tools?refresh=1"); await refreshState(); location.reload(); } }, "Detect again"),
        h("div.grow"),
        h("button.btn.primary", { onclick: async () => {
          const patch = {};
          for (const [k, el] of Object.entries(inputs)) patch[k] = el.value.trim();
          if (await save({ paths: patch })) location.reload();
        } }, "Save paths"))),
    h("div.card", h("h3", "Where things are"), h("div.kv", h("span", "workspace"), h("code", st.workspace),
      h("span", "app"), h("code", t.app_home || "(running from source)"), h("span", "secrets"),
      h("span", st.settings.secrets_encrypted ? "encrypted for your Windows account" : "in a file only your user can read"))));
}

function ai(body) {
  const a = store.state.settings.ai;
  const provider = h("select", [["anthropic", "Claude (Anthropic API)"], ["ollama", "Ollama (local)"], ["none", "off"]].map(([v, l]) => h("option", { value: v }, l)));
  provider.value = a.provider;
  const key = h("input", { type: "password", placeholder: a.anthropic_key_set ? "saved - type to replace" : "sk-ant-...", style: { flex: 1 } });
  const model = h("select", h("option", { value: a.anthropic_model }, a.anthropic_model || "newest Sonnet (automatic)"));
  const base = h("input", { type: "text", value: a.anthropic_base_url });
  const ourl = h("input", { type: "text", value: a.ollama_url });
  const omodel = h("select", h("option", { value: a.ollama_model }, a.ollama_model || "first installed model"));
  const autonomy = h("select", [["ask", "ask before every change"], ["auto_safe", "ask only before touching servers"], ["full", "never ask"]].map(([v, l]) => h("option", { value: v }, l)));
  autonomy.value = a.autonomy;
  const vision = h("input", { type: "checkbox", checked: a.vision });
  const steps = h("input", { type: "number", min: 5, max: 200, value: a.max_steps });
  const result = h("div");

  async function models(which, sel, current) {
    const r = await api.get(`/ai/models?provider=${which}`);
    if (r.error) { toast(r.error, "err"); return; }
    clear(sel).append(h("option", { value: "" }, which === "anthropic" ? "newest Sonnet (automatic)" : "first installed model"),
      r.models.map((m) => h("option", { value: m.id }, m.name !== m.id ? `${m.name} (${m.id})` : m.id)));
    sel.value = current || "";
  }

  body.append(
    h("div.card", h("h3", "AI agent"),
      h("p.help", "The agent drives the same tools as the buttons: inspect, render, edit the profile, build, check, install. Claude reads the renders to check the parts."),
      h("div.form", h("label.field", "Provider", provider), h("label.field", "Permissions", autonomy),
        h("label.field", "Max steps per turn", steps), h("label.field", "Send renders to the model", h("label.row", vision, h("span", "vision")))) ),
    h("div.card", h("h3", "Claude"),
      h("label.field", "API key (console.anthropic.com)", h("div.row", key,
        h("button.btn", { onclick: async () => {
          await api.post("/secrets", { key: "anthropic_api_key", value: key.value.trim() || null });
          key.value = ""; await refreshState(); toast("API key saved", "ok"); location.reload();
        } }, "Save key"),
        a.anthropic_key_set ? h("button.btn.ghost", { onclick: async () => { await api.post("/secrets", { key: "anthropic_api_key", value: null }); await refreshState(); location.reload(); } }, "Remove") : null)),
      h("div.form", { style: { marginTop: "10px" } }, h("label.field", "Model", h("div.row", model, h("button.btn.small", { onclick: () => models("anthropic", model, a.anthropic_model) }, "List"))),
        h("label.field", "API address", base))),
    h("div.card", h("h3", "Ollama"),
      h("p.help", "Runs models on this PC. Pick one that supports tool calling (for example qwen2.5, llama3.1, mistral-nemo); a vision model can also read the renders."),
      h("div.form", h("label.field", "Address", ourl), h("label.field", "Model", h("div.row", omodel, h("button.btn.small", { onclick: () => models("ollama", omodel, a.ollama_model) }, "List"))))),
    h("div.row",
      h("button.btn", { onclick: async () => {
        clear(result).append(h("span.muted", "testing..."));
        const r = await api.post("/ai/test", { provider: provider.value });
        clear(result).append(r.ok ? h("span.ok", `ok - ${r.model} replied "${r.reply}"`) : h("span.err", r.error));
      } }, "Test"), result, h("div.grow"),
      h("button.btn.primary", { onclick: () => save({ ai: { provider: provider.value, anthropic_model: model.value, anthropic_base_url: base.value.trim(),
        ollama_url: ourl.value.trim(), ollama_model: omodel.value, autonomy: autonomy.value, vision: vision.checked, max_steps: Number(steps.value) } }) }, "Save")));
}

function build(body) {
  const b = store.state.settings.build;
  const d = store.state.settings.deploy;
  const keep = h("input", { type: "checkbox", checked: b.keep_work });
  const mode = h("select", [["stage", "install, restart when nobody is online"], ["restart", "install and restart now"], ["files", "install files only"]].map(([v, l]) => h("option", { value: v }, l)));
  mode.value = d.mode;
  const srv = h("select", h("option", { value: "" }, "(ask)"), store.state.settings.servers.map((s) => h("option", { value: s.id }, s.label)));
  srv.value = d.default_server;
  body.append(h("div.card", h("h3", "Build and install"),
    h("label.row", keep, h("span", "keep WolvenKit's intermediate files (for debugging)")),
    h("div.form", { style: { marginTop: "12px" } }, h("label.field", "Default install", mode), h("label.field", "Default server", srv)),
    h("div.row", { style: { marginTop: "12px" } }, h("div.grow"),
      h("button.btn.primary", { onclick: () => save({ build: { keep_work: keep.checked }, deploy: { mode: mode.value, default_server: srv.value } }) }, "Save"))));
}
