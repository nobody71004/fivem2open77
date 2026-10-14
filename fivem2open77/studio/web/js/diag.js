// What goes wrong in the page goes to the Studio's log too (workspace/logs/studio-*.log): script
// errors, rejected promises, a lost graphics context, a shader the GPU would not compile, and the
// GPU itself once.  A problem on someone's machine is then in a file, not only in a console nobody
// opened.  Bounded: the same message once, at most 40 a session, each cut to 600 characters.
const sent = new Set();
let count = 0;

export function diag(kind, message, extra = {}) {
  try {
    const text = String(message || "").slice(0, 600);
    const key = `${kind}:${text}`;
    if (sent.has(key) || count >= 40) return;
    sent.add(key);
    count++;
    const body = JSON.stringify(Object.assign({ kind, message: text, page: location.hash.slice(0, 80) }, extra));
    fetch("/api/diag", { method: "POST", headers: { "Content-Type": "application/json" }, body, credentials: "same-origin", keepalive: true })
      .catch(() => { /* the server is gone: nothing to tell */ });
  } catch (_) { /* never let reporting break the page */ }
}

export function initDiag() {
  window.addEventListener("error", (e) => {
    const where = e.filename ? ` (${String(e.filename).replace(location.origin, "")}:${e.lineno}:${e.colno})` : "";
    diag("error", (e.message || (e.error && e.error.message) || "script error") + where);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    diag("rejection", r && r.stack ? r.stack.split("\n").slice(0, 3).join(" | ") : String(r));
  });
}

// the GPU and the page's scale, once per kind of view
export function diagGpu(gl, what) {
  try {
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    const name = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    diag("gpu", `${what}: ${name}`, { dpr: window.devicePixelRatio || 1, size: [innerWidth, innerHeight] });
  } catch (_) { /* no context */ }
}
