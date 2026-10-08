// JSON API client.  The session cookie (set by /auth) authenticates every call.

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.error ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
    this.errors = (body && body.errors) || [];
  }
}

async function call(method, path, body, opts = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (body instanceof Blob || body instanceof ArrayBuffer) {
    init.body = body;
    init.headers["Content-Type"] = "application/octet-stream";
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch("/api" + path, init);
  const ct = r.headers.get("Content-Type") || "";
  if (!r.ok) {
    let b = null;
    try { b = ct.includes("json") ? await r.json() : { error: await r.text() }; } catch (_) { /* empty */ }
    throw new ApiError(r.status, b);
  }
  if (opts.raw) return r;
  return ct.includes("json") ? r.json() : r.text();
}

export const api = {
  get: (p) => call("GET", p),
  post: (p, b = {}) => call("POST", p, b),
  put: (p, b) => call("PUT", p, b),
  patch: (p, b) => call("PATCH", p, b),
  del: (p) => call("DELETE", p),
  upload: (p, blob) => call("POST", p, blob),
};

// upload with progress (fetch has no upload progress)
export function uploadFile(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api" + path);
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let b = null;
      try { b = JSON.parse(x.responseText); } catch (_) { /* empty */ }
      if (x.status >= 200 && x.status < 300) resolve(b); else reject(new ApiError(x.status, b));
    };
    x.onerror = () => reject(new ApiError(0, { error: "upload failed" }));
    x.send(file);
  });
}

// wait for a job to end; onUpdate(detail) on each poll
export async function waitJob(id, onUpdate, interval = 700) {
  let since = 0;
  let failures = 0;
  const lines = [];
  for (;;) {
    let j;
    try {
      j = await api.get(`/jobs/${id}?since=${since}`);
      failures = 0;
    } catch (e) {
      // a busy or restarting server: keep polling a while before giving up
      if (++failures > 20 || (e.status && e.status !== 0 && e.status < 500 && e.status !== 429)) throw e;
      await new Promise((r) => setTimeout(r, Math.min(5000, interval * failures)));
      continue;
    }
    lines.push(...j.log);
    since = j.lines;
    j.allLines = lines;
    if (onUpdate) onUpdate(j);
    if (!["queued", "running"].includes(j.state)) return j;
    await new Promise((r) => setTimeout(r, interval));
  }
}
