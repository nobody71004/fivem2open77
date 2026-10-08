// Settings: tools (game, WolvenKit, .NET, 7-Zip, donor files), AI, build options.
import { api, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, logLines } from "../ui.js";

export function settingsPage(root, args) {
  const section = args[0] || "tools";
  const tabs = h("div.tabs", [["tools", "Tools"], ["ai", "AI"], ["build", "Build"]].map(([k, l]) =>
    h("a", { href: `#/settings/${k}`, class: k === section ? "active" : "" }, l)));
  const body = h("div");
  root.append(h("div.page-head", h("h1", "Settings")), tabs, body);
  ({ tools, ai, build }[section] || tools)(body);
  return {};
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
