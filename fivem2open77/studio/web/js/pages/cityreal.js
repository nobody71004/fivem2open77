// The real city up close, for the 3D city: around the point you look at, the game's own meshes --
// buildings, walls, props, signs, trees and their foliage -- with their materials' colours, and
// further out the game's own far-view stand-ins of whole buildings (see world/detail.py and
// world/geocache.py).  The Studio reads them out of the player's game the first time an area is
// looked at (a job; the layer fills in as meshes arrive) and keeps them.
//
// Like the game, it draws a thing only while it is big enough to see: a mesh shows while the camera
// is nearer than SIZE_K times its size (a can a few metres, a wall hundreds), within a triangle
// budget that shrinks when frames get slow.  The Studio leaves out what the camera could not see
// from where it is anyway (it is told where the camera is), so answers stay small.
//
// realCity(ctx) -> { tick(now, info), setEnabled(on), setRange(name), setQuality(q), adapt(ms),
//                    cut(), state(), dispose() }
//   ctx: { THREE, api, city (the group in Night City's frame), scene, poke, onStatus(text|null, frac),
//          range: "close" | "normal" | "wide" (RANGES: metres of the near ring and the far one),
//          quality: "high" | "low" }
//   info: { x, y (game metres: where the view looks), range (metres from the camera to there),
//           cam: [x, y, z] (the camera, game metres) }
const GEO_V = 1;
export const RANGES = { close: [140, 600], normal: [200, 900], wide: [300, 1400] };
const QUALITY = { high: { k: 120, tris: 6e6 }, low: { k: 70, tris: 2.5e6 } };
const MIN_SHOW = 6;                 // metres: anything this near shows
const ACTIVE_RANGE = 4200;          // the view nearer than this (metres) to what it looks at: the layer is on
const MOVE = 90;                    // ask again when the point moved this far
const POLL = 2500;                  // while the Studio reads the area
const CULL_EVERY = 110;             // ms between culls while the camera moves
const MAX_FETCH = 6;
const KIND_GLASS = 1, KIND_LIGHT = 2, KIND_HIDDEN = 3;
const BINS = 64;

export function parseGeo(buf) {
  const dv = new DataView(buf);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== "F2GE") throw new Error("not a .geo file");
  const hlen = dv.getUint32(8, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, hlen)));
  let off = 12 + hlen;
  const n = header.vertices;
  const pos = new Float32Array(buf, off, n * 3);
  off += n * 12;
  const nrm = new Int8Array(buf, off, n * 3);
  off += n * 3;
  off += (4 - ((n * 3) % 4)) % 4;
  const m = header.triangles * 3;
  const idx = header.index === "u32" ? new Uint32Array(buf, off, m) : new Uint16Array(buf, off, m);
  return { header, pos, nrm, idx };
}

export function realCity(ctx) {
  const { THREE, api, city, scene, poke } = ctx;
  const root = new THREE.Group();
  root.name = "real-city";
  city.add(root);
  // light for real materials (the city's own shaders light themselves)
  const hemi = new THREE.HemisphereLight(0xb4c8e8, 0x2a2e36, 2.3);
  const sun = new THREE.DirectionalLight(0xffeedd, 2.4);
  sun.position.set(-0.45, 0.85, 0.35);
  scene.add(hemi, sun);
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.82, metalness: 0.04, fog: true });
  material.onBeforeCompile = (sh) => {
    sh.vertexShader = sh.vertexShader.replace("#include <common>", "#include <common>\nattribute float aGlow;\nvarying float vGlow;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvGlow = aGlow;");
    sh.fragmentShader = sh.fragmentShader.replace("#include <common>", "#include <common>\nvarying float vGlow;")
      .replace("#include <emissivemap_fragment>", "#include <emissivemap_fragment>\ntotalEmissiveRadiance += diffuseColor.rgb * (vGlow + 0.05);");
  };
  let enabled = true, disposed = false;
  let [NEAR, FAR] = RANGES[ctx.range] || RANGES.normal;
  let Q = QUALITY[ctx.quality] || QUALITY.high;
  let load = 1;                     // the triangle budget's share (adapt: slow frames shrink it)
  let asked = null;                 // { x, y, cam, reach, at } of the last answer asked for
  let inflight = false;
  let reading = false;
  let lastAnswer = null;
  const geos = new Map();           // geo id -> Promise<{ header, pos, nrm, idx, shared }>
  const meshes = new Map();         // key (id|app) -> entry (see place())
  let fetching = 0;
  const queue = [];
  let center = null;                // what is drawn: { x, y, near }
  let cam = null;                   // the camera (game metres) at the last cull
  let culledAt = 0, cullDirty = true;
  let kNow = Q.k, trisNow = 0, shownNow = 0;

  function fetchGeo(id) {
    if (geos.has(id)) return geos.get(id);
    const p = new Promise((resolve, reject) => queue.push({ id, resolve, reject }));
    geos.set(id, p);
    pump();
    p.catch(() => geos.delete(id));
    return p;
  }
  function pump() {
    while (fetching < MAX_FETCH && queue.length) {
      const job = queue.shift();
      fetching++;
      fetch(`/api/city/geo/${job.id}?v=${GEO_V}`, { credentials: "same-origin", headers: api.headers ? api.headers() : {} })
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.arrayBuffer(); })
        .then((buf) => {
          const g = parseGeo(buf);
          g.shared = {
            position: new THREE.BufferAttribute(new Float32Array(g.pos), 3),
            normal: new THREE.BufferAttribute(new Int8Array(g.nrm), 3, true),
          };
          job.resolve(g);
        })
        .catch(job.reject)
        .finally(() => { fetching--; pump(); });
    }
  }

  function geometryFor(g, app) {
    const h = g.header;
    const cols = h.apps[app] || h.apps[h.default] || Object.values(h.apps)[0] || [];
    const n = h.vertices;
    const color = new Float32Array(n * 3), glow = new Float32Array(n);
    const keep = [];
    for (const c of h.chunks) {
      const k = cols[c.chunk] || [0.36, 0.36, 0.37, 0];
      if (k[3] === KIND_HIDDEN) continue;
      keep.push(c);
      const gl = k[3] === KIND_LIGHT ? 2.2 : k[3] === KIND_GLASS ? 0.18 : 0;
      for (let v = c.v[0]; v < c.v[0] + c.v[1]; v++) {
        color[v * 3] = k[0]; color[v * 3 + 1] = k[1]; color[v * 3 + 2] = k[2];
        glow[v] = gl;
      }
    }
    if (!keep.length) return null;
    let index = g.idx;
    if (keep.length !== h.chunks.length) {
      const total = keep.reduce((a, c) => a + c.i[1], 0);
      index = g.idx instanceof Uint32Array ? new Uint32Array(total) : new Uint16Array(total);
      let o = 0;
      for (const c of keep) { index.set(g.idx.subarray(c.i[0], c.i[0] + c.i[1]), o); o += c.i[1]; }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", g.shared.position);
    geo.setAttribute("normal", g.shared.normal);
    geo.setAttribute("color", new THREE.BufferAttribute(color, 3));
    geo.setAttribute("aGlow", new THREE.BufferAttribute(glow, 1));
    geo.setIndex(new THREE.BufferAttribute(index instanceof Uint32Array ? new Uint32Array(index) : new Uint16Array(index), 1));
    geo.computeBoundingSphere();
    geo.userData.tris = index.length / 3;
    return geo;
  }

  function b64floats(s) {
    const bin = atob(s);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Float32Array(u8.buffer);
  }

  // an entry: one mesh and appearance, every instance the Studio sent (12 floats each: x, y, z axis,
  // translation), where each stands and how big it is; the InstancedMesh holds those shown
  function place(key, geo, list) {
    let n = 0;
    for (const f of list) n += f.length / 12;
    const f = new Float32Array(n * 12);
    let o = 0;
    for (const a of list) { f.set(a, o); o += a.length; }
    const bs = geo.boundingSphere, c = bs.center, r0 = bs.radius;
    const px = new Float32Array(n), py = new Float32Array(n), pz = new Float32Array(n), rad = new Float32Array(n);
    for (let i = 0, k = 0; i < n; i++, k += 12) {
      px[i] = f[k + 9] + f[k] * c.x + f[k + 3] * c.y + f[k + 6] * c.z;
      py[i] = f[k + 10] + f[k + 1] * c.x + f[k + 4] * c.y + f[k + 7] * c.z;
      pz[i] = f[k + 11] + f[k + 2] * c.x + f[k + 5] * c.y + f[k + 8] * c.z;
      const sx = f[k] * f[k] + f[k + 1] * f[k + 1] + f[k + 2] * f[k + 2];
      const sy = f[k + 3] * f[k + 3] + f[k + 4] * f[k + 4] + f[k + 5] * f[k + 5];
      const sz = f[k + 6] * f[k + 6] + f[k + 7] * f[k + 7] + f[k + 8] * f[k + 8];
      rad[i] = Math.max(0.05, r0 * Math.sqrt(Math.max(sx, sy, sz)));
    }
    let e = meshes.get(key);
    if (!e || e.cap < n) {
      if (e) { root.remove(e.mesh); e.mesh.dispose(); }
      const cap = e ? Math.ceil(n * 1.5) : n;
      const mesh = new THREE.InstancedMesh(geo, material, cap);
      mesh.frustumCulled = true;
      mesh.renderOrder = 3;
      root.add(mesh);
      e = { mesh, cap };
      meshes.set(key, e);
    }
    Object.assign(e, { f, n, px, py, pz, rad, ratio: new Float32Array(n), tris: geo.userData.tris || 0, shown: -1, sig: "" });
    // the bounds: every instance (the frustum test; a cull only shows fewer)
    writeAll(e);
    e.mesh.computeBoundingSphere();
    e.mesh.count = 0;
    e.mesh.visible = false;
    cullDirty = true;
    return e;
  }
  function writeAll(e) {
    e.mesh.count = e.n;
    const a = e.mesh.instanceMatrix.array;
    for (let i = 0; i < e.n; i++) put(a, i, e.f, i);
  }
  function put(a, j, f, i) {
    const o = j * 16, k = i * 12;
    a[o] = f[k]; a[o + 1] = f[k + 1]; a[o + 2] = f[k + 2]; a[o + 3] = 0;
    a[o + 4] = f[k + 3]; a[o + 5] = f[k + 4]; a[o + 6] = f[k + 5]; a[o + 7] = 0;
    a[o + 8] = f[k + 6]; a[o + 9] = f[k + 7]; a[o + 10] = f[k + 8]; a[o + 11] = 0;
    a[o + 12] = f[k + 9]; a[o + 13] = f[k + 10]; a[o + 14] = f[k + 11]; a[o + 15] = 1;
  }

  // what shows from where the camera is: nearer than k times its size, k the most that keeps the
  // triangles within the budget (the smallest things go first)
  function cull() {
    if (!cam) return;
    const K = Q.k, budget = Q.tris * load;
    const hist = new Float64Array(BINS);
    let always = 0;
    const [cx, cy, cz] = cam;
    for (const e of meshes.values()) {
      if (!e.n) continue;
      const { px, py, pz, rad, ratio, tris } = e;
      for (let i = 0; i < e.n; i++) {
        const dx = px[i] - cx, dy = py[i] - cy, dz = pz[i] - cz;
        const d = Math.sqrt(dx * dx + dy * dy + dz * dz) - rad[i];
        if (d < MIN_SHOW) { ratio[i] = 0; always += tris; continue; }
        const q = d / rad[i];
        ratio[i] = q;
        if (q < K) hist[Math.min(BINS - 1, Math.floor((q / K) * BINS))] += tris;
      }
    }
    let k = K, sum = always;
    for (let b = 0; b < BINS; b++) {
      if (sum + hist[b] > budget) { k = Math.max(K * 0.15, (K * b) / BINS); break; }
      sum += hist[b];
    }
    kNow = k;
    let tris = 0, shown = 0, changed = false;
    for (const e of meshes.values()) {
      const a = e.mesh.instanceMatrix.array, { ratio } = e;
      let j = 0, sig = 0;
      for (let i = 0; i < e.n; i++) {
        if (ratio[i] < k) { put(a, j, e.f, i); j++; sig = (sig * 31 + i) | 0; }
      }
      const s = `${j}:${sig}`;
      if (s !== e.sig) {
        e.sig = s;
        e.mesh.count = j;
        e.mesh.visible = j > 0;
        e.mesh.instanceMatrix.needsUpdate = true;
        changed = true;
      }
      tris += j * e.tris;
      shown += j;
    }
    trisNow = tris;
    shownNow = shown;
    if (changed) poke();
  }

  function apply(ans) {
    lastAnswer = ans;
    // the rings of one mesh and appearance draw as one
    const groups = new Map();
    for (const g of ans.groups || []) {
      if (!g.ready || !g.m) continue;
      const key = `${g.id}|${g.app || ""}`;
      const e = groups.get(key) || { id: g.id, app: g.app || "", m: [], h: [] };
      e.m.push(g.m);
      e.h.push(g.h != null ? g.h : g.m.length);
      groups.set(key, e);
    }
    for (const [key, m] of meshes) {
      if (!groups.has(key)) { root.remove(m.mesh); m.mesh.dispose(); meshes.delete(key); cullDirty = true; }
    }
    center = { x: ans.center[0], y: ans.center[1], near: ans.near };
    for (const [key, e] of groups) {
      const data = e.h.join(",");
      const have = meshes.get(key);
      if (have && have.data === data) continue;          // the same instances: nothing to redo
      fetchGeo(e.id).then((g) => {
        if (disposed || lastAnswer !== ans) return;       // a newer answer takes over
        let geo = geoms.get(key);
        if (geo === undefined) { geo = geometryFor(g, e.app); geoms.set(key, geo); }
        if (!geo) return;
        const old = meshes.get(key);
        if (old && old.data === data) return;
        const ent = place(key, geo, e.m.map(b64floats));
        ent.data = data;
        poke();
      }).catch(() => { /* a mesh the Studio could not serve: left out */ });
    }
  }
  const geoms = new Map();          // id|app -> BufferGeometry (kept while the layer lives)

  function status() {
    if (!ctx.onStatus) return;
    const a = lastAnswer;
    if (!enabled || !a) return ctx.onStatus(null);
    if (a.state === "index") return ctx.onStatus("Real city: Night City's sector list is not read yet (World > Read it now)", null);
    if (a.state === "reading") {
      const t = a.meshes ? a.meshes.total : 0, r = a.meshes ? a.meshes.ready + (a.meshes.failed || 0) : 0;
      return ctx.onStatus(`Reading the real city here from the game… ${r.toLocaleString()} / ${t.toLocaleString()} meshes`, t ? r / t : 0);
    }
    return ctx.onStatus(null);
  }

  // how far the camera may go before the Studio is asked again (it sends what could show that far)
  const reachFor = (range) => Math.min(300, Math.max(30, range * 0.25));

  async function ask(x, y, c, reach) {
    inflight = true;
    asked = { x, y, cam: c ? c.slice() : null, reach, at: performance.now() };
    const camq = c ? `&cx=${c[0].toFixed(1)}&cy=${c[1].toFixed(1)}&cz=${c[2].toFixed(1)}&k=${Q.k}&reach=${reach.toFixed(0)}` : "";
    try {
      const ans = await api.get(`/city/detail?x=${x.toFixed(1)}&y=${y.toFixed(1)}&near=${NEAR}&far=${FAR}${camq}`);
      if (disposed) return;
      reading = ans.state === "reading";
      apply(ans);
    } catch (e) {
      reading = false;
      lastAnswer = { state: "error", error: e.message };
    } finally {
      inflight = false;
      status();
    }
  }

  function tick(now, info) {
    if (disposed || !enabled || !info) return;
    const active = info.range < ACTIVE_RANGE;
    root.visible = active;
    if (!active) { ctx.onStatus && ctx.onStatus(null); return; }
    const c = info.cam || null;
    // the camera moved: cull again (now and then)
    if (c && (cullDirty || !cam || Math.hypot(c[0] - cam[0], c[1] - cam[1], c[2] - cam[2]) > Math.max(1.5, info.range * 0.01))) {
      if (now - culledAt > CULL_EVERY || cullDirty) {
        cam = c.slice();
        culledAt = now;
        cullDirty = false;
        cull();
      }
    }
    if (inflight) return;
    const reach = reachFor(info.range);
    const moved = !asked || Math.hypot(info.x - asked.x, info.y - asked.y) > MOVE
      || (c && asked.cam && Math.hypot(c[0] - asked.cam[0], c[1] - asked.cam[1], c[2] - asked.cam[2]) > asked.reach * 0.5)
      || (c && asked.reach && (reach < asked.reach * 0.6 || reach > asked.reach * 1.8));
    const again = reading && now - asked.at > POLL;
    if (moved || again) ask(info.x, info.y, c, reach);
  }

  function setRange(r) {
    const v = RANGES[r] || RANGES.normal;
    if (v[0] === NEAR && v[1] === FAR) return;
    [NEAR, FAR] = v;
    asked = null;
  }

  function setQuality(q) {
    const v = QUALITY[q] || QUALITY.high;
    if (v === Q) return;
    Q = v;
    asked = null;
    cullDirty = true;
  }

  // frame times (ms, averaged): slow frames shrink the real city's triangle budget, quick ones grow it back
  function adapt(ms) {
    const before = load;
    if (ms > 30 && trisNow > 2e5) load = Math.max(0.3, load * 0.8);
    else if (ms < 17) load = Math.min(1, load * 1.1);
    if (load !== before) cullDirty = true;
  }

  function setEnabled(on) {
    enabled = !!on;
    root.visible = enabled;
    if (!enabled) { status(); ctx.onStatus && ctx.onStatus(null); } else asked = null;
    setTimeout(() => { if (!disposed) poke(); }, 0);      // (called while the viewer is still being set up)
  }

  // where the map screen's own blocks give way to the real city (the city's shaders cut there):
  // once most of the near ring is read (before that, both show)
  function cut() {
    if (!enabled || !root.visible || !center || !meshes.size || !lastAnswer || !lastAnswer.meshes) return null;
    const m = lastAnswer.meshes;
    const [r, t] = m.near || [m.ready + (m.failed || 0), m.total];
    if (t && r < 0.6 * t) return null;
    return { x: center.x, y: center.y, r: center.near };
  }

  function state() {
    let instances = 0;
    for (const m of meshes.values()) instances += m.n || 0;
    return { enabled, visible: root.visible, meshes: meshes.size, instances, shown: shownNow, triangles: trisNow,
      k: Math.round(kNow), load: +load.toFixed(2), reading, geos: geos.size, near: NEAR, far: FAR,
      answer: lastAnswer ? { state: lastAnswer.state, groups: (lastAnswer.groups || []).length, meshes: lastAnswer.meshes,
        culled: lastAnswer.culled, dropped: lastAnswer.dropped } : null };
  }

  function dispose() {
    disposed = true;
    for (const m of meshes.values()) m.mesh.dispose();
    meshes.clear();
    for (const g of geoms.values()) if (g) g.dispose();
    geoms.clear();
    city.remove(root);
    scene.remove(hemi, sun);
    material.dispose();
  }

  return { tick, setEnabled, setRange, setQuality, adapt, cut, state, dispose };
}
