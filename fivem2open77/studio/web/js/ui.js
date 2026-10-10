// Small DOM helpers: h() builds elements, toast()/modal()/confirmBox() for feedback.

export function h(tag, attrs, ...kids) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.className = classes.join(" ");
  if (attrs !== undefined && attrs !== null && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className += (el.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k in el && typeof v !== "string" && k !== "list") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false) continue;
    if (Array.isArray(k)) append(el, k);
    else el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

// el.append(...) without the null / false children (DOM append would print them as text)
export function put(el, ...kids) { append(el, kids); return el; }

export function toast(text, kind = "") {
  const t = h("div.toast" + (kind ? "." + kind : ""), text);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 9000 : 4500);
}

export function modal(title, body, actions = []) {
  const root = document.getElementById("modal-root");
  return new Promise((resolve) => {
    const close = (v) => { back.remove(); resolve(v); };
    const back = h("div.modal-back", { onclick: (e) => { if (e.target === back) close(null); } },
      h("div.modal", h("h2", title), body,
        h("div.actions", actions.map((a) => h("button.btn" + (a.kind ? "." + a.kind : ""),
          { onclick: () => close(a.value !== undefined ? a.value : a.label) }, a.label)))));
    root.append(back);
    const first = back.querySelector("input,textarea,select");
    if (first) first.focus();
  });
}

export async function confirmBox(title, text, okLabel = "OK", kind = "primary") {
  const r = await modal(title, h("p", text), [{ label: "Cancel", value: false }, { label: okLabel, value: true, kind }]);
  return r === true;
}

export async function prompt(title, label, value = "") {
  const input = h("input", { type: "text", value, style: { width: "100%" } });
  const r = await modal(title, h("label.field", label, input), [{ label: "Cancel", value: null }, { label: "OK", value: "ok", kind: "primary" }]);
  return r === "ok" ? input.value : null;
}

export function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KiB", "MiB", "GiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
              : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtAgo(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtTime(t);
}

export function pill(state) {
  const cls = { done: "ok", ok: "ok", running: "run", queued: "run", failed: "err", cancelled: "warn",
                waiting_user: "warn", waiting_approval: "warn", finished: "ok", stopped: "warn" }[state] || "";
  return h("span.pill" + (cls ? "." + cls : ""), (state || "").replace("_", " "));
}

export function logLines(lines) {
  return lines.map((l) => {
    const t = typeof l === "string" ? l : l.text;
    const cls = /error|failed|NOT started|!!/i.test(t) ? "e" : /warning|PENDING|skipped/i.test(t) ? "w" : /done|ok|started|STAGED|LIVE|passed/i.test(t) ? "g" : "";
    return h("div" + (cls ? "." + cls : ""), t);
  });
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// an icon from the sprite (img/icons.svg, put into the page at start)
const SVG = "http://www.w3.org/2000/svg";
export function icon(name, cls = "") {
  const s = document.createElementNS(SVG, "svg");
  s.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVG, "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
}

// the title bar's trail: [["Maps", "#/maps"], ["Cedar Crest"]]
export function setCrumbs(items) {
  const nav = document.getElementById("crumbs");
  if (!nav) return;
  clear(nav);
  (items || []).forEach(([label, href], i) => {
    if (i) nav.append(icon("chevron-right"));
    nav.append(href ? h("a", { href }, label) : h("span", label));
  });
}
