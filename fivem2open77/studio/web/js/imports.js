// Bringing FiveM resources in, whatever their size: dropped files and folders (their real paths in
// the desktop app: nothing is uploaded, a 6.9 GB city is read where it is), a typed path, or an
// upload in a plain browser; then the import job (copy, extract once, or link) and the job it
// starts (a map's first look, a car's inspection).
import { api, uploadFile, waitJob } from "./api.js";
import { h, clear, fmtBytes, logLines, pill, icon } from "./ui.js";

// ------------------------------------------------------------------ the desktop app's file paths
const bridge = window.chrome && window.chrome.webview && typeof window.chrome.webview.postMessageWithAdditionalObjects === "function"
  ? window.chrome.webview : null;
let seq = 0;
const waiting = new Map();
if (bridge) {
  bridge.addEventListener("message", (e) => {
    const d = e.data;
    if (d && d.type === "f2o77-paths" && waiting.has(d.id)) {
      const done = waiting.get(d.id);
      waiting.delete(d.id);
      done(Array.isArray(d.paths) ? d.paths : []);
    }
  });
}
export const desktop = !!bridge;

// What a drop holds, read during the drop event (the browser forgets it right after).
export function readDrop(dt) {
  const files = [...(dt.files || [])];
  const items = [...(dt.items || [])];
  const handles = items.map((i) => { try { return i.getAsFileSystemHandle ? i.getAsFileSystemHandle() : null; } catch (_) { return null; } })
    .filter(Boolean);
  let folder = null;
  for (const it of items) {
    const en = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
    if (en && en.isDirectory) { folder = en.name; break; }
  }
  return { files, handles, folder };
}

// The paths on this PC of a drop (the desktop app only; null in a browser).
export async function dropPaths(drop) {
  if (!bridge || !drop.files.length) return null;
  const ask = (objs) => new Promise((resolve) => {
    const id = ++seq;
    waiting.set(id, resolve);
    try {
      bridge.postMessageWithAdditionalObjects({ type: "f2o77-paths", id }, objs);
    } catch (_) {
      waiting.delete(id);
      resolve(null);
      return;
    }
    setTimeout(() => { if (waiting.has(id)) { waiting.delete(id); resolve(null); } }, 5000);
  });
  let paths = await ask(drop.files);
  if ((!paths || paths.length < drop.files.length) && drop.handles.length) {
    // a dropped folder: its file system handle knows where it is
    const hs = (await Promise.all(drop.handles.map((p) => p.catch(() => null)))).filter(Boolean);
    const more = hs.length ? await ask(hs) : null;
    if (more && more.length) paths = [...new Set([...(paths || []), ...more])];
  }
  return paths && paths.length ? paths : null;
}

// ------------------------------------------------------------------ a job's progress card
export function jobCard(title, help) {
  const head = h("b", title);
  const step = h("span.muted", "");
  const st = h("span", pill("running"));
  const bar = h("div.progress.indef", h("div"));
  const log = h("div.log", { style: { maxHeight: "150px" } });
  const extra = h("div");
  const el = h("div.card.job-card", h("div.row", head, step, h("div.grow"), st), help ? h("p.help", help) : null, bar,
    h("div", { style: { height: "8px" } }), log, extra);
  return {
    el, extra,
    title(t) { head.textContent = t; },
    update(j) {
      clear(log).append(...logLines((j.allLines || []).slice(-80)));
      log.scrollTop = log.scrollHeight;
      step.textContent = j.step || "";
      if (j.progress != null) { bar.className = "progress"; bar.firstChild.style.width = `${(j.progress * 100).toFixed(1)}%`; }
      else bar.className = "progress indef";
    },
    finish(state, text) {
      st.replaceChildren(pill(state));
      bar.className = "progress";
      bar.firstChild.style.width = state === "done" ? "100%" : bar.firstChild.style.width;
      step.textContent = text || "";
    },
  };
}

// follow a job in a card; resolves with the job's detail when it ends
export async function follow(card, jobId) {
  const j = await waitJob(jobId, (jj) => card.update(jj));
  card.update(j);
  return j;
}

// ------------------------------------------------------------------ the import form
// A drop zone with "choose a file" and a path field.  onSource({path} | {file}) is called once per
// import (a drop of several things imports the first).
export function importZone({ what, accept = ".zip,.rar,.7z", help, placeholder, onSource }) {
  const fileInput = h("input", { type: "file", accept, class: "hidden", onchange: () => {
    if (fileInput.files[0]) onSource({ file: fileInput.files[0] });
    fileInput.value = "";
  } });
  const pathInput = h("input", { type: "text", placeholder, style: { flex: 1, minWidth: "260px" } });
  const go = () => { const v = pathInput.value.trim().replace(/^"|"$/g, ""); if (v) onSource({ path: v }); };
  pathInput.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  const drop = h("div.dropzone",
    h("div.big", icon("upload")),
    h("div", h("b", `Drop ${what} here`), desktop ? " (a folder, .zip, .rar or .7z) or " : " (.zip, .rar, .7z) or ",
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); fileInput.click(); } }, "choose a file")),
    help ? h("div.help", { style: { marginTop: "6px" } }, help) : null,
    h("div.row", { style: { marginTop: "12px", justifyContent: "center" } }, pathInput,
      h("button.btn", { onclick: go }, "Import path")),
    fileInput);
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", async (e) => {
    const d = readDrop(e.dataTransfer);
    const paths = await dropPaths(d);
    if (paths && paths.length) return onSource({ path: paths[0] });
    if (d.folder) return onSource({ folder: d.folder });
    if (d.files[0]) onSource({ file: d.files[0] });
  });
  return drop;
}

// {path} | {file} -> the body for POST /projects or /packs (an upload first for a file)
export async function sourceBody(src, card) {
  if (src.folder) {
    throw new Error(`${src.folder} is a folder: a browser cannot upload a folder. Type its path in the field ` +
      "(e.g. C:\\Users\\me\\Downloads\\" + src.folder + "), or open the Studio's own window, where folders can be dropped.");
  }
  if (src.path) return { path: src.path };
  const f = src.file;
  card.title(`Uploading ${f.name} (${fmtBytes(f.size)})`);
  if (f.size > 4 * 1024 ** 3) {
    card.extra.append(h("p.help", "A big file: typing its path in the field instead reads it where it is, without the upload."));
  }
  const up = await uploadFile(`/uploads?name=${encodeURIComponent(f.name)}`, f, (p) => card.update({ progress: p, step: `${(p * 100).toFixed(0)}%` }));
  return { upload_id: up.upload_id };
}
