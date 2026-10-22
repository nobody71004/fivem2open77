// The real city up close, for the 3D city: around the point you look at, the game's own meshes --
// buildings, walls, props, signs, trees and their foliage -- with their materials' colours, and
// further out the game's own far-view stand-ins of whole buildings (see world/detail.py and
// world/geocache.py).  The Studio reads them out of the player's game the first time an area is
// looked at (a job; the layer fills in as meshes arrive) and keeps them.
//
// realCity(ctx) -> { tick(now, info), setEnabled(on), cut(), state(), dispose() }
//   ctx: { THREE, api, city (the group in Night City's frame), scene, poke, onStatus(text|null, frac) }
//   info: { x, y (game metres: where the view looks), range (metres from the camera to there) }
const GEO_V = 1;
const NEAR = 200, FAR = 900;
const ACTIVE_RANGE = 4200;          // the view nearer than this (metres) to what it looks at: the layer is on
const MOVE = 90;                    // ask again when the point moved this far
const POLL = 2500;                  // while the Studio reads the area
const MAX_FETCH = 6;
const KIND_GLASS = 1, KIND_LIGHT = 2, KIND_HIDDEN = 3;

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
  const hemi = new THREE.HemisphereLight(0xa9c2e6, 0x1b1f26, 1.1);
  const sun = new THREE.DirectionalLight(0xfff1e0, 1.6);
  sun.position.set(-0.45, 0.85, 0.35);
  scene.add(hemi, sun);
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.82, metalness: 0.04, fog: true });
  material.onBeforeCompile = (sh) => {
    sh.vertexShader = sh.vertexShader.replace("#include <common>", "#include <common>\nattribute float aGlow;\nvarying float vGlow;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvGlow = aGlow;");
    sh.fragmentShader = sh.fragmentShader.replace("#include <common>", "#include <common>\nvarying float vGlow;")
      .replace("#include <emissivemap_fragment>", "#include <emissivemap_fragment>\ntotalEmissiveRadiance += diffuseColor.rgb * vGlow;");
  };
  let enabled = true, disposed = false;
  let want = null;                  // { x, y } the view looks at
  let asked = null;                 // { x, y, at } of the last answer asked for
  let inflight = false;
  let reading = false;
  let lastAnswer = null;
  const geos = new Map();           // geo id -> Promise<{ header, pos, nrm, idx, shared }>
  const meshes = new Map();         // key (id|app) -> { mesh, count, sig }
  let fetching = 0;
  const queue = [];
  let center = null;                // what is drawn: { x, y, near }

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
    return geo;
  }

  function b64floats(s) {
    const bin = atob(s);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Float32Array(u8.buffer);
  }

  // columns x, y, z axis and translation (12 floats an instance) -> instanceMatrix (16, column-major)
  function fillMatrices(mesh, list) {
    const a = mesh.instanceMatrix.array;
    let i = 0;
    for (const f of list) {
      for (let k = 0; k < f.length; k += 12, i++) {
        const o = i * 16;
        a[o] = f[k]; a[o + 1] = f[k + 1]; a[o + 2] = f[k + 2]; a[o + 3] = 0;
        a[o + 4] = f[k + 3]; a[o + 5] = f[k + 4]; a[o + 6] = f[k + 5]; a[o + 7] = 0;
        a[o + 8] = f[k + 6]; a[o + 9] = f[k + 7]; a[o + 10] = f[k + 8]; a[o + 11] = 0;
        a[o + 12] = f[k + 9]; a[o + 13] = f[k + 10]; a[o + 14] = f[k + 11]; a[o + 15] = 1;
      }
    }
    mesh.instanceMatrix.needsUpdate = true;
  }

  async function apply(ans) {
    lastAnswer = ans;
    // the rings of one mesh and appearance draw as one
    const groups = new Map();
    for (const g of ans.groups || []) {
      if (!g.ready) continue;
      const key = `${g.id}|${g.app || ""}`;
      const e = groups.get(key) || { id: g.id, app: g.app || "", parts: [], count: 0, sig: "" };
      e.parts.push(b64floats(g.m));
      e.count += g.count;
      e.sig += `${g.ring}:${g.count};`;
      groups.set(key, e);
    }
    for (const [key, m] of meshes) {
      if (!groups.has(key)) { root.remove(m.mesh); m.mesh.geometry.dispose(); m.mesh.dispose(); meshes.delete(key); }
    }
    center = { x: ans.center[0], y: ans.center[1], near: ans.near };
    for (const [key, e] of groups) {
      const have = meshes.get(key);
      if (have && have.sig === e.sig && have.cx === ans.center[0] && have.cy === ans.center[1]) continue;
      let g;
      try { g = await fetchGeo(e.id); } catch (_) { continue; }
      if (disposed || lastAnswer !== ans) return;
      if (have) { root.remove(have.mesh); have.mesh.geometry.dispose(); have.mesh.dispose(); meshes.delete(key); }
      const geo = geometryFor(g, e.app);
      if (!geo) continue;
      const mesh = new THREE.InstancedMesh(geo, material, e.count);
      fillMatrices(mesh, e.parts);
      mesh.computeBoundingSphere();
      mesh.frustumCulled = true;
      mesh.renderOrder = 3;
      root.add(mesh);
      meshes.set(key, { mesh, sig: e.sig, cx: ans.center[0], cy: ans.center[1] });
      poke();
    }
  }

  function status() {
    if (!ctx.onStatus) return;
    const a = lastAnswer;
    if (!enabled || !a) return ctx.onStatus(null);
    if (a.state === "index") return ctx.onStatus("Real city: Night City's sector list is not read yet (World > Read it now)", null);
    if (a.state === "reading") {
      const t = a.meshes ? a.meshes.total : 0, r = a.meshes ? a.meshes.ready : 0;
      return ctx.onStatus(`Reading the real city here from the game… ${r.toLocaleString()} / ${t.toLocaleString()} meshes`, t ? r / t : 0);
    }
    return ctx.onStatus(null);
  }

  async function ask(x, y) {
    inflight = true;
    asked = { x, y, at: performance.now() };
    try {
      const ans = await api.get(`/city/detail?x=${x.toFixed(1)}&y=${y.toFixed(1)}&near=${NEAR}&far=${FAR}`);
      if (disposed) return;
      reading = ans.state === "reading";
      await apply(ans);
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
    want = { x: info.x, y: info.y };
    if (inflight) return;
    const moved = !asked || Math.hypot(want.x - asked.x, want.y - asked.y) > MOVE;
    const again = reading && now - asked.at > POLL;
    if (moved || again) ask(want.x, want.y);
  }

  function setEnabled(on) {
    enabled = !!on;
    root.visible = enabled;
    if (!enabled) { status(); ctx.onStatus && ctx.onStatus(null); } else asked = null;
    poke();
  }

  // where the map screen's own blocks give way to the real city (the city's shaders cut there)
  function cut() {
    if (!enabled || !root.visible || !center || !meshes.size) return null;
    return { x: center.x, y: center.y, r: center.near };
  }

  function state() {
    let instances = 0;
    for (const m of meshes.values()) instances += m.mesh.count;
    return { enabled, visible: root.visible, meshes: meshes.size, instances, reading, geos: geos.size,
      answer: lastAnswer ? { state: lastAnswer.state, groups: (lastAnswer.groups || []).length, meshes: lastAnswer.meshes } : null };
  }

  function dispose() {
    disposed = true;
    for (const m of meshes.values()) { m.mesh.geometry.dispose(); m.mesh.dispose(); }
    meshes.clear();
    city.remove(root);
    scene.remove(hemi, sun);
    material.dispose();
  }

  return { tick, setEnabled, cut, state, dispose };
}
