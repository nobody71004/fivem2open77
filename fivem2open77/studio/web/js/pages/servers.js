// Open77 servers the Studio installs cars on (over SSH).
import { api, waitJob } from "../api.js";
import { store, refreshState } from "../store.js";
import { h, clear, toast, modal, confirmBox, logLines, pill } from "../ui.js";

const EMPTY = { id: "", label: "", host: "", port: 22, user: "root", auth: "key", key_file: "", servers_root: "/opt",
  names: [], systemctl: "systemctl", allow_unsecured: false };

export function serversPage(root) {
  const list = h("div.col");
  const out = h("div");

  async function load() {
    const st = await refreshState();
    clear(list);
    if (!st.settings.servers.length) list.append(h("div.card.empty", "No servers yet. Add the Open77 server you play on (it needs SSH access)."));
    for (const s of st.settings.servers) list.append(card(s));
  }

  function card(s) {
    const statusBox = h("div");
    return h("div.card",
      h("div.row", h("h3", { style: { margin: 0 } }, s.label), h("code.muted", `${s.user}@${s.host}:${s.port}`), h("div.grow"),
        h("button.btn.small", { onclick: () => edit(s) }, "Edit"),
        h("button.btn.small.danger", { onclick: async () => {
          if (await confirmBox("Remove server", `Remove ${s.label} from the Studio? Nothing changes on the server.`, "Remove", "danger")) {
            await api.del(`/servers/${s.id}`); load();
          }
        } }, "Remove")),
      h("div.kv", { style: { marginTop: "8px" } }, h("span", "server folders"), h("span", s.names.map((n) => `${s.servers_root}/${n}`).join(", ")),
        h("span", "sign-in"), h("span", s.auth === "key" ? `key ${s.key_file}` : s.auth === "password" ? (s.password_set ? "password (saved)" : "password (missing)") : "SSH agent"),
        h("span", "services"), h("span", s.systemctl === "systemctl" ? "system units" : "user units")),
      h("div.row", { style: { marginTop: "12px" } },
        h("button.btn.small", { onclick: () => action(s, "test", statusBox) }, "Test connection"),
        h("button.btn.small", { onclick: () => action(s, "status", statusBox) }, "Status"),
        h("button.btn.small.cyan", { onclick: async () => {
          if (await confirmBox("Install the car hub", "Install f2o77_hub on " + s.label + "? It starts with the next server restart and from then on restarts the server to add new cars when nobody is online.", "Install")) {
            action(s, "hub", statusBox, { mode: "stage" });
          }
        } }, "Install car hub")),
      statusBox);
  }

  async function action(s, what, box, body = {}) {
    const log = h("div.log", { style: { maxHeight: "220px" } });
    clear(box).append(h("div", { style: { marginTop: "12px" } }, log));
    try {
      const job = await api.post(`/servers/${s.id}/${what}`, body);
      const j = await waitJob(job.id, (jj) => { clear(log).append(...logLines(jj.allLines)); });
      if (j.state !== "done") { log.append(h("div.e", j.error || j.state)); return; }
      if (what === "test") {
        const r = j.result;
        clear(box).append(h("div.card", { style: { marginTop: "12px" } }, h("div.kv",
          h("span", "connection"), h("span.ok", "ok"), h("span", "system"), h("span", r.system), h("span", "python3"), h("span", r.python3 ? "yes" : h("b.err", "missing")),
          ...Object.entries(r.servers).flatMap(([n, ok]) => [h("span", n), ok ? h("span.ok", "found") : h("span.err", "no server.jsonc")]))));
      } else if (what === "status") {
        clear(box).append(...j.result.map((x) => h("div.card", { style: { marginTop: "12px" } },
          h("div.row", h("b", x.name), h("span.muted", x.version || ""), pill(x.unit === "active" ? "running" : x.unit), h("span.muted", `${(x.online || []).length} online`)),
          h("div.help", (x.cars || []).length ? "cars: " + x.cars.map((c) => `${c.display || c.name} ${c.version}`).join(", ") : "no converted cars"),
          h("div.help", x.hub ? `car hub ${x.hub.version}` + (x.hub.status ? `: ${(x.hub.status.waiting || []).length} waiting` : " (starts at the next restart)") : "car hub not installed"))));
      }
    } catch (e) { log.append(h("div.e", e.message)); }
  }

  async function edit(s) {
    const isNew = !s;
    s = Object.assign({}, EMPTY, s || {});
    const f = {};
    const field = (k, label, el) => { f[k] = el; return h("label.field", label, el); };
    const auth = h("select", [["key", "private key file"], ["password", "password"], ["agent", "SSH agent / Pageant"]].map(([v, l]) => h("option", { value: v }, l)));
    auth.value = s.auth;
    const sysctl = h("select", [["systemctl", "system services (root)"], ["systemctl --user", "user services (systemctl --user)"]].map(([v, l]) => h("option", { value: v }, l)));
    sysctl.value = s.systemctl;
    const pw = h("input", { type: "password", placeholder: s.password_set ? "saved (leave empty to keep)" : "" });
    const pp = h("input", { type: "password", placeholder: s.passphrase_set ? "saved (leave empty to keep)" : "only if the key has one" });
    const unsec = h("input", { type: "checkbox", checked: s.allow_unsecured });
    const keyRow = h("div", field("key_file", "Private key file", h("input", { type: "text", value: s.key_file, placeholder: "C:\\Users\\me\\.ssh\\id_ed25519" })),
      h("label.field", "Key passphrase", pp));
    const pwRow = h("label.field", "Password", pw);
    const sync = () => { keyRow.classList.toggle("hidden", auth.value !== "key"); pwRow.classList.toggle("hidden", auth.value !== "password"); };
    auth.addEventListener("change", sync);
    const body = h("div.col",
      h("div.form",
        field("label", "Name", h("input", { type: "text", value: s.label, placeholder: "My server" })),
        field("id", "Id (a-z 0-9 - _)", h("input", { type: "text", value: s.id, placeholder: "home", disabled: !isNew })),
        field("host", "Host", h("input", { type: "text", value: s.host, placeholder: "192.168.0.10" })),
        field("port", "SSH port", h("input", { type: "number", value: s.port })),
        field("user", "User", h("input", { type: "text", value: s.user })),
        h("label.field", "Sign in with", auth)),
      keyRow, pwRow,
      h("div.form",
        field("servers_root", "Folder that holds the servers", h("input", { type: "text", value: s.servers_root })),
        field("names", "Server folders (comma separated)", h("input", { type: "text", value: s.names.join(", "), placeholder: "open77-myserver" })),
        h("label.field", "Services", sysctl)),
      h("div.help", "Each server folder is <folder>/<name> and runs as the systemd unit <name>.service."),
      h("label.row", unsec, h("span", "allow switching the world to unsecured (ArchiveXL/TweakXL are executable packages)")));
    sync();
    const r = await modal(isNew ? "Add a server" : `Edit ${s.label}`, body, [{ label: "Cancel", value: null }, { label: "Save", value: "save", kind: "primary" }]);
    if (r !== "save") return;
    const data = {
      id: f.id.value.trim().toLowerCase(), label: f.label.value.trim(), host: f.host.value.trim(), port: Number(f.port.value || 22),
      user: f.user.value.trim(), auth: auth.value, key_file: f.key_file.value.trim(), servers_root: f.servers_root.value.trim(),
      names: f.names.value.split(",").map((x) => x.trim()).filter(Boolean), systemctl: sysctl.value, allow_unsecured: unsec.checked,
    };
    if (pw.value) data.password = pw.value;
    if (pp.value) data.passphrase = pp.value;
    try {
      await api.post("/servers", data);
      toast("Server saved", "ok");
      load();
    } catch (e) {
      toast(e.errors && e.errors.length ? e.errors.join("; ") : e.message, "err");
      edit(Object.assign(data, { password_set: s.password_set }));
    }
  }

  const importInput = h("input", { type: "file", accept: ".json,application/json", class: "hidden", onchange: async () => {
    const f = importInput.files[0];
    importInput.value = "";
    if (!f) return;
    try {
      const r = await api.post("/servers/import", JSON.parse(await f.text()));
      toast(`added ${r.added.length} server(s)` + (r.skipped.length ? `, ${r.skipped.length} already there` : "") +
            (r.errors.length ? `; problems: ${r.errors.join("; ")}` : ""), r.errors.length ? "err" : "ok");
      load();
    } catch (e) { toast(e.message, "err"); }
  } });
  root.append(h("div.page-head", h("h1", "Servers"), h("div.grow"),
    h("button.btn", { onclick: () => importInput.click() }, "Import..."), importInput,
    h("button.btn.primary", { onclick: () => edit(null) }, "Add server")),
    h("p.muted", "The Studio installs cars over SSH with the same steps as the command line: backup, resources.load, a config check, then a restart now or through the car hub."),
    list, out);
  load().catch((e) => toast(e.message, "err"));
  return {};
}
