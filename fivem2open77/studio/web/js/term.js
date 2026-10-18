// The Studio's terminal (terminal.py): a console for every Studio tool, the AI agent, the API, the
// converter's command line and system commands.  mountTerminal() is the console itself; the Terminal
// page and the dock (Ctrl+`) mount one each, on the same session, so the scrollback follows you.
// Every line arrives on the event stream: agents' commands show here as they run.
import { api } from "./api.js";
import { store } from "./store.js";
import { h, clear, toast, modal } from "./ui.js";

const SESSION_KEY = "f2o77.term.session";
const PREFS_KEY = "f2o77.term.prefs";
const HEX12 = /\b[a-f0-9]{12}\b/g;

export function prefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch (_) { p = {}; }
  return { showAgents: p.showAgents !== false, followAgents: p.followAgents !== false };
}
export function setPref(k, v) {
  const p = prefs();
  p[k] = v;
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (_) { /* private mode */ }
}

let specCache = null;
let specAt = 0;
export async function loadSpec(force = false) {
  if (!force && specCache && Date.now() - specAt < 15000) return specCache;
  specCache = await api.get("/terminal/spec");
  specAt = Date.now();
  return specCache;
}

// ------------------------------------------------------------------ JSON colours
function jsonSpans(text) {
  const out = [];
  const rx = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let last = 0, m;
  while ((m = rx.exec(text))) {
    if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
    if (m[1] !== undefined) {
      out.push(h("span." + (m[2] ? "j-key" : "j-str"), m[1]));
      if (m[2]) out.push(document.createTextNode(m[2]));
    } else if (m[3] !== undefined) out.push(h("span.j-bool", m[3]));
    else if (m[4] !== undefined) out.push(h("span.j-num", m[4]));
    else out.push(h("span.j-null", "null"));
    last = rx.lastIndex;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}

// text with its ids (jobs, projects, runs) clickable: a click puts the id in the input
function refText(text, onRef) {
  const out = [];
  let last = 0, m;
  HEX12.lastIndex = 0;
  while ((m = HEX12.exec(text))) {
    if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
    const id = m[0];
    out.push(h("span.ref", { title: "click: put it in the command line", onclick: () => onRef(id) }, id));
    last = HEX12.lastIndex;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}

// ------------------------------------------------------------------ the command line, read as the server does
function splitWords(text) {
  // [{text, start, end, quoted}] -- quotes group words, a JSON value is one word
  const words = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    if (/\s/.test(text[i])) { i++; continue; }
    const start = i;
    let quoted = false;
    let buf = "";
    if (text[i] === "{" || text[i] === "[") {
      let depth = 0, inStr = false;
      for (; i < n; i++) {
        const c = text[i];
        if (inStr) { if (c === "\\") { i++; continue; } if (c === '"') inStr = false; }
        else if (c === '"') inStr = true;
        else if (c === "{" || c === "[") depth++;
        else if (c === "}" || c === "]") { depth--; if (!depth) { i++; break; } }
      }
      words.push({ text: text.slice(start, i), start, end: i, quoted: false });
      continue;
    }
    while (i < n && !/\s/.test(text[i])) {
      const c = text[i];
      if (c === '"' || c === "'") {
        const q = c; i++;
        const s = i;
        while (i < n && text[i] !== q) i++;
        buf += text.slice(s, i);
        i++;
        quoted = true;
      } else { buf += c; i++; }
    }
    words.push({ text: buf, start, end: Math.min(i, n), quoted });
  }
  return words;
}

// ------------------------------------------------------------------ the console
export function mountTerminal(root, opts = {}) {
  const st = {
    sid: null, spec: null, blocks: new Map(), active: new Set(), hist: [], hi: -1, draft: "",
    comp: null, project: null, cwd: "", disposed: false, follow: true,
  };
  const pf = prefs();

  // ---- the frame
  const sig = h("span.sig");
  const where = h("span.where", "connecting");
  const showAgents = h("input", { type: "checkbox", checked: pf.showAgents, onchange: () => setPref("showAgents", showAgents.checked) });
  const followAgents = h("input", { type: "checkbox", checked: pf.followAgents, onchange: () => setPref("followAgents", followAgents.checked) });
  const bar = h("div.term-bar", sig, h("b", "f2o77 // term"), where, h("div.grow"),
    h("label.tog", { title: "Show the commands agents run (MCP) in this terminal" }, showAgents, "agents"),
    h("label.tog", { title: "Let agents show you pages of the Studio (studio_ui)" }, followAgents, "follow"),
    opts.onClose ? h("button.btn.ghost.small", { title: "Close (Ctrl+`)", onclick: opts.onClose }, "\u2715") : null);
  const out = h("div.term-out", { tabindex: "-1" });
  const ps = h("span.ps", "f2o77 \u203a");
  const ta = h("textarea", { rows: 1, spellcheck: false, autocomplete: "off", "aria-label": "Command" });
  const ghost = h("div.ghost");
  const comp = h("div.term-comp");
  const sigline = h("div.term-sig");
  const inbox = h("div.term-in", comp, ps, h("div.field", ghost, ta));
  const term = h("div.term", bar, out, sigline, inbox);
  root.append(term);

  out.addEventListener("click", (e) => {
    if (e.target === out && !window.getSelection().toString()) ta.focus();
  });
  out.addEventListener("scroll", () => { st.follow = out.scrollHeight - out.scrollTop - out.clientHeight < 40; });

  function scroll(force) {
    if (force || st.follow) out.scrollTop = out.scrollHeight;
  }

  function setPrompt(project, cwd) {
    st.project = project || null;
    if (cwd) st.cwd = cwd;
    clear(ps).append("f2o77", project ? h("i", ` [${project.name}]`) : "", " \u203a");
    clear(where).append(`session ${st.sid || "\u2026"}` + (project ? ` \u00b7 ${project.kind} ${project.name}` : "") +
      (st.cwd ? ` \u00b7 ${st.cwd}` : ""));
  }

  function busy() {
    const on = st.active.size > 0;
    bar.classList.toggle("busy", on);
    if (opts.onBusy) opts.onBusy(on);
  }

  // ---- blocks: one per command run
  function block(run, label, own) {
    let b = st.blocks.get(run);
    if (b) return b;
    const el = h("div.tblk" + (own ? "" : ".other"));          // (not .agent: that is the Agent page's grid)
    b = { el, own, label, jobs: new Map() };
    st.blocks.set(run, b);
    out.append(el);
    if (st.blocks.size > 400) {           // keep the page light: the oldest blocks go
      const first = st.blocks.keys().next().value;
      st.blocks.get(first).el.remove();
      st.blocks.delete(first);
    }
    return b;
  }

  function putRef(id) {
    const v = ta.value;
    ta.value = v + (v && !/\s$/.test(v) ? " " : "") + id;
    ta.focus();
    autosize();
    hint();
  }

  function lineEl(line) {
    const k = line.k;
    if (k === "json") {
      const el = h("div.tl.json", jsonSpans(line.t));
      const n = (line.t.match(/\n/g) || []).length;
      if (n > 14) {
        el.classList.add("fold");
        const more = h("span.more", `show all ${n + 1} lines`);
        more.onclick = () => { el.classList.toggle("fold"); more.textContent = el.classList.contains("fold") ? `show all ${n + 1} lines` : "fold"; };
        return [el, more];
      }
      return [el];
    }
    if (k === "table" && Array.isArray(line.cols)) {
      const head = h("tr", line.cols.map((c) => h("th", String(c))));
      const rows = (line.rows || []).map((r) => h("tr", r.map((v, i) => {
        const s = v === null || v === undefined ? "" : String(v);
        const col = String(line.cols[i] || "").toLowerCase();
        const cls = col === "risk" ? `.r-${s}` : (col === "state" || col === "status") ? `.st-${s}` : "";
        return h("td" + cls, refText(s, putRef));
      })));
      return [h("div.tl.table", h("table.tt", h("thead", head), h("tbody", rows)))];
    }
    if (k === "img" && line.data) {
      const src = `data:${line.mime || "image/png"};base64,${line.data}`;
      const img = h("img", { src, alt: line.t || "image", onclick: () => modal(line.t || "Image", h("img", { src, style: { maxWidth: "100%" } }), [{ label: "Close" }]) });
      return [h("div.tl.img", img)];
    }
    if (k === "agent") return [h("div.tl.ag" + (line.role === "user" ? ".user" : ""), line.t)];
    // (a job log line is .lg: .log is the Studio's log panel)
    const cls = k === "log" ? "lg" : ["out", "err", "warn", "ok", "info"].includes(k) ? k : "out";
    return [h("div.tl." + cls, refText(line.t || "", putRef))];
  }

  function jobBox(b, id) {
    let j = b.jobs.get(id);
    if (j) return j;
    const bar_ = h("div.bar.indef", h("i"));
    const ti = h("span.ti", `job ${id}`);
    const stt = h("span.stt", "running");
    const step = h("span.step");
    const log = h("div.jlog");
    const el = h("div.tjob.running", h("div.jl", { title: "show / fold its log" }, stt, ti, bar_, step), log);
    el.querySelector(".jl").addEventListener("click", () => el.classList.toggle("folded"));
    j = { el, bar: bar_, ti, stt, step, log, state: "running" };
    b.jobs.set(id, j);
    b.el.append(el);
    return j;
  }

  function addLine(run, line, label, own) {
    if (line.k === "ctl") {
      if (line.action === "clear") { clearView(); return; }
      if (line.action === "end") {
        const b = st.blocks.get(run);
        st.active.delete(run);
        busy();
        if (b) {
          b.el.classList.remove("live");
          if (line.state !== "done") b.el.classList.add("bad");
          const cls = line.state === "done" ? "ok" : line.state === "cancelled" ? "cancel" : "bad";
          b.el.append(h("div.end." + cls, `${line.state}${line.exit ? " \u00b7 exit " + line.exit : ""} \u00b7 ${line.seconds}s`));
        }
        if (own) setPrompt(line.project, line.cwd);
        if (own && line.project !== undefined && st.spec) loadSpec().then((s) => { st.spec = s; }).catch(() => {});
      }
      return;
    }
    const b = block(run, label, own);
    if (line.k === "in") {
      clear(b.el);
      b.el.classList.add("live");
      st.active.add(run);
      busy();
      b.el.append(h("div.tl.in", own ? null : h("span.who", `[${label}]`),
        h("span.ps", "f2o77", line.prompt ? h("i", ` [${line.prompt}]`) : "", " \u203a"), line.t));
      scroll(own);
      return;
    }
    if (line.k === "job" && line.job) {
      const j = jobBox(b, line.job);
      j.ti.textContent = `${line.title || "job"} \u00b7 ${line.job}`;
      j.stt.textContent = line.state;
      j.el.className = "tjob " + line.state + (j.el.classList.contains("folded") ? " folded" : "");
      const pct = typeof line.p === "number" ? Math.round(line.p * 100) : null;
      j.bar.classList.toggle("indef", line.state === "running" && pct === null);
      j.bar.firstChild.style.width = line.state === "done" ? "100%" : pct !== null ? `${pct}%` : "";
      j.step.textContent = line.state === "running" ? (line.step || (pct !== null ? `${pct}%` : "")) : line.state === "failed" ? (line.t.split(" -- ").slice(1).join(" -- ")) : "";
      if (line.state === "done" && j.state !== "done") j.el.classList.add("folded");
      j.state = line.state;
      scroll();
      return;
    }
    if (line.k === "log" && line.job) {
      const j = jobBox(b, line.job);
      const atEnd = j.log.scrollHeight - j.log.scrollTop - j.log.clientHeight < 30;
      j.log.append(h("div.tl.lg", line.t));
      while (j.log.childNodes.length > 1500) j.log.firstChild.remove();
      if (atEnd) j.log.scrollTop = j.log.scrollHeight;
      scroll();
      return;
    }
    b.el.append(...lineEl(line));
    scroll();
  }

  function clearView() {
    clear(out);
    st.blocks.clear();
    welcome();
  }

  function welcome() {
    const keys = ["help", "tools", "status", "projects", "jobs", "agent", "routes"].map((c) =>
      h("button", { type: "button", onclick: () => run(c) }, c));
    out.append(h("div.term-welcome",
      h("div.ban", h("span", "fivem2open77"), " \u00b7 terminal"),
      h("div.sub", "Every Studio tool is a command \u00b7 Tab completes \u00b7 \u2191\u2193 history \u00b7 Ctrl+C stops \u00b7 agents' commands show here too"),
      h("div.keys", keys)));
  }

  // ---- live lines
  const off = store.on((ev) => {
    if (st.disposed) return;
    if (ev.type === "term") {
      const own = ev.session === st.sid;
      if (!own && !(showAgents.checked && ev.origin === "mcp")) return;
      addLine(ev.run, ev.line, ev.label || "agent", own);
    } else if (ev.type === "mcp" && showAgents.checked && ev.call && !["terminal", "terminal_output"].includes(ev.call.tool)) {
      // an agent's direct tool call (not through the terminal): its line and its answer
      const run = "mcp:" + ev.call.id;
      if (ev.kind === "call") {
        addLine(run, { k: "in", t: `${ev.call.tool} ${JSON.stringify(ev.call.args || {})}`.replace(/ \{\}$/, "") }, ev.label, false);
      } else if (ev.kind === "result") {
        addLine(run, { k: ev.call.error ? "err" : "info", t: (ev.call.text || "").slice(0, 600) + (ev.call.images ? ` (+${ev.call.images} image)` : "") }, ev.label, false);
        addLine(run, { k: "ctl", action: "end", state: ev.call.error ? "failed" : "done", exit: ev.call.error ? 1 : 0, seconds: ev.call.seconds }, ev.label, false);
      }
    } else if (ev.type === "job" && st.spec) {
      st.spec.jobs = [ev.job, ...(st.spec.jobs || []).filter((j) => j.id !== ev.job.id)].slice(0, 30);
    }
  });

  // ---- running a line
  async function run(line) {
    line = (line || "").trim();
    if (!line) return;
    if (line === "exit" && opts.onClose) { opts.onClose(); return; }
    if (!st.hist.length || st.hist[st.hist.length - 1] !== line) st.hist.push(line);
    st.hi = -1;
    st.draft = "";
    try {
      await api.post("/terminal/run", { session: st.sid, line });
    } catch (e) { toast(e.message, "err"); }
    scroll(true);
  }

  async function cancel() {
    const runs = [...st.active].filter((r) => !String(r).startsWith("mcp:") && st.blocks.get(r) && st.blocks.get(r).own);
    if (!runs.length) return false;
    const last = runs[runs.length - 1];
    try { await api.post(`/terminal/runs/${last}/cancel`); } catch (e) { toast(e.message, "err"); }
    return true;
  }

  // ---- completion
  function candidates(text, caret) {
    const spec = st.spec;
    if (!spec) return null;
    const before = text.slice(0, caret);
    const words = splitWords(before);
    const endsSpace = !before.length || /\s$/.test(before);
    const cur = endsSpace ? { text: "", start: caret, end: caret } : words[words.length - 1];
    const prev = endsSpace ? words : words.slice(0, -1);
    const tools = spec.tools || [];
    const bnames = (spec.builtins || []).map((b) => ({ value: b.name, hint: b.help }));
    const tnames = tools.map((t) => ({ value: t.name, hint: `${t.group} \u00b7 ${t.summary}` }));
    const projects = (spec.projects || []).map((p) => ({ value: /\s/.test(p.name) ? `"${p.name}"` : p.name, hint: `${p.kind} \u00b7 ${p.id}` }));
    const projIds = (spec.projects || []).map((p) => ({ value: p.id, hint: `${p.kind} \u00b7 ${p.name}` }));
    const servers = (spec.servers || []).map((s) => ({ value: s.id, hint: `${s.label || ""} ${(s.names || []).join(", ")}` }));
    const jobs = (spec.jobs || []).map((j) => ({ value: j.id, hint: `${j.state} \u00b7 ${j.title}` }));
    let pool = [];
    let word = cur.text;
    let prefix = "";
    if (!prev.length) pool = bnames.concat(tnames);
    else {
      const cmd = prev[0].text;
      const tool = tools.find((t) => t.name === cmd || t.name === cmd.replace(/-/g, "_"));
      if (tool) {
        const used = new Set(prev.slice(1).map((w) => w.text.split("=")[0].replace(/^--/, "")));
        const eq = word.indexOf("=");
        if (eq > 0) {
          const key = word.slice(0, eq);
          prefix = word.slice(0, eq + 1);
          word = word.slice(eq + 1);
          const p = tool.params.find((x) => x.name === key);
          if (p && p.enum) pool = p.enum.map((v) => ({ value: String(v), hint: "" }));
          else if (p && p.type.includes("boolean")) pool = [{ value: "true" }, { value: "false" }];
          else if (key === "project") pool = projIds.concat(projects);
          else if (key === "server") pool = servers;
          else if (key === "job") pool = jobs;
        } else {
          const req = tool.params.filter((p) => p.required && !used.has(p.name));
          const positional = prev.length - 1;
          if (req.length && positional < tool.params.filter((p) => p.required).length && !word.includes("=")) {
            const p = req[0];
            if (p.name === "server") pool = servers.slice();
            else if (p.name === "job") pool = jobs.slice();
            else if (p.name === "project") pool = projIds.concat(projects);
            else if (p.enum) pool = p.enum.map((v) => ({ value: String(v) }));
          }
          pool = pool.concat(tool.params.filter((p) => !used.has(p.name)).map((p) => ({
            value: `${p.name}=`, hint: `${p.required ? "needed \u00b7 " : ""}${p.enum ? p.enum.join("|") : p.type}${p.description ? " \u00b7 " + p.description : ""}`, keep: true })));
        }
      } else {
        const sub = prev[1] ? prev[1].text : null;
        switch (cmd) {
          case "help": case "man": case "?": pool = prev.length === 1 ? bnames.concat(tnames) : []; break;
          case "use": pool = prev.length === 1 ? projects.concat([{ value: "-", hint: "no project" }]) : []; break;
          case "log": case "job": case "wait": case "cancel": case "kill": pool = prev.length === 1 ? jobs : [{ value: "-f", hint: "follow" }]; break;
          case "ui":
            if (prev.length === 1) pool = (spec.pages || []).map((p) => ({ value: p })).concat([{ value: "project", hint: "a project's page" }]);
            else if (sub === "project" && prev.length === 2) pool = projects;
            else if (sub === "project" && prev.length === 3) pool = ["overview", "mapping", "handling", "sound", "build", "deploy", "agent"].map((v) => ({ value: v }));
            break;
          case "agent":
            if (prev.length === 1) pool = ["list", "start", "say", "approve", "decline", "stop", "attach", "log", "watch"].map((v) => ({ value: v }));
            else if (["attach", "log", "stop", "watch"].includes(sub)) pool = (spec.agents || []).map((a) => ({ value: a.id, hint: `${a.status} \u00b7 ${a.goal || ""}` }));
            else if (sub === "start") pool = [{ value: "--project" }, { value: "--autonomy" }, { value: "--provider" }];
            break;
          case "api":
            if (prev.length === 1) pool = ["GET", "POST", "PUT", "PATCH", "DELETE"].map((v) => ({ value: v })).concat(
              (spec.routes || []).filter((r) => r.method === "GET").map((r) => ({ value: r.path, hint: r.about })));
            else if (prev.length === 2) pool = (spec.routes || []).filter((r) => r.method === sub.toUpperCase()).map((r) => ({ value: r.path, hint: r.about }));
            break;
          case "cli": pool = prev.length === 1 ? (spec.cli || []).map((v) => ({ value: v })) : []; break;
          default: pool = [];
        }
      }
    }
    const low = word.toLowerCase();
    let items = pool.filter((c) => c.value.toLowerCase().startsWith(low));
    if (low) items = items.concat(pool.filter((c) => !c.value.toLowerCase().startsWith(low) && c.value.toLowerCase().includes(low)));
    const seen = new Set();
    items = items.filter((c) => (seen.has(c.value) ? false : seen.add(c.value)));
    return { start: cur.start + prefix.length, end: cur.end, prefix: word, items: items.slice(0, 80) };
  }

  function commonPrefix(list) {
    if (!list.length) return "";
    let p = list[0];
    for (const s of list) { while (!s.toLowerCase().startsWith(p.toLowerCase())) p = p.slice(0, -1); }
    return p;
  }

  function applyCandidate(c, cand) {
    const v = ta.value;
    const ins = cand.value + (cand.keep || cand.value.endsWith("=") ? "" : " ");
    ta.value = v.slice(0, c.start) + ins + v.slice(c.end);
    const pos = c.start + ins.length;
    ta.setSelectionRange(pos, pos);
    autosize();
  }

  function showComp(c, on) {
    clear(comp);
    st.comp = c && c.items.length > 1 ? { ...c, on } : null;
    if (!st.comp) { comp.classList.remove("on"); return; }
    c.items.forEach((it, i) => comp.append(h("div.c" + (i === on ? ".on" : ""), {
      onmousedown: (e) => { e.preventDefault(); applyCandidate(c, it); showComp(null); hint(); }
    }, h("span.v", it.value), it.hint ? h("span.hint", it.hint) : null)));
    comp.classList.add("on");
    const el = comp.children[on];
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  function tab(back) {
    if (st.comp && st.comp.items.length) {
      const n = st.comp.items.length;
      const on = ((st.comp.on ?? -1) + (back ? -1 : 1) + n) % n;
      const c = st.comp;
      // re-read the word being completed and put this candidate in its place
      const v = ta.value;
      const curEnd = ta.selectionStart;
      const start = c.start;
      ta.value = v.slice(0, start) + c.items[on].value + v.slice(curEnd);
      const pos = start + c.items[on].value.length;
      ta.setSelectionRange(pos, pos);
      st.comp = { ...c, end: pos, on };
      showComp(st.comp, on);
      autosize();
      return;
    }
    const c = candidates(ta.value, ta.selectionStart);
    if (!c || !c.items.length) return;
    // one value that fits wins over argument names (deploy_car p<Tab>: prod, not "pro")
    const starts = c.items.filter((i) => i.value.toLowerCase().startsWith(c.prefix.toLowerCase()));
    const values = starts.filter((i) => !i.keep);
    const pick = c.items.length === 1 ? c.items[0] : values.length === 1 && c.prefix ? values[0] : starts.length === 1 ? starts[0] : null;
    if (pick) { applyCandidate(c, pick); showComp(null); hint(); return; }
    const cp = commonPrefix(c.items.map((i) => i.value));
    if (cp.length > c.prefix.length) {
      const v = ta.value;
      ta.value = v.slice(0, c.start) + cp + v.slice(c.end);
      const pos = c.start + cp.length;
      ta.setSelectionRange(pos, pos);
      c.end = pos;
      autosize();
    }
    showComp(c, -1);
  }

  // the signature line: what the command takes, the argument being written lit
  function hint() {
    clear(sigline);
    clear(ghost);
    const spec = st.spec;
    const text = ta.value;
    if (!spec || !text.trim()) return;
    const words = splitWords(text);
    const cmd = words[0] ? words[0].text : "";
    const tool = (spec.tools || []).find((t) => t.name === cmd || t.name === cmd.replace(/-/g, "_"));
    const b = (spec.builtins || []).find((x) => x.name === cmd.toLowerCase());
    if (tool) {
      // the argument being written: key= under the caret, else the next required one by position
      const before = text.slice(0, ta.selectionStart);
      const bw = splitWords(before);
      const caretWord = /\s$/.test(before) ? null : bw[bw.length - 1];
      let key = caretWord && caretWord.text.includes("=") ? caretWord.text.split("=")[0].replace(/^--/, "") : null;
      if (!key && bw.length) {
        const positional = bw.slice(1).filter((w) => !w.text.includes("=") && !w.text.startsWith("--") && w !== caretWord).length;
        const named = new Set(bw.slice(1).map((w) => w.text.split("=")[0].replace(/^--/, "")));
        const req = tool.params.filter((p) => p.required && !named.has(p.name));
        if (req[positional]) key = req[positional].name;
      }
      const parts = [h("b", tool.name)];
      for (const p of tool.params) {
        const label = p.required ? `<${p.name}>` : `[${p.name}${p.enum ? "=" + p.enum.join("|") : ""}]`;
        parts.push(" ", p.name === key ? h("span.cur", label) : label);
      }
      const pk = key && tool.params.find((p) => p.name === key);
      parts.push(h("span.about", pk && pk.description ? pk.description : tool.summary));
      sigline.append(...parts);
    } else if (b) {
      sigline.append(h("b", b.usage), h("span.about", b.help));
    } else if (words.length === 1 && !/\s$/.test(text)) {
      const c = candidates(text, text.length);
      if (c && c.items.length) sigline.append(h("span.about", { style: { marginLeft: 0 } }, c.items.slice(0, 8).map((i) => i.value).join("  \u00b7  ")));
    }
    // the ghost: the rest of the one candidate that fits, after the caret at the end
    if (ta.selectionStart === text.length && !/\s$/.test(text)) {
      const c = candidates(text, text.length);
      if (c && c.items.length && c.items[0].value.toLowerCase().startsWith(c.prefix.toLowerCase()) && c.prefix.length) {
        ghost.append(h("b", text), c.items[0].value.slice(c.prefix.length));
      }
    }
  }

  function autosize() {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
  }

  ta.addEventListener("input", () => { autosize(); showComp(null); hint(); });
  ta.addEventListener("click", hint);
  ta.addEventListener("blur", () => setTimeout(() => showComp(null), 150));
  ta.addEventListener("keydown", async (e) => {
    if (e.key === "Tab") { e.preventDefault(); tab(e.shiftKey); hint(); return; }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (st.comp && st.comp.on >= 0) { showComp(null); ta.value += ta.value.endsWith(" ") || ta.value.endsWith("=") ? "" : " "; hint(); return; }
      const line = ta.value;
      ta.value = "";
      autosize();
      showComp(null);
      hint();
      run(line);
      return;
    }
    if (e.key === "Escape") { if (st.comp) showComp(null); else { ta.value = ""; autosize(); hint(); } return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c" && ta.selectionStart === ta.selectionEnd && !window.getSelection().toString()) {
      e.preventDefault();
      if (!(await cancel())) { ta.value = ""; autosize(); hint(); }
      return;
    }
    if (e.ctrlKey && e.key.toLowerCase() === "l") { e.preventDefault(); run("clear"); return; }
    if (e.ctrlKey && e.key.toLowerCase() === "u") { e.preventDefault(); ta.value = ta.value.slice(ta.selectionStart); ta.setSelectionRange(0, 0); autosize(); hint(); return; }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && st.comp) {
      e.preventDefault();
      tab(e.key === "ArrowUp");
      hint();
      return;
    }
    if (e.key === "ArrowUp" && !ta.value.slice(0, ta.selectionStart).includes("\n")) {
      if (!st.hist.length) return;
      e.preventDefault();
      if (st.hi === -1) { st.draft = ta.value; st.hi = st.hist.length; }
      st.hi = Math.max(0, st.hi - 1);
      ta.value = st.hist[st.hi];
      autosize();
      hint();
      return;
    }
    if (e.key === "ArrowDown" && st.hi !== -1 && !ta.value.slice(ta.selectionEnd).includes("\n")) {
      e.preventDefault();
      st.hi += 1;
      if (st.hi >= st.hist.length) { st.hi = -1; ta.value = st.draft; } else ta.value = st.hist[st.hi];
      autosize();
      hint();
      return;
    }
    if (e.key === "PageUp") { out.scrollTop -= out.clientHeight * 0.85; e.preventDefault(); }
    if (e.key === "PageDown") { out.scrollTop += out.clientHeight * 0.85; e.preventDefault(); }
  });

  // ---- start
  async function init() {
    let want = "";
    try { want = localStorage.getItem(SESSION_KEY) || ""; } catch (_) { /* private mode */ }
    const s = await api.post("/terminal/sessions", { id: want });
    st.sid = s.id;
    try { localStorage.setItem(SESSION_KEY, s.id); } catch (_) { /* private mode */ }
    setPrompt(s.project, s.cwd);
    const sb = await api.get(`/terminal/sessions/${s.id}?limit=1200`);
    if (!sb.lines.some((l) => l.k !== "ctl")) welcome();
    for (const line of sb.lines) addLine(line.run, line, "You", true);
    for (const r of sb.running || []) st.active.add(r);
    busy();
    scroll(true);
    try {
      st.spec = await loadSpec(true);
      st.hist = (st.spec.history || []).slice();
    } catch (e) { toast(e.message, "err"); }
    if (opts.initial) run(opts.initial);
  }
  init().catch((e) => out.append(h("div.tl.err", `the terminal could not start: ${e.message}`)));

  return {
    focus: () => ta.focus(),
    run,
    insert: (text) => { ta.value = text; autosize(); ta.focus(); ta.setSelectionRange(text.length, text.length); hint(); },
    dispose: () => { st.disposed = true; off(); },
  };
}
