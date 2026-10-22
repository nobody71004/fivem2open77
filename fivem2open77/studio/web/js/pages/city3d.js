// Night City in 3D, for Live maps: the game's own world-map model (read once from the player's
// game into the Studio's cache, see world/citymap.py) -- terrain, streets, NCART lines, water,
// landmarks, the map screen's ~255,000 building blocks and the districts -- with the live maps,
// their spots and the players on it.  It looks like the game's map screen (or the Studio's neon
// look), from the angles the map screen has (top, map, iso, low; turned and through a long or a
// wide lens).  Orbit it like a map, fly through it, or walk the streets.
//
// cityStage(host, opts) -> { el, update(scene), dispose() }
//   scene: { focus, items: [{ key, name, role, anchor: {x, y, z, yaw}, bounds: [[x0, y0, z0], [x1, y1, z1]], note }],
//            players: [{ key, name, x, y, z }] }
//   opts.onPick({x, y, z})       "Set the new spot" tool: a click on the city
//   opts.onNudge(body)           arrows / PgUp PgDn / Q E in orbit mode (like the plan view)
import * as THREE from "../../vendor/three/three.module.min.js";
import { OrbitControls } from "../../vendor/three/OrbitControls.js";
import { EffectComposer } from "../../vendor/three/EffectComposer.js";
import { RenderPass } from "../../vendor/three/RenderPass.js";
import { ShaderPass } from "../../vendor/three/ShaderPass.js";
import { UnrealBloomPass } from "../../vendor/three/UnrealBloomPass.js";
import { OutputPass } from "../../vendor/three/OutputPass.js";
import { api, waitJob } from "../api.js";
import { h, clear, toast } from "../ui.js";
import { loadCity, view, TriGrid, BoxSet, insidePoly, polyCentroid } from "./citydata.js";
import { realCity } from "./cityreal.js";
import * as S from "./cityshaders.js";
import { diag, diagGpu } from "../diag.js";

const ROLE = {
  now: { color: 0x41f08f, tag: "NOW" },
  new: { color: 0xffd23f, tag: "NEW SPOT" },
  drag: { color: 0xffd23f, tag: "DROP TO SET" },
  previous: { color: 0x8b95ab, tag: "PREVIOUS" },
  other: { color: 0x5e9cff, tag: "MAP" },
  preset: { color: 0xb38cff, tag: "SAVED" },
  ghost: { color: 0x9ef3ed, tag: "CLICK TO SET" },
};
const PLAYER = 0x47d4ff;
const DISTRICT_COLORS = {
  watson: 0xf7d046, westbrook: 0xff4fa3, city_center: 0x34e7ff, heywood: 0x7dff6b, santo_domingo: 0xff9a3c,
  pacifica: 0xa77bff, dogtown: 0xff3b4e, morro_rock: 0x9fb4c9,
};
const BADLANDS = 0x3fb6c9;
const MODES = { orbit: "Orbit", fly: "Fly", walk: "Walk" };
const HELP = {
  orbit: [["drag", "turn"], ["right drag", "pan"], ["wheel / + -", "zoom"], ["WASD", "move"], ["T M I L", "top / map / iso / low"], [", .", "turn"], ["N", "north up"],
    ["double-click", "go there"], ["G", "go to…"], ["Backspace", "back"], ["Home", "whole city"], ["2 / 3", "fly / walk"]],
  fly: [["click", "mouse look"], ["WASD", "fly"], ["Space / E", "up"], ["C / Q", "down"], ["Shift", "fast"], ["Alt", "slow"], ["wheel", "speed"], ["Esc", "free the mouse"]],
  walk: [["click", "mouse look"], ["WASD", "walk"], ["Shift", "run"], ["Space", "jump (twice: double)"], ["2", "fly from here"], ["Esc", "free the mouse"]],
};
const PREFS_KEY = "f2o77.city3d";
const EYE = 1.72, RADIUS = 0.35, STEP = 0.75, GRAVITY = 19.6;

const LOOKS = { game: "The game's map screen", neon: "Neon" };
// the map screen's angles: pitch above the horizon (degrees); iso also turns the view
const VIEWS = { top: { label: "Top", pitch: 89.4, key: "T" }, map: { label: "Map", pitch: 58, key: "M" },
  iso: { label: "Iso", pitch: 35.3, yaw: 45, key: "I" }, low: { label: "Low", pitch: 14, key: "L" } };
const LENSES = { 75: "Wide lens", 55: "Normal lens", 30: "Long lens", 12: "Flat (no perspective)" };

function loadPrefs() {
  const d = { buildings: true, real: true, realRange: "normal", roads: true, metro: true, districts: true, labels: true, grid: false, glow: true, holo: false,
    tint: false, quality: "high", mode: "orbit", speed: 70, look: "game", lens: 55, autotilt: true };
  try { return Object.assign(d, JSON.parse(localStorage.getItem(PREFS_KEY) || "{}")); } catch (_) { return d; }
}
function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch (_) { /* private mode */ } }

// game (x east, y north, z up) <-> three.js (y up)
const g2t = (x, y, z, v = new THREE.Vector3()) => v.set(x, z, -y);
const t2g = (v) => [v.x, -v.z, v.y];
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export function cityStage(host, opts = {}) {
  const prefs = loadPrefs();
  const el = h("div.c3", { tabIndex: 0 });
  const veil = h("div.c3-veil");
  el.append(veil);
  host.append(el);
  let viewer = null;
  let disposed = false;
  let lastScene = null;

  async function boot() {
    let st;
    try {
      st = await api.get("/citymap");
    } catch (e) {
      return showVeil("error", e.message);
    }
    if (disposed) return;
    if (!st.ready) return showBuild(st);
    showVeil("loading", "Loading Night City", 0);
    try {
      const data = await loadCity(st.key, (f) => showVeil("loading", "Loading Night City", f));
      if (disposed) return;
      showVeil("loading", "Raising the city", 1);
      await new Promise((r) => setTimeout(r, 30));
      viewer = createViewer(el, data, prefs, Object.assign({}, opts, { onRebuild: () => stage.rebuild(), onLost: contextLost, upgrade: !!st.upgrade }));
      if (lastScene) viewer.update(lastScene);
      if (st.stale) toast("The game was updated since the 3D city was built: rebuild it from the city's menu if anything looks off");
      veil.classList.add("gone");
      setTimeout(() => { if (!disposed) clear(veil); }, 600);
    } catch (e) {
      console.error(e);
      showVeil("error", `The 3D city did not load: ${e.message}`);
    }
  }

  function showVeil(kind, text, frac) {
    veil.classList.remove("gone");
    clear(veil).append(h("div.c3-veil-in",
      h("div.c3-glyph", h("i"), h("i"), h("i")),
      h("div.c3-veil-title", kind === "error" ? "3D CITY OFFLINE" : "NIGHT CITY // 3D"),
      h("div.c3-veil-text", text),
      kind === "loading" ? h("div.c3-bar", h("i", { style: { width: `${Math.round((frac || 0) * 100)}%` } })) : null,
      kind === "error" ? h("button.btn.small", { onclick: boot }, "Try again") : null));
  }

  function showBuild(st) {
    const log = h("div.c3-veil-log");
    const bar = h("div.c3-bar", h("i", { style: { width: "0%" } }));
    const btn = h("button.btn.primary", { onclick: () => build(false) }, "Build the 3D city");
    const can = st.game && st.wolvenkit;
    clear(veil).append(h("div.c3-veil-in",
      h("div.c3-glyph", h("i"), h("i"), h("i")),
      h("div.c3-veil-title", "NIGHT CITY // 3D"),
      h("div.c3-veil-text", "The Studio reads the game's own 3D map -- the one on the map screen: terrain, streets, NCART lines, ",
        "its ~255,000 building blocks and the districts -- out of your Cyberpunk 2077 install, once (about a minute), and keeps it here. ",
        "Nothing is uploaded."),
      can ? btn : h("div.c3-veil-text.warn", !st.game ? "Cyberpunk 2077 was not found: set its folder in Settings > Tools." : "WolvenKit CLI was not found: set it up in Settings > Tools.",
        " ", h("a", { href: "#/settings" }, "Open Settings")),
      bar, log));
    bar.style.visibility = "hidden";
    async function build(force) {
      btn.disabled = true;
      bar.style.visibility = "visible";
      try {
        const job = await api.post("/citymap/build", { force });
        const j = await waitJob(job.id, (d) => {
          bar.firstChild.style.width = `${Math.round((d.progress || 0) * 100)}%`;
          if (d.step) log.textContent = d.step;
        });
        if (j.state !== "done") throw new Error(j.error || j.state);
        boot();
      } catch (e) {
        btn.disabled = false;
        log.textContent = `Failed: ${e.message}`;
        log.classList.add("warn");
      }
    }
    if (st.job) build(false);
  }

  // the graphics driver took the context away (a reset, a GPU switch, too many contexts): bring the
  // view back on a new one; three losses within a minute stop it with a way out
  let losses = [];
  function contextLost() {
    if (disposed) return;
    if (viewer) { viewer.dispose(); viewer = null; }
    const now = Date.now();
    losses = losses.filter((t) => now - t < 60000).concat(now);
    if (losses.length >= 3) {
      return showVeil("error", "The graphics driver keeps resetting the 3D view. Choose Fast in the layers menu (or turn Glow off) once it is back, "
        + "and check that the graphics driver is up to date.");
    }
    showVeil("loading", "The graphics driver reset the 3D view: bringing it back", 1);
    setTimeout(() => { if (!disposed && !viewer) boot(); }, 1200);
  }

  boot();
  const stage = {
    el,
    update(scene) { lastScene = scene; if (viewer) viewer.update(scene); },
    focus(name) { if (viewer) viewer.focusItem(name); },
    rebuild: async () => {
      if (viewer) { viewer.dispose(); viewer = null; }
      try { await api.post("/citymap/build", { force: true }).then((j) => waitJob(j.id)); } catch (e) { toast(e.message, "err"); }
      boot();
    },
    dispose() { disposed = true; if (viewer) viewer.dispose(); el.remove(); },
  };
  return stage;
}

// ====================================================================== the viewer
function createViewer(root, data, prefs, opts) {
  const { meta, buf } = data;
  let disposed = false;

  // ---------------------------------------------------------------- renderer, scene, camera
  const renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: "high-performance", logarithmicDepthBuffer: true, stencil: false });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.setClearColor(0x03050b, 1);
  const canvas = renderer.domElement;
  canvas.className = "c3-canvas";
  root.prepend(canvas);
  diagGpu(renderer.getContext(), "3D city");
  // a shader the GPU will not take: in the log, with the driver's own words
  renderer.debug.onShaderError = (gl, program, vs, fs) => {
    const info = [gl.getProgramInfoLog(program), gl.getShaderInfoLog(vs), gl.getShaderInfoLog(fs)].filter(Boolean).join(" | ");
    console.error("3D city shader:", info);
    diag("shader", `3D city: ${info}`);
  };
  let lost = false;
  const onLost = (e) => {
    e.preventDefault();
    if (disposed || lost) return;
    lost = true;
    console.warn("3D city: the graphics context was lost");
    diag("webgl", "3D city: the graphics context was lost (driver reset or GPU switch); rebuilding the view");
    if (opts.onLost) opts.onLost();
  };
  canvas.addEventListener("webglcontextlost", onLost);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(Number(prefs.lens) || 55, 1, 0.3, 400000);
  camera.rotation.order = "YXZ";
  const city = new THREE.Group();
  city.rotation.x = -Math.PI / 2;               // children are in Night City's frame
  scene.add(city);

  const U = {
    uTime: { value: 0 }, uFogColor: { value: new THREE.Color(0x04060d) }, uFogDensity: { value: 0.000085 },
    uPulse: { value: new THREE.Vector4(0, 0, -100, 0) }, uCursor: { value: new THREE.Vector3(0, 0, 0) },
    uPxScale: { value: 0.001 }, uLook: { value: prefs.look === "game" ? 1 : 0 },
    uCut: { value: new THREE.Vector4(0, 0, 0, 0) },
  };
  const mat = (vs, fs, extra = {}, more = {}) => new THREE.ShaderMaterial(Object.assign({
    vertexShader: vs, fragmentShader: fs, uniforms: Object.assign({}, U, extra),
  }, more));
  const additive = { transparent: true, depthWrite: false, blending: THREE.AdditiveBlending };

  // sky
  const sky = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 24), new THREE.ShaderMaterial({
    vertexShader: S.skyVS, fragmentShader: S.skyFS, uniforms: { uTime: U.uTime, uLook: U.uLook }, side: THREE.BackSide, depthWrite: false, depthTest: false,
  }));
  sky.renderOrder = -1000;
  sky.frustumCulled = false;
  sky.scale.setScalar(1000);
  scene.add(sky);

  // ---------------------------------------------------------------- the city's layers
  const L = meta.layers;
  const layerGeo = (name, normals) => {
    const e = L[name];
    if (!e) return null;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(view(buf, e.pos, Float32Array), 3));
    g.setIndex(new THREE.BufferAttribute(view(buf, e.idx, Uint32Array), 1));
    if (e.col) g.setAttribute("aCol", new THREE.BufferAttribute(view(buf, e.col, Uint8Array), 4, true));
    if (normals) g.computeVertexNormals();
    g.computeBoundingSphere();
    return g;
  };
  const meshes = {};
  const add = (name, geo, material, order = 0) => {
    if (!geo) return null;
    const m = new THREE.Mesh(geo, material);
    m.renderOrder = order;
    m.frustumCulled = name !== "terrain" && name !== "water";
    city.add(m);
    meshes[name] = m;
    return m;
  };
  const terrainMat = mat(S.terrainVS, S.terrainFS, { uGrid: { value: prefs.grid ? 1 : 0 } });
  add("terrain", layerGeo("terrain", true), terrainMat);
  add("water", layerGeo("water", true), mat(S.terrainVS, S.waterFS), 1);
  // (uLook is in U: every material of the city follows the look)
  add("cliffs", layerGeo("cliffs", true), mat(S.terrainVS, S.solidFS, { uColor: { value: new THREE.Color(0x03070c) }, uRim: { value: new THREE.Color(0x1fa9c9) }, uCuts: { value: 1 } }));
  add("landmarks", layerGeo("landmarks", true), mat(S.terrainVS, S.solidFS, { uColor: { value: new THREE.Color(0x0a0410) }, uRim: { value: new THREE.Color(0xff4fa3) }, uCuts: { value: 1 } }));
  add("borders", layerGeo("borders"), mat(S.glowVS, S.roadFS, { uLift: { value: 0.25 }, uColor: { value: new THREE.Color(0x0b6b86) },
    uColor2: { value: new THREE.Color(0x0b6b86) }, uIntensity: { value: 0.12 } }, additive), 5);
  add("roads", layerGeo("roads"), mat(S.glowVS, S.roadFS, { uLift: { value: 0.35 }, uColor: { value: new THREE.Color(0x14c8ff) },
    uColor2: { value: new THREE.Color(0xff3d7a) }, uIntensity: { value: 0.3 } }, additive), 6);
  add("metro", layerGeo("metro"), mat(S.glowVS, S.metroFS, { uLift: { value: 0.6 }, uIntensity: { value: 1.6 } }), 4);

  // buildings: the map screen's own blocks (a cache of citymap v2: ~255,000 of them, district by
  // district, each shaded by its district's grey map), else one box per building (the colliders)
  const CU = meta.cubes || null;
  const B = CU ? view(buf, CU.data, Float32Array) : meta.buildings ? view(buf, meta.buildings.data, Float32Array) : new Float32Array(0);
  const SH = CU ? view(buf, CU.shade, Uint8Array) : null;
  const boxes = new BoxSet(B, CU ? 32 : 64);       // (the blocks are small: a finer grid)
  const districts = (meta.districts || []).map((d) => Object.assign({}, d, { poly: d.outline.map((p) => [p[0], p[1]]) }));
  const tops = districts.filter((d) => d.top);
  const districtAt = (x, y) => {
    let sub = null, top = null;
    for (const d of districts) {
      if (!insidePoly(d.poly, x, y)) continue;
      if (d.top) { if (!top) top = d; } else if (!sub) sub = d;
    }
    return { top, sub };
  };
  const hoverUniform = { value: -1 };          // the box under the cursor (an index into B)
  const tintUniform = { value: prefs.tint ? 1 : 0 };
  const blockSets = [];                        // { mesh, game, neon, holo }
  function blockSet(start, n, part) {
    const geo = new THREE.BoxGeometry(1, 1, 1);
    const size = new Float32Array(n * 3), tint = new Float32Array(n * 3), seed = new Float32Array(n), ids = new Float32Array(n),
      shade = new Float32Array(n);
    const c = new THREE.Color();
    const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), sc = new THREE.Vector3();
    const mesh = new THREE.InstancedMesh(geo, null, n);
    let lastTop = null;
    for (let i = 0; i < n; i++) {
      const b = start + i, o = b * 10;
      p.set(B[o], B[o + 1], B[o + 2]);
      sc.set(B[o + 3] * 2, B[o + 4] * 2, B[o + 5] * 2);
      q.set(B[o + 6], B[o + 7], B[o + 8], B[o + 9]).normalize();
      m4.compose(p, q, sc);
      mesh.setMatrixAt(i, m4);
      size[i * 3] = sc.x; size[i * 3 + 1] = sc.y; size[i * 3 + 2] = sc.z;
      let dc = BADLANDS;
      if (lastTop && insidePoly(lastTop.poly, B[o], B[o + 1])) dc = DISTRICT_COLORS[lastTop.id] || BADLANDS;
      else for (const d of tops) if (insidePoly(d.poly, B[o], B[o + 1])) { dc = DISTRICT_COLORS[d.id] || BADLANDS; lastTop = d; break; }
      c.setHex(dc);
      tint[i * 3] = c.r; tint[i * 3 + 1] = c.g; tint[i * 3 + 2] = c.b;
      seed[i] = (Math.sin(b * 12.9898 + B[o] * 0.0137) * 43758.5453) % 1;
      if (seed[i] < 0) seed[i] += 1;
      ids[i] = b;
      shade[i] = SH ? SH[b] / 255 : 0.62;
    }
    geo.setAttribute("aSize", new THREE.InstancedBufferAttribute(size, 3));
    geo.setAttribute("aTint", new THREE.InstancedBufferAttribute(tint, 3));
    geo.setAttribute("aSeed", new THREE.InstancedBufferAttribute(seed, 1));
    geo.setAttribute("aId", new THREE.InstancedBufferAttribute(ids, 1));
    geo.setAttribute("aShade", new THREE.InstancedBufferAttribute(shade, 1));
    let tex = null;
    if (part && part.mask) {
      tex = new THREE.DataTexture(view(buf, part.mask.data, Uint8Array), part.mask.w, part.mask.h, THREE.RedFormat, THREE.UnsignedByteType);
      tex.minFilter = THREE.LinearMipmapLinearFilter;
      tex.magFilter = THREE.LinearFilter;
      tex.generateMipmaps = true;
      tex.needsUpdate = true;
    }
    const mm = part ? part.mask_min : [0, 0], mx = part ? part.mask_max : [1, 1];
    const set = {
      mesh,
      game: mat(S.cubeVS, S.cubeFS, { uHover: hoverUniform, uTint: tintUniform, uMask: { value: tex }, uHasMask: { value: tex ? 1 : 0 },
        uMaskMin: { value: new THREE.Vector2(mm[0], mm[1]) }, uMaskMax: { value: new THREE.Vector2(mx[0], mx[1]) }, uEdge: { value: 0.0 } }),
      neon: mat(S.cubeVS, S.buildingFS, { uHover: hoverUniform, uHolo: { value: 0 }, uTint: tintUniform }),
      holo: mat(S.cubeVS, S.buildingFS, { uHover: hoverUniform, uHolo: { value: 1 }, uTint: tintUniform }, additive),
      tex,
    };
    mesh.material = set.game;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    mesh.frustumCulled = true;
    mesh.renderOrder = 2;
    city.add(mesh);
    blockSets.push(set);
    return set;
  }
  if (CU) for (const part of CU.districts) blockSet(part.start, part.count, part);
  else if (boxes.n) blockSet(0, boxes.n, null);

  // walking surfaces and picking: the ground, the streets, the cliffs, the landmarks
  const ground = new TriGrid(["terrain", "roads", "cliffs", "landmarks", "water"].filter((n) => L[n]).map((n, i) => ({
    pos: view(buf, L[n].pos, Float32Array), idx: view(buf, L[n].idx, Uint32Array), kind: i,
  })));
  const KIND_NAMES = ["terrain", "roads", "cliffs", "landmarks", "water"].filter((n) => L[n]);
  // the real city's ring (cityreal.js): the map's blocks are not drawn there, so they neither hold
  // the camera up, nor stop a walk, nor catch the pointer
  const inCut = (x, y) => U.uCut.value.w > 0.5 && Math.hypot(x - U.uCut.value.x, y - U.uCut.value.y) < U.uCut.value.z - 25;
  const surfaceZ = (x, y, zMax = Infinity) => {
    let z = ground.heightAt(x, y, zMax);
    if (inCut(x, y)) return z;
    for (const b of boxes.near(x, y, 0, nearTmp)) {
      const t = boxes.topAt(b, x, y);
      if (t > z && t <= zMax) z = t;
    }
    return z;
  };
  const nearTmp = [];

  // ---------------------------------------------------------------- glowing lines (districts)
  function ribbon(points, closed, { color, width = 1, px = 1.6, intensity = 1, dash = 0, flow = 0 }) {
    const pos = [], dir = [], side = [], along = [], idx = [];
    const pts = closed ? points.concat([points[0]]) : points;
    let acc = 0;
    for (let i = 0; i + 1 < pts.length; i++) {
      const [ax, ay, az] = pts[i], [bx, by, bz] = pts[i + 1];
      const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
      const base = pos.length / 3;
      for (const [x, y, z, a] of [[ax, ay, az, acc], [bx, by, bz, acc + len]]) {
        for (const sd of [-1, 1]) { pos.push(x, y, z); dir.push(dx / len, dy / len); side.push(sd); along.push(a); }
      }
      idx.push(base, base + 1, base + 2, base + 1, base + 3, base + 2);
      acc += len;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("aDir", new THREE.Float32BufferAttribute(dir, 2));
    g.setAttribute("aSide", new THREE.Float32BufferAttribute(side, 1));
    g.setAttribute("aAlong", new THREE.Float32BufferAttribute(along, 1));
    g.setIndex(idx);
    const m = new THREE.Mesh(g, mat(S.lineVS, S.lineFS, {
      uColor: { value: new THREE.Color(color) }, uIntensity: { value: intensity }, uWidth: { value: width }, uPx: { value: px },
      uDash: { value: dash }, uFlow: { value: flow },
    }, additive));
    m.renderOrder = 9;
    m.frustumCulled = false;
    return m;
  }
  // a line laid on the city: densified, each point on the highest surface under it
  function drape(poly, lift = 2, step = 14) {
    const out = [];
    for (let i = 0; i < poly.length; i++) {
      const [ax, ay] = poly[i], [bx, by] = poly[(i + 1) % poly.length];
      const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / step));
      for (let k = 0; k < n; k++) {
        const x = ax + (bx - ax) * k / n, y = ay + (by - ay) * k / n;
        const z = ground.heightAt(x, y);
        out.push([x, y, (Number.isFinite(z) ? Math.max(z, -3) : -3) + lift]);
      }
    }
    return out;
  }
  const districtGroup = new THREE.Group();
  city.add(districtGroup);
  const districtLines = new Map();
  for (const d of districts) {
    const col = DISTRICT_COLORS[d.id] || DISTRICT_COLORS[d.parent] || BADLANDS;
    const line = ribbon(drape(d.poly, d.top ? 3 : 2), true, d.top
      ? { color: col, width: 2.5, px: 2.2, intensity: 0.9, flow: 1.4 }
      : { color: col, width: 1.2, px: 1.2, intensity: 0.45, dash: 18 });
    line.userData.base = line.material.uniforms.uIntensity.value;
    districtGroup.add(line);
    districtLines.set(d.id, line);
    const [cx, cy] = polyCentroid(d.poly);
    const z = ground.heightAt(cx, cy);
    d.center = [cx, cy, Number.isFinite(z) ? z : 0];
  }
  districtGroup.visible = prefs.districts;

  // ---------------------------------------------------------------- markers (live maps, players)
  const markerGroup = new THREE.Group();
  city.add(markerGroup);
  const markers = new Map();           // key -> { group, beam, pad, ring, label, item }
  const beamGeo = (() => {
    const g = new THREE.CylinderGeometry(1, 1, 1, 24, 1, true);
    g.rotateX(Math.PI / 2);             // along z (game up)
    g.translate(0, 0, 0.5);
    return g;
  })();
  function beamMesh(color, height, radius, intensity) {
    const g = beamGeo.clone();
    g.scale(1, 1, height);
    const m = new THREE.Mesh(g, mat(S.beamVS, S.beamFS, { uColor: { value: new THREE.Color(color) }, uIntensity: { value: intensity }, uRadius: { value: radius } }, Object.assign({ side: THREE.DoubleSide }, additive)));
    m.renderOrder = 12;
    m.frustumCulled = false;
    return m;
  }
  const ringGeo = new THREE.PlaneGeometry(2, 2);
  function ringMesh(color, radius, phase = 0) {
    const m = new THREE.Mesh(ringGeo, mat(S.padVS, S.ringFS, { uColor: { value: new THREE.Color(color) }, uIntensity: { value: 1.2 }, uPhase: { value: phase } }, additive));
    m.scale.setScalar(radius);
    m.renderOrder = 11;
    m.frustumCulled = false;
    return m;
  }
  function padMesh(color, b) {
    const x0 = b[0][0], y0 = b[0][1], x1 = b[1][0], y1 = b[1][1];
    const g = new THREE.PlaneGeometry(x1 - x0, y1 - y0);
    g.translate((x0 + x1) / 2, (y0 + y1) / 2, 0);
    const m = new THREE.Mesh(g, mat(S.padVS, S.padFS, { uColor: { value: new THREE.Color(color) }, uMin: { value: new THREE.Vector2(x0, y0) },
      uMax: { value: new THREE.Vector2(x1, y1) }, uIntensity: { value: 1 } }, Object.assign({ side: THREE.DoubleSide }, additive)));
    m.renderOrder = 10;
    m.frustumCulled = false;
    return m;
  }
  const labelLayer = h("div.c3-labels");
  root.append(labelLayer);
  function labelEl(cls, title, sub, onclick) {
    const tag = h("div.c3-tag", h("b", title), sub ? h("span", sub) : null);
    const e = h(`div.c3-label.${cls}`, tag);
    if (onclick) { tag.classList.add("click"); tag.addEventListener("click", (ev) => { ev.stopPropagation(); onclick(); }); }
    labelLayer.append(e);
    return e;
  }
  function disposeObj(o) {
    o.traverse((x) => { if (x.geometry && x.geometry !== ringGeo) x.geometry.dispose(); if (x.material) x.material.dispose(); });
  }
  function setItem(item) {
    const key = item.key;
    const role = ROLE[item.role] || ROLE.other;
    const b = item.bounds && item.bounds[0] ? item.bounds : [[-20, -20, 0], [20, 20, 10]];
    const sig = JSON.stringify([item.role, b, item.name]);
    let mk = markers.get(key);
    if (mk && mk.sig !== sig) { markerGroup.remove(mk.group); disposeObj(mk.group); mk.label.remove(); markers.delete(key); mk = null; }
    if (!mk) {
      const group = new THREE.Group();
      const isSpot = item.role !== "preset";
      const size = Math.max(b[1][0] - b[0][0], b[1][1] - b[0][1], 10);
      const pad = isSpot ? padMesh(role.color, b) : null;
      const beam = beamMesh(role.color, item.role === "previous" || item.role === "preset" ? 220 : 650, isSpot ? 2.2 : 1.2, item.role === "previous" ? 0.35 : 0.9);
      const ring = ringMesh(role.color, Math.max(16, size * 0.7), Math.random());
      if (pad) {
        group.add(pad);
        const xray = new THREE.Mesh(pad.geometry, pad.material.clone());
        xray.material.depthTest = false;
        xray.material.uniforms.uIntensity = { value: 0.28 };
        xray.renderOrder = 13;
        xray.frustumCulled = false;
        group.add(xray);
      }
      group.add(beam, ring);
      markerGroup.add(group);
      const label = labelEl(`role-${item.role}`, item.name, role.tag + (item.note ? ` · ${item.note}` : ""), () => focusItem(key, true));
      mk = { group, beam, pad, ring, label, sig, item };
      markers.set(key, mk);
    }
    mk.item = item;
    const a = item.anchor;
    mk.group.position.set(a.x, a.y, (a.z || 0) + 0.15);
    mk.group.rotation.z = (a.yaw || 0) * Math.PI / 180;
    mk.top = [a.x, a.y, (a.z || 0) + (b[1][2] || 10) + 18];
    mk.seen = true;
  }
  const playerMarks = new Map();
  function setPlayer(p) {
    let mk = playerMarks.get(p.key);
    if (!mk) {
      const group = new THREE.Group();
      const beam = beamMesh(PLAYER, 160, 0.9, 1.1);
      const ring = ringMesh(PLAYER, 9, Math.random());
      const pin = new THREE.Mesh(new THREE.OctahedronGeometry(1.6, 0), mat(S.terrainVS, S.solidFS, { uColor: { value: new THREE.Color(0x0a2a40) }, uRim: { value: new THREE.Color(PLAYER) } }));
      pin.scale.set(1, 1, 1.8);
      pin.position.z = 6;
      group.add(beam, ring, pin);
      markerGroup.add(group);
      mk = { group, pin, label: labelEl("player", p.name, "PLAYER", () => flyTo([p.x, p.y, p.z], 140)) };
      playerMarks.set(p.key, mk);
    }
    mk.p = p;
    mk.group.position.set(p.x, p.y, p.z || 0);
    mk.top = [p.x, p.y, (p.z || 0) + 14];
    mk.seen = true;
  }

  const districtLabels = districts.map((d) => {
    const e = labelEl(d.top ? "district" : "subdistrict", d.top ? d.label.toUpperCase() : d.label, null, () => flyTo(d.center, d.top ? 2200 : 900));
    return { d, e };
  });

  // ---------------------------------------------------------------- post processing
  const rt = new THREE.WebGLRenderTarget(4, 4, { type: THREE.HalfFloatType, samples: 4 });
  const composer = new EffectComposer(renderer, rt);
  composer.addPass(new RenderPass(scene, camera));
  // nothing that is not a finite colour reaches the bloom (a NaN pixel would come out a black square)
  composer.addPass(new ShaderPass({ uniforms: { tDiffuse: { value: null } }, vertexShader: S.CLEAN_VS, fragmentShader: S.CLEAN_FS }));
  const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.5, 0.38, 0.8);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());
  bloom.enabled = prefs.glow && prefs.quality !== "low";

  // ---------------------------------------------------------------- HUD
  const hud = h("div.c3-hud");
  const modeBtns = {};
  const modeBar = h("div.c3-modes", Object.entries(MODES).map(([k, label]) => (modeBtns[k] = h("button", { type: "button", onclick: () => setMode(k) },
    h("span.k", String(Object.keys(MODES).indexOf(k) + 1)), label))));
  const toggles = [["buildings", "Buildings"], ["real", "Real city (the game's own meshes, all of Night City)"], ["roads", "Streets"], ["metro", "NCART"],
    ["districts", "Districts"], ["labels", "Labels"], ["grid", "Grid"],
    ["tint", "District colours"], ["holo", "Hologram"], ["glow", "Glow"], ["autotilt", "Tilt as you zoom (like the map screen)"]];
  const prefBoxes = {};
  const lookSel = h("select", Object.entries(LOOKS).map(([v, label]) => h("option", { value: v }, label)));
  lookSel.value = LOOKS[prefs.look] ? prefs.look : "game";
  lookSel.addEventListener("change", () => { prefs.look = lookSel.value; savePrefs(prefs); applyPrefs(); poke(); });
  const layerMenu = h("div.c3-menu.hidden", h("label", h("span", "Look"), lookSel), h("div.sep"), toggles.map(([k, label]) => {
    const cb = h("input", { type: "checkbox", checked: !!prefs[k] });
    prefBoxes[k] = cb;
    cb.addEventListener("change", () => { prefs[k] = cb.checked; savePrefs(prefs); applyPrefs(); });
    return h("label", cb, h("span", label));
  }), h("div.sep"),
  h("label", h("span", "Real city"), (() => {
    const sel = h("select", { title: "How far the game's own meshes reach: the game's own distances, or less, or more" },
      h("option", { value: "close" }, "Less detail (faster)"), h("option", { value: "normal" }, "Like the game"),
      h("option", { value: "wide" }, "More detail (further out)"));
    sel.value = prefs.realRange || "normal";
    sel.addEventListener("change", () => { prefs.realRange = sel.value; savePrefs(prefs); real.setRange(sel.value); poke(); });
    return sel;
  })()),
  h("label", (() => {
    const sel = h("select", h("option", { value: "high" }, "High quality"), h("option", { value: "low" }, "Fast"));
    sel.value = prefs.quality;
    sel.addEventListener("change", () => { prefs.quality = sel.value; savePrefs(prefs); real.setQuality(sel.value); applyPrefs(); resize(); });
    return sel;
  })()),
  h("button.btn.small.ghost", { onclick: () => { layerMenu.classList.add("hidden"); if (opts.onRebuild) opts.onRebuild(); } }, "Rebuild from the game"));
  const layerBtn = h("button.c3-icon", { type: "button", title: "Layers and look", onclick: (e) => { e.stopPropagation(); layerMenu.classList.toggle("hidden"); } }, "☰");
  // "Go to": districts, landmarks, the maps, the players, or coordinates (x y [z]), as you type
  const searchIn = h("input.c3-search", { type: "search", placeholder: "Go to…  district, map, player or x y", spellcheck: false,
    title: "Go to a place (G or /): a district, a landmark, a map, a player, or coordinates like -530 300" });
  const searchList = h("div.c3-results.hidden");
  const searchBox = h("div.c3-searchbox", searchIn, searchList);
  const pretty = (id) => String(id).replace(/_[a-z]$/, "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  function places() {
    const out = [];
    for (const m of markers.values()) {
      if (m.item.role === "preset") continue;
      out.push({ label: m.item.name, sub: (ROLE[m.item.role] || ROLE.other).tag.toLowerCase(), go: () => focusItem(m.item.key, true) });
    }
    for (const m of playerMarks.values()) {
      const p = m.p || {};
      if (Number.isFinite(p.x)) out.push({ label: p.name || "player", sub: "player", go: () => flyTo([p.x, p.y, p.z || 0], 260, 45) });
    }
    for (const d of districts) out.push({ label: d.label, sub: d.top ? "district" : "sub-district", go: () => flyTo(d.center, d.top ? 2400 : 1000) });
    for (const l of meta.landmarks || []) out.push({ label: pretty(l.id), sub: "landmark", go: () => flyTo(l.center, 420, 40) });
    return out;
  }
  let results = [], sel = 0;
  function coordsOf(q) {
    const m = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)(?:\s*[,\s]\s*(-?\d+(?:\.\d+)?))?\s*$/);
    if (!m) return null;
    const x = Number(m[1]), y = Number(m[2]);
    const z = m[3] != null ? Number(m[3]) : surfaceZ(x, y);
    return { label: `${x.toFixed(1)}, ${y.toFixed(1)}${m[3] != null ? ", " + z.toFixed(1) : ""}`, sub: "coordinates",
      go: () => flyTo([x, y, Number.isFinite(z) ? z : 0], 300, 45) };
  }
  function search() {
    const q = searchIn.value.trim().toLowerCase();
    results = [];
    if (q) {
      const c = coordsOf(q);
      if (c) results.push(c);
      const all = places();
      const starts = all.filter((p) => p.label.toLowerCase().startsWith(q));
      const has = all.filter((p) => !p.label.toLowerCase().startsWith(q) && (p.label.toLowerCase().includes(q) || p.sub.includes(q)));
      results.push(...starts, ...has);
    } else if (document.activeElement === searchIn) {
      results = places().filter((p) => p.sub !== "landmark" && p.sub !== "sub-district");
    }
    results = results.slice(0, 12);
    sel = 0;
    clear(searchList).append(...results.map((r, i) => h("button", { type: "button", class: i === sel ? "on" : "",
      onmousedown: (e) => { e.preventDefault(); goTo(r); } }, h("b", r.label), h("span", r.sub))));
    searchList.classList.toggle("hidden", !results.length);
  }
  function goTo(r) {
    r.go();
    searchIn.value = "";
    searchIn.blur();
    searchList.classList.add("hidden");
    canvas.focus();
  }
  searchIn.addEventListener("input", search);
  searchIn.addEventListener("focus", search);
  searchIn.addEventListener("blur", () => setTimeout(() => searchList.classList.add("hidden"), 120));
  searchIn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!results.length) return;
      sel = (sel + (e.key === "ArrowDown" ? 1 : results.length - 1)) % results.length;
      [...searchList.children].forEach((b, i) => b.classList.toggle("on", i === sel));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (results[sel]) goTo(results[sel]);
    } else if (e.key === "Escape") {
      searchIn.value = "";
      searchIn.blur();
      canvas.focus();
    }
    e.stopPropagation();
  });
  // the map screen's angles, turning the view, and the lens
  const viewBtns = {};
  const lensSel = h("select.c3-lens", { title: "Lens: a long lens flattens the city like the map screen" },
    Object.entries(LENSES).map(([v, label]) => h("option", { value: v }, label)));
  lensSel.value = String(LENSES[prefs.lens] ? prefs.lens : 55);
  lensSel.addEventListener("change", () => { setLens(Number(lensSel.value)); canvas.focus(); });
  const viewBar = h("div.c3-views",
    Object.entries(VIEWS).map(([k, v]) => (viewBtns[k] = h("button", { type: "button", title: `${v.label} view (${v.key})`, onclick: () => setView(k) }, v.label))),
    h("span.c3-vsep"),
    h("button", { type: "button", title: "Turn the view left (,)", onclick: () => turn(-45) }, "⟲"),
    h("button", { type: "button", title: "Turn the view right (.)", onclick: () => turn(45) }, "⟳"),
    h("button", { type: "button", title: "North up (N)", onclick: () => northUp() }, "N"),
    h("span.c3-vsep"), lensSel);
  const pickBtn = opts.onPick ? h("button.c3-tool", { type: "button", title: "Click the city to set the new spot (P)", onclick: () => setPick(!picking) }, "◎ Set the spot") : null;
  const focusBtn = h("button.c3-tool", { type: "button", title: "Back to the map (F)", onclick: () => focusItem(null, true) }, "⌖ Focus");
  const fullBtn = h("button.c3-icon", { type: "button", title: "Full screen", onclick: () => (document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen && root.requestFullscreen()) }, "⛶");
  const where = h("div.c3-where");
  const coords = h("div.c3-coords");
  const compass = h("canvas.c3-compass", { width: 600, height: 50 });
  const speedEl = h("div.c3-speed");
  const help = h("div.c3-help");
  const realChip = h("div.c3-real.hidden", h("span.c3-real-t"), h("div.c3-bar", h("i")));
  compass.style.cursor = "pointer";
  compass.title = "North up (N)";
  compass.addEventListener("click", () => northUp());
  const reticle = h("div.c3-reticle", h("i"), h("i"), h("i"), h("i"));
  const lockHint = h("div.c3-lockhint.hidden", "click to look around");
  hud.append(
    h("div.c3-tl", modeBar, viewBar, h("div.c3-row", layerBtn, searchBox, pickBtn, focusBtn, fullBtn), layerMenu),
    h("div.c3-tr", where, compass, coords, speedEl),
    h("div.c3-bl", help),
    realChip, reticle, lockHint);
  root.append(hud);
  if (opts.upgrade || !CU) {
    hud.append(h("div.c3-upgrade", h("b", "More detail: "), "this 3D city was built by an older Studio, with one box per building. ",
      "Rebuilding it from the game (about a minute) adds the map screen's own ~255,000 building blocks. ",
      h("button.btn.small.primary", { type: "button", onclick: () => { if (opts.onRebuild) opts.onRebuild(); } }, "Rebuild now")));
  }
  const cursorGlow = h("div.c3-cursor");
  root.append(cursorGlow);
  document.addEventListener("click", closeMenu);
  function closeMenu(e) { if (!layerMenu.contains(e.target) && e.target !== layerBtn) layerMenu.classList.add("hidden"); }

  // the game's own meshes around where the view looks (see cityreal.js)
  scene.fog = new THREE.FogExp2(U.uFogColor.value.getHex(), U.uFogDensity.value);
  const real = realCity({
    THREE, api, city, scene, poke, range: prefs.realRange, quality: prefs.quality,
    onStatus: (text, frac) => {
      realChip.classList.toggle("hidden", !text);
      if (!text) return;
      realChip.firstChild.textContent = text;
      realChip.lastChild.style.visibility = frac == null ? "hidden" : "visible";
      realChip.lastChild.firstChild.style.width = `${Math.round((frac || 0) * 100)}%`;
    },
  });

  function applyPrefs() {
    const game = prefs.look === "game";
    real.setEnabled(prefs.real);
    U.uLook.value = game ? 1 : 0;
    tintUniform.value = prefs.tint ? 1 : 0;
    for (const b of blockSets) {
      b.mesh.visible = prefs.buildings;
      b.mesh.material = prefs.holo ? b.holo : game ? b.game : b.neon;
      b.mesh.renderOrder = prefs.holo ? 8 : 2;
    }
    renderer.setClearColor(game ? 0x05080d : 0x03050b, 1);
    U.uFogColor.value.setHex(game ? 0x070b11 : 0x04060d);
    bloom.strength = game ? 0.32 : 0.5;
    bloom.threshold = game ? 0.85 : 0.8;
    if (meshes.roads) meshes.roads.visible = prefs.roads;
    if (meshes.borders) meshes.borders.visible = prefs.roads;
    if (meshes.metro) meshes.metro.visible = prefs.metro;
    districtGroup.visible = prefs.districts;
    terrainMat.uniforms.uGrid.value = prefs.grid ? 1 : 0;
    bloom.enabled = prefs.glow && prefs.quality !== "low";
    labelLayer.classList.toggle("hidden", !prefs.labels);
  }
  applyPrefs();

  // ---------------------------------------------------------------- sizing
  let W = 1, H = 1, dprMax = 2, dpr = 1;
  function resize() {
    const r = root.getBoundingClientRect();
    if (!r.width || !r.height) return;
    W = r.width; H = r.height;
    dprMax = prefs.quality === "low" ? 1 : Math.min(window.devicePixelRatio || 1, 2);
    dpr = Math.min(dpr || dprMax, dprMax) || dprMax;
    applySize();
  }
  function applySize() {
    renderer.setPixelRatio(dpr);
    renderer.setSize(W, H, false);
    composer.setPixelRatio(dpr);
    composer.setSize(W, H);
    bloom.resolution.set(W * dpr, H * dpr);
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
    U.uPxScale.value = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) / H;
  }
  const ro = new ResizeObserver(resize);
  ro.observe(root);
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  resize();

  // ---------------------------------------------------------------- camera control
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.085;
  controls.screenSpacePanning = false;
  controls.zoomToCursor = true;
  controls.minDistance = 25;
  controls.maxDistance = 26000;
  controls.maxPolarAngle = THREE.MathUtils.degToRad(86);
  controls.rotateSpeed = 0.6;
  controls.panSpeed = 1.1;
  const cm = meta.city || { min: [-5000, -7000, 0], max: [5000, 4500, 400] };
  const cityCenter = [(cm.min[0] + cm.max[0]) / 2, (cm.min[1] + cm.max[1]) / 2];
  g2t(cityCenter[0] - 600, cityCenter[1] + 1800, 0, controls.target);
  const lensK = (fov) => Math.tan(THREE.MathUtils.degToRad(27.5)) / Math.tan(THREE.MathUtils.degToRad(fov) / 2);
  controls.maxDistance = 26000 * lensK(camera.fov);
  controls.minDistance = 25 * lensK(camera.fov);
  const FOG = U.uFogDensity.value;
  U.uFogDensity.value = FOG / lensK(camera.fov);          // a long lens looks from farther: the haze thins with it
  if (prefs.look === "game") {
    const el0 = THREE.MathUtils.degToRad(62), d0 = 6400 * lensK(camera.fov);
    camera.position.copy(controls.target).add(new THREE.Vector3(0, Math.sin(el0), Math.cos(el0)).multiplyScalar(d0));
  } else {
    camera.position.copy(controls.target).add(new THREE.Vector3(2600, 3600, 4200).multiplyScalar(lensK(camera.fov)));
  }
  controls.update();

  let mode = "orbit";
  const fly = { yaw: 0, pitch: -0.3, vel: new THREE.Vector3(), speed: prefs.speed || 70 };
  const walk = { feet: [0, 0, 0], vz: 0, vx: 0, vy: 0, onGround: false, jumps: 0 };
  const keys = new Set();
  let locked = false;
  let tween = null;
  let idleUntil = 0;

  function setMode(m, quiet) {
    if (!MODES[m] || m === mode && !quiet) return;
    const prev = mode;
    mode = m;
    prefs.mode = m;
    savePrefs(prefs);
    tween = null;
    for (const [k, b] of Object.entries(modeBtns)) b.classList.toggle("on", k === m);
    controls.enabled = m === "orbit";
    clear(help).append(...HELP[m].map(([k, t]) => h("span", h("kbd", k), t)));
    root.classList.toggle("c3-first", m !== "orbit");
    if (m === "orbit" && prev === "orbit") {
      // (the first call: the view as it was set up)
    } else if (m === "orbit") {
      if (locked) document.exitPointerLock();
      // look at the point ahead
      const dir = camera.getWorldDirection(new THREE.Vector3());
      const [x, y, z] = t2g(camera.position);
      const [dx, dy, dz] = t2g(dir);
      const hit = raycast(x, y, z, dx, dy, dz, 6000 * lensK(camera.fov));
      const tgt = hit ? hit.p : [x + dx * 400, y + dy * 400, surfaceZ(x + dx * 400, y + dy * 400)];
      g2t(tgt[0], tgt[1], Number.isFinite(tgt[2]) ? tgt[2] : 0, controls.target);
      controls.update();
      camera.near = 0.5;
    } else {
      const dir = camera.getWorldDirection(new THREE.Vector3());
      fly.yaw = Math.atan2(-dir.x, -dir.z);
      fly.pitch = Math.asin(THREE.MathUtils.clamp(dir.y, -1, 1));
      fly.vel.set(0, 0, 0);
      if (m === "walk") {
        // land where the view was: the orbit's centre, or under the camera; on a street, not in a building
        const [cx, cy, cz] = t2g(camera.position);
        const [tx, ty] = prev === "orbit" ? t2g(controls.target) : [cx, cy];
        const low = cz - surfaceZ(cx, cy, cz) < 30;
        const spot = low ? [cx, cy, surfaceZ(cx, cy, cz + STEP)] : findStreet(tx, ty);
        if (!Number.isFinite(spot[2])) spot[2] = 0;
        walk.feet = spot.slice();
        walk.vx = walk.vy = walk.vz = 0;
        walk.onGround = true;
        fly.pitch = low ? THREE.MathUtils.clamp(fly.pitch, -0.6, 0.4) : -0.04;
        if (!low) startTween([spot[0], spot[1], spot[2] + EYE], null, 1.1, () => {});    // a quick drop-in, not a jump cut
      }
      camera.near = m === "walk" ? 0.1 : 0.3;
    }
    camera.updateProjectionMatrix();
    lockHint.classList.toggle("hidden", m === "orbit" || locked);
    reticle.classList.toggle("on", m !== "orbit");
    poke();
  }

  // ---------------------------------------------------------------- the map screen's angles
  // where the camera is around the orbit's centre: azimuth (0: looking north), elevation, distance
  function sphOf() {
    const off = camera.position.clone().sub(controls.target);
    const d = off.length() || 1;
    return { az: Math.atan2(off.x, off.z), el: Math.asin(THREE.MathUtils.clamp(off.y / d, -1, 1)), d };
  }
  function orbitTo(az, el, d, dur = 900) {
    if (mode !== "orbit") setMode("orbit");
    const from = sphOf();
    const daz = Math.atan2(Math.sin(az - from.az), Math.cos(az - from.az));        // the short way round
    tween = { t0: performance.now(), dur, sph: { from, to: { az: from.az + daz, el, d } } };
    poke();
  }
  function setView(key) {
    const v = VIEWS[key];
    if (!v) return;
    const cur = sphOf();
    let az = cur.az;
    if (v.yaw != null) az = Math.round((cur.az - Math.PI / 4) / (Math.PI / 2)) * (Math.PI / 2) + Math.PI / 4;    // the nearest diagonal
    if (key === "top") az = Math.round(cur.az / (Math.PI / 2)) * (Math.PI / 2);
    orbitTo(az, THREE.MathUtils.degToRad(v.pitch), cur.d);
    // the map view tilts with the zoom like the map screen; the others hold their angle
    prefs.autotilt = key === "map";
    if (prefBoxes.autotilt) prefBoxes.autotilt.checked = prefs.autotilt;
    prefs.view = key;
    savePrefs(prefs);
    markView(key);
  }
  function markView(key) { for (const [k, b] of Object.entries(viewBtns)) b.classList.toggle("on", k === key); }
  function turn(deg) { const cur = sphOf(); orbitTo(cur.az + THREE.MathUtils.degToRad(deg), cur.el, cur.d, 600); }
  function northUp() { const cur = sphOf(); orbitTo(0, cur.el, cur.d, 700); }
  function setLens(fov) {
    const before = camera.fov;
    camera.fov = fov;
    controls.maxDistance = 26000 * lensK(fov);
    controls.minDistance = 25 * lensK(fov);
    U.uFogDensity.value = FOG / lensK(fov);
    if (mode === "orbit") {                   // the same width of the city in view: closer with a wide lens
      const cur = sphOf();
      const d = cur.d * lensK(fov) / lensK(before);
      camera.position.copy(controls.target).add(new THREE.Vector3(Math.sin(cur.az) * Math.cos(cur.el), Math.sin(cur.el), Math.cos(cur.az) * Math.cos(cur.el)).multiplyScalar(d));
      controls.update();
    }
    prefs.lens = fov;
    savePrefs(prefs);
    applySize();
    poke();
  }
  let lastOrbitD = 0;
  // zooming in tilts the view down to the streets, zooming out lifts it to a plan (the map screen)
  function autoTilt(dt) {
    const cur = sphOf();
    if (lastOrbitD && Math.abs(cur.d - lastOrbitD) / lastOrbitD > 0.002 && !drag) {
      const dn = cur.d / lensK(camera.fov);
      const f = THREE.MathUtils.clamp((Math.log(dn) - Math.log(260)) / (Math.log(9000) - Math.log(260)), 0, 1);
      const want = THREE.MathUtils.degToRad(THREE.MathUtils.lerp(40, 80, f));
      const el = cur.el + (want - cur.el) * Math.min(1, dt * 5);
      camera.position.copy(controls.target).add(tmp.set(Math.sin(cur.az) * Math.cos(el), Math.sin(el), Math.cos(cur.az) * Math.cos(el)).multiplyScalar(cur.d));
      controls.update();
    }
    lastOrbitD = sphOf().d;
  }

  // where you were before a jump: Backspace goes back
  const history = [];
  function remember() {
    history.push({ cam: camera.position.clone(), tgt: controls.target.clone() });
    if (history.length > 40) history.shift();
  }
  function back() {
    const v = history.pop();
    if (!v) return;
    if (mode !== "orbit") setMode("orbit");
    tween = { t0: performance.now(), dur: 1100, fromCam: camera.position.clone(), fromTgt: controls.target.clone(), toCam: v.cam, toTgt: v.tgt, arc: 0 };
    poke();
  }
  // + / - : nearer, farther (in fly mode: faster, slower)
  function zoomBy(k) {
    if (mode !== "orbit") { fly.speed = THREE.MathUtils.clamp(fly.speed * (k < 1 ? 1.25 : 0.8), 3, 900); prefs.speed = fly.speed; savePrefs(prefs); poke(); return; }
    const off = camera.position.clone().sub(controls.target);
    const d = THREE.MathUtils.clamp(off.length() * k, controls.minDistance, controls.maxDistance);
    tween = { t0: performance.now(), dur: 380, fromCam: camera.position.clone(), fromTgt: controls.target.clone(),
      toCam: controls.target.clone().add(off.setLength(d)), toTgt: controls.target.clone(), arc: 0 };
    poke();
  }
  function tilt(deg) {
    const cur = sphOf();
    orbitTo(cur.az, THREE.MathUtils.clamp(cur.el + THREE.MathUtils.degToRad(deg), 0.06, 1.5), cur.d, 420);
  }

  // a smooth camera move: to a point (game) at a distance, or the camera itself to a spot
  function flyTo(p, dist = 900, pitchDeg = 52) {
    remember();
    if (mode !== "orbit") setMode("orbit");
    dist *= lensK(camera.fov);
    const tgt = g2t(p[0], p[1], Number.isFinite(p[2]) ? p[2] : 0);
    const off = camera.position.clone().sub(controls.target);
    const az = Math.atan2(off.x, off.z);
    const el2 = THREE.MathUtils.degToRad(pitchDeg);
    const camTo = tgt.clone().add(new THREE.Vector3(Math.sin(az) * Math.cos(el2), Math.sin(el2), Math.cos(az) * Math.cos(el2)).multiplyScalar(dist));
    tween = { t0: performance.now(), dur: 1500, fromCam: camera.position.clone(), fromTgt: controls.target.clone(), toCam: camTo, toTgt: tgt, arc: Math.min(2500, camera.position.distanceTo(camTo) * 0.25) };
    pulse(p[0], p[1]);
    poke();
  }
  function startTween(eyeGame, _unused, dur, done) {
    const to = g2t(eyeGame[0], eyeGame[1], eyeGame[2]);
    tween = { t0: performance.now(), dur: dur * 1000, fromCam: camera.position.clone(), toCam: to, camOnly: true, done };
  }
  function pulse(x, y) {
    U.uPulse.value.set(x, y, U.uTime.value, 1);
  }

  let focusKey = null;           // the map the view follows (its name)
  let focusDone = null;
  function findFocus(name) {
    const list = [...markers.values()].filter((m) => !name || m.item.map === name);
    return list.find((m) => m.item.role === "new") || list.find((m) => m.item.role === "now") || list.find((m) => m.item.role !== "preset") || null;
  }
  function focusItem(key, animate) {
    const mk = key && markers.has(key) ? markers.get(key) : findFocus(key || focusKey);
    if (!mk) { if (!key) flyTo([cityCenter[0], cityCenter[1], 0], 7000, 55); return; }
    const a = mk.item.anchor;
    const b = mk.item.bounds || [[-20, -20, 0], [20, 20, 10]];
    const size = Math.max(b[1][0] - b[0][0], b[1][1] - b[0][1], 40);
    const d = Math.min(4000, Math.max(420, size * 3.5));
    if (animate) flyTo([a.x, a.y, a.z || 0], d, 60);
    else {
      g2t(a.x, a.y, a.z || 0, controls.target);
      const el2 = THREE.MathUtils.degToRad(60), az = THREE.MathUtils.degToRad(35);
      camera.position.copy(controls.target).add(new THREE.Vector3(Math.sin(az) * Math.cos(el2), Math.sin(el2), Math.cos(az) * Math.cos(el2)).multiplyScalar(d));
      controls.update();
      pulse(a.x, a.y);
    }
  }

  // the nearest open street ground around (x, y): no building over it
  function findStreet(x, y) {
    const covered = (px, py, gz) => {
      if (inCut(px, py)) return false;
      for (const b of boxes.near(px, py, RADIUS, nearTmp)) {
        const t = boxes.topAt(b, px, py);
        if (Number.isFinite(t) && t > gz + 1) return true;
      }
      return false;
    };
    for (let r = 0; r <= 600; r += 6) {
      const n = r ? Math.max(8, Math.round(2 * Math.PI * r / 6)) : 1;
      for (let k = 0; k < n; k++) {
        const a = k / n * Math.PI * 2;
        const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r;
        const gz = ground.heightAt(px, py);
        if (!Number.isFinite(gz) || gz < -2.5) continue;          // the sea
        if (!covered(px, py, gz)) return [px, py, gz];
      }
    }
    const gz = ground.heightAt(x, y);
    return [x, y, Number.isFinite(gz) ? gz : 0];
  }

  // ---------------------------------------------------------------- picking
  function raycast(x, y, z, dx, dy, dz, maxT = 40000) {
    const n = Math.hypot(dx, dy, dz) || 1;
    dx /= n; dy /= n; dz /= n;
    let best = null;
    const g = ground.raycast(x, y, z, dx, dy, dz, maxT);
    if (g) best = { t: g.t, kind: KIND_NAMES[g.kind] || "ground" };
    if (prefs.buildings && boxes.n) {
      const b = boxes.raycast(x, y, z, dx, dy, dz, best ? best.t : maxT);
      if (b && (!best || b.t < best.t) && !inCut(x + dx * b.t, y + dy * b.t)) best = { t: b.t, kind: "building", box: b.box };
    }
    // the sea (z = -3) when nothing else is hit
    if (!best && dz < -1e-4) {
      const t = (-3 - z) / dz;
      if (t > 0 && t < maxT) best = { t, kind: "water" };
    }
    if (best) best.p = [x + dx * best.t, y + dy * best.t, z + dz * best.t];
    return best;
  }
  const ndc = new THREE.Vector2();
  const rc = new THREE.Raycaster();
  function rayFromScreen(px, py) {
    ndc.set((px / W) * 2 - 1, -(py / H) * 2 + 1);
    rc.setFromCamera(ndc, camera);
    const [x, y, z] = t2g(rc.ray.origin);
    const [dx, dy, dz] = t2g(rc.ray.direction);
    return raycast(x, y, z, dx, dy, dz);
  }
  let mouse = null;               // { x, y } in the stage
  let hover = null;               // last ray hit under the cursor (or the crosshair)
  let hoverDirty = true;
  let picking = false;
  let ghost = null;
  function setPick(on) {
    picking = !!on && !!opts.onPick;
    root.classList.toggle("c3-picking", picking);
    if (pickBtn) pickBtn.classList.toggle("on", picking);
    if (!picking && ghost) { markerGroup.remove(ghost.group); disposeObj(ghost.group); ghost.label.remove(); ghost = null; }
    poke();
  }
  function updateGhost() {
    if (!picking || !hover) { if (ghost) ghost.group.visible = false; if (ghost) ghost.label.style.display = "none"; return; }
    const src = [...markers.values()].find((m) => m.item.role === "new") || [...markers.values()].find((m) => m.item.role === "now");
    const b = src ? src.item.bounds : [[-20, -20, 0], [20, 20, 10]];
    const yaw = src ? src.item.anchor.yaw || 0 : 0;
    if (!ghost) {
      const group = new THREE.Group();
      group.add(padMesh(ROLE.ghost.color, b), ringMesh(ROLE.ghost.color, 30));
      markerGroup.add(group);
      ghost = { group, label: labelEl("role-ghost", "set the new spot here", "") };
    }
    ghost.group.visible = true;
    ghost.group.position.set(hover.p[0], hover.p[1], hover.p[2] + 0.2);
    ghost.group.rotation.z = yaw * Math.PI / 180;
    ghost.top = [hover.p[0], hover.p[1], hover.p[2] + 12];
    ghost.label.style.display = "";
    const tag = ghost.label.firstChild;
    if (!tag.querySelector("span")) tag.append(h("span"));
    tag.querySelector("span").textContent = `${hover.p[0].toFixed(1)}, ${hover.p[1].toFixed(1)}, ${hover.p[2].toFixed(1)}`;
  }

  // ---------------------------------------------------------------- input
  const onKey = (e, down) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    const engaged = locked || root.contains(document.activeElement) || root.matches(":hover");
    if (!engaged) return;
    const k = e.code;
    if (down) {
      if (k === "Digit1") { setMode("orbit"); e.preventDefault(); return; }
      if (k === "Digit2") { setMode("fly"); e.preventDefault(); return; }
      if (k === "Digit3") { setMode("walk"); e.preventDefault(); return; }
      if (k === "KeyF" && mode === "orbit") { focusItem(null, true); return; }
      if ((k === "KeyG" || k === "Slash") && !e.ctrlKey && !e.metaKey) { e.preventDefault(); searchIn.focus(); return; }
      if (k === "Backspace") { e.preventDefault(); back(); return; }
      if (k === "Home") { e.preventDefault(); flyTo([cityCenter[0], cityCenter[1], 0], 7000, 55); return; }
      if (k === "Equal" || k === "NumpadAdd") { e.preventDefault(); zoomBy(0.66); return; }
      if (k === "Minus" || k === "NumpadSubtract") { e.preventDefault(); zoomBy(1.5); return; }
      if (mode === "orbit" && !e.ctrlKey && !e.metaKey) {
        const vk = { KeyT: "top", KeyM: "map", KeyI: "iso", KeyL: "low" }[k];
        if (vk) { setView(vk); e.preventDefault(); return; }
        if (k === "KeyN") { northUp(); return; }
        if (k === "Comma") { turn(-45); return; }
        if (k === "Period") { turn(45); return; }
      }
      if (k === "KeyP" && opts.onPick) {
        if (mode !== "orbit" && hover) { opts.onPick({ x: +hover.p[0].toFixed(2), y: +hover.p[1].toFixed(2), z: +hover.p[2].toFixed(2) }); pulse(hover.p[0], hover.p[1]); }
        else setPick(!picking);
        return;
      }
      if (k === "Escape" && picking) { setPick(false); return; }
      if (mode === "orbit" && opts.onNudge) {
        const step = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
        const body = k === "ArrowLeft" ? { dx: -step } : k === "ArrowRight" ? { dx: step } : k === "ArrowUp" ? { dy: step } : k === "ArrowDown" ? { dy: -step }
          : k === "PageUp" ? { dz: step } : k === "PageDown" ? { dz: -step } : k === "KeyQ" ? { dyaw: e.shiftKey ? 45 : 5 } : k === "KeyE" ? { dyaw: e.shiftKey ? -45 : -5 } : null;
        if (body) { e.preventDefault(); opts.onNudge(body); return; }
      }
      if (mode === "orbit" && !opts.onNudge) {                 // nothing to nudge: the arrows turn and tilt the view
        const t = { ArrowLeft: () => turn(-15), ArrowRight: () => turn(15), ArrowUp: () => tilt(8), ArrowDown: () => tilt(-8) }[k];
        if (t) { e.preventDefault(); t(); return; }
      }
      if (k === "Space" && mode === "walk" && !e.repeat) {
        if (walk.onGround) { walk.vz = 6.2; walk.onGround = false; walk.jumps = 1; } else if (walk.jumps > 0) { walk.vz = 6.8; walk.jumps--; }
      }
      if (["KeyW", "KeyA", "KeyS", "KeyD", "Space", "KeyE", "KeyQ", "KeyC", "ControlLeft", "ShiftLeft", "ShiftRight", "AltLeft", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(k)) {
        if (mode !== "orbit" || ["KeyW", "KeyA", "KeyS", "KeyD"].includes(k)) e.preventDefault();
      }
      keys.add(k);
    } else {
      keys.delete(k);
    }
    poke();
  };
  const kd = (e) => onKey(e, true), ku = (e) => onKey(e, false);
  window.addEventListener("keydown", kd);
  window.addEventListener("keyup", ku);
  const blur = () => keys.clear();
  window.addEventListener("blur", blur);

  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    root.focus({ preventScroll: true });
    drag = { x: e.clientX, y: e.clientY, moved: 0, button: e.button };
    if (mode !== "orbit" && !locked && e.button === 0 && canvas.requestPointerLock) {
      try { const p = canvas.requestPointerLock({ unadjustedMovement: true }); if (p && p.catch) p.catch(() => canvas.requestPointerLock()); } catch (_) { canvas.requestPointerLock(); }
    }
    poke();
  });
  window.addEventListener("pointerup", onUp);
  function onUp(e) {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (d.moved < 5 && d.button === 0 && picking && hover && mode === "orbit") {
      opts.onPick({ x: +hover.p[0].toFixed(2), y: +hover.p[1].toFixed(2), z: +hover.p[2].toFixed(2) });
      pulse(hover.p[0], hover.p[1]);
      setPick(false);
    }
  }
  canvas.addEventListener("dblclick", () => {
    if (mode === "orbit" && hover) {
      const off = camera.position.distanceTo(controls.target);
      flyTo(hover.p, Math.max(120, Math.min(off * 0.6, 1600)));
    }
  });
  const onMove = (e) => {
    const r = root.getBoundingClientRect();
    if (locked) {
      fly.yaw -= e.movementX * 0.0022;
      fly.pitch = THREE.MathUtils.clamp(fly.pitch - e.movementY * 0.0022, -1.55, 1.55);
      hoverDirty = true;
      poke();
      return;
    }
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const inside = x >= 0 && y >= 0 && x <= r.width && y <= r.height;
    mouse = inside ? { x, y } : null;
    if (drag) {
      drag.moved += Math.abs(e.movementX) + Math.abs(e.movementY);
      if (mode !== "orbit" && drag.button === 0) {          // no pointer lock: drag to look
        fly.yaw -= e.movementX * 0.0035;
        fly.pitch = THREE.MathUtils.clamp(fly.pitch - e.movementY * 0.0035, -1.55, 1.55);
      }
    }
    cursorGlow.style.transform = `translate(${x}px, ${y}px)`;
    cursorGlow.classList.toggle("on", inside && mode === "orbit");
    hoverDirty = true;
    poke();
  };
  window.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerleave", () => { mouse = null; hoverDirty = true; cursorGlow.classList.remove("on"); });
  canvas.addEventListener("wheel", (e) => {
    if (mode === "orbit") return;            // OrbitControls zooms
    e.preventDefault();
    fly.speed = THREE.MathUtils.clamp(fly.speed * Math.exp(-e.deltaY * 0.0012), 3, 3000);
    prefs.speed = Math.round(fly.speed);
    savePrefs(prefs);
    poke();
  }, { passive: false });
  const onLock = () => {
    locked = document.pointerLockElement === canvas;
    lockHint.classList.toggle("hidden", mode === "orbit" || locked);
    root.classList.toggle("c3-locked", locked);
    keys.clear();
  };
  document.addEventListener("pointerlockchange", onLock);
  controls.addEventListener("change", () => { hoverDirty = true; poke(); });
  controls.addEventListener("start", () => { tween = null; });

  // ---------------------------------------------------------------- the frame
  let last = performance.now();
  let raf = 0;
  let frameTimes = [];
  let visible = true;
  const io = new IntersectionObserver((ents) => { visible = ents.some((x) => x.isIntersecting); if (visible) poke(); });
  io.observe(root);
  function poke() { idleUntil = performance.now() + 2500; if (!raf && !disposed) raf = requestAnimationFrame(frame); }

  const fwd = new THREE.Vector3(), right = new THREE.Vector3(), upv = new THREE.Vector3(0, 1, 0), tmp = new THREE.Vector3();
  function frame(now) {
    raf = 0;
    if (disposed) return;
    frames++;
    const dt = Math.min(0.1, Math.max(0.001, (now - last) / 1000));
    last = now;
    U.uTime.value += dt;
    if (tween) runTween(now);
    else if (mode === "orbit") orbitStep(dt);
    else if (mode === "fly") flyStep(dt);
    else walkStep(dt);
    if (mode !== "orbit") camera.rotation.set(fly.pitch, fly.yaw, 0);
    sky.position.copy(camera.position);
    // near plane: tight on the ground, loose high up
    const [cx, cy, cz] = t2g(camera.position);
    const gz = surfaceZ(cx, cy, cz + 0.5);
    const alt = cz - (Number.isFinite(gz) ? gz : -3);
    const near = mode === "walk" ? 0.1 : THREE.MathUtils.clamp(alt * 0.01, 0.2, 40);
    if (Math.abs(near - camera.near) > camera.near * 0.2) { camera.near = near; camera.updateProjectionMatrix(); }
    if (mode === "orbit") {
      const tg = t2g(controls.target);
      real.tick(now, { x: tg[0], y: tg[1], range: camera.position.distanceTo(controls.target), cam: [cx, cy, cz] });
    } else {
      real.tick(now, { x: cx, y: cy, range: Math.max(alt, 1), cam: [cx, cy, cz] });
    }
    const cut = real.cut();
    if (cut) U.uCut.value.set(cut.x, cut.y, cut.r, 1); else U.uCut.value.w = 0;
    scene.fog.density = U.uFogDensity.value;
    scene.fog.color.copy(U.uFogColor.value);
    if (hoverDirty || mode !== "orbit") updateHover();
    updateLabels();
    updateHud(alt, dt);
    if (visible && !document.hidden && !lost) {
      composer.render(dt);
      if (fullRate) adapt(dt);
    }
    const animating = tween || keys.size || drag || (mode !== "orbit") || U.uPulse.value.w > 0 && U.uTime.value - U.uPulse.value.z < 5;
    fullRate = !!(animating || performance.now() < idleUntil);
    if (raf) return;                                  // poked while drawing: already asked for
    if (fullRate) raf = requestAnimationFrame(frame);
    else setTimeout(() => { if (!raf && !disposed) raf = requestAnimationFrame(frame); }, 1000 / 24);   // idle: the city still breathes
  }
  let fullRate = false;
  let frames = 0;
  // dynamic resolution: hold ~60 fps on smaller GPUs
  let adaptAt = 0;
  function adapt(dt) {
    frameTimes.push(dt * 1000);
    if (frameTimes.length < 40 || performance.now() < adaptAt) return;
    const avg = frameTimes.reduce((a, b) => a + b, 0) / frameTimes.length;
    frameTimes = [];
    adaptAt = performance.now() + 1500;
    if (avg > 24 && dpr > 0.75) { dpr = Math.max(0.75, dpr - 0.25); applySize(); }
    else if (avg < 14 && dpr < dprMax) { dpr = Math.min(dprMax, dpr + 0.25); applySize(); }
    real.adapt(avg);
  }

  function runTween(now) {
    const t = Math.min(1, (now - tween.t0) / tween.dur);
    const k = ease(t);
    if (tween.sph) {                          // round the orbit's centre (a view, a turn)
      const { from, to } = tween.sph;
      const az = from.az + (to.az - from.az) * k, el = from.el + (to.el - from.el) * k, d = from.d + (to.d - from.d) * k;
      camera.position.copy(controls.target).add(tmp.set(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)).multiplyScalar(d));
      camera.lookAt(controls.target);
      if (t >= 1) { tween = null; controls.update(); lastOrbitD = d; }
      return;
    }
    camera.position.lerpVectors(tween.fromCam, tween.toCam, k);
    if (tween.arc) camera.position.y += Math.sin(Math.PI * k) * tween.arc;
    if (!tween.camOnly) {
      controls.target.lerpVectors(tween.fromTgt, tween.toTgt, k);
      camera.lookAt(controls.target);
    }
    if (t >= 1) {
      const done = tween.done;
      tween = null;
      if (mode === "orbit") controls.update();
      if (mode === "walk") { const [x, y, z] = t2g(camera.position); walk.feet = [x, y, z - EYE]; }
      if (done) done();
    }
  }

  function orbitStep(dt) {
    // WASD pans the target along the ground
    const speed = camera.position.distanceTo(controls.target) * 0.9 * (keys.has("ShiftLeft") ? 2.5 : 1);
    let mx = 0, mz = 0;
    if (keys.has("KeyW")) mz += 1;
    if (keys.has("KeyS")) mz -= 1;
    if (keys.has("KeyA")) mx -= 1;
    if (keys.has("KeyD")) mx += 1;
    if (mx || mz) {
      camera.getWorldDirection(fwd);
      fwd.y = 0; fwd.normalize();
      right.crossVectors(fwd, upv).normalize();
      tmp.set(0, 0, 0).addScaledVector(fwd, mz).addScaledVector(right, mx).normalize().multiplyScalar(speed * dt);
      controls.target.add(tmp);
      camera.position.add(tmp);
    }
    controls.update();
    if (prefs.autotilt) autoTilt(dt);
    // never under the city
    const [x, y, z] = t2g(camera.position);
    const gz = surfaceZ(x, y);
    const minZ = (Number.isFinite(gz) ? gz : -3) + 6;
    if (z < minZ) camera.position.y = minZ;
  }

  function flyStep(dt) {
    let f = 0, s = 0, u = 0;
    if (keys.has("KeyW") || keys.has("ArrowUp")) f += 1;
    if (keys.has("KeyS") || keys.has("ArrowDown")) f -= 1;
    if (keys.has("KeyD") || keys.has("ArrowRight")) s += 1;
    if (keys.has("KeyA") || keys.has("ArrowLeft")) s -= 1;
    if (keys.has("Space") || keys.has("KeyE")) u += 1;
    if (keys.has("KeyC") || keys.has("KeyQ") || keys.has("ControlLeft")) u -= 1;
    const boost = keys.has("ShiftLeft") || keys.has("ShiftRight") ? 5 : 1;
    const slow = keys.has("AltLeft") ? 0.15 : 1;
    camera.rotation.set(fly.pitch, fly.yaw, 0);
    camera.getWorldDirection(fwd);
    right.crossVectors(fwd, upv).normalize();
    tmp.set(0, 0, 0).addScaledVector(fwd, f).addScaledVector(right, s).addScaledVector(upv, u);
    if (tmp.lengthSq() > 0) tmp.normalize().multiplyScalar(fly.speed * boost * slow);
    fly.vel.lerp(tmp, 1 - Math.exp(-dt * 6));
    camera.position.addScaledVector(fly.vel, dt);
    const [x, y, z] = t2g(camera.position);
    const gz = surfaceZ(x, y, z + 1);
    const minZ = (Number.isFinite(gz) ? gz : -3) + 1.2;
    if (z < minZ) camera.position.y = minZ;
  }

  function walkStep(dt) {
    let f = 0, s = 0;
    if (keys.has("KeyW") || keys.has("ArrowUp")) f += 1;
    if (keys.has("KeyS") || keys.has("ArrowDown")) f -= 1;
    if (keys.has("KeyD") || keys.has("ArrowRight")) s += 1;
    if (keys.has("KeyA") || keys.has("ArrowLeft")) s -= 1;
    const run = keys.has("ShiftLeft") || keys.has("ShiftRight");
    const speed = run ? 9.5 : 4.4;
    // forward on the ground plane, in game axes
    const fx = -Math.sin(fly.yaw), fy = Math.cos(fly.yaw);       // three -z is north
    const rx = fy, ry = -fx;
    let wx = fx * f + rx * s, wy = fy * f + ry * s;
    const n = Math.hypot(wx, wy);
    if (n > 0) { wx = wx / n * speed; wy = wy / n * speed; }
    const ctl = walk.onGround ? 12 : 2.5;
    walk.vx += (wx - walk.vx) * (1 - Math.exp(-dt * ctl));
    walk.vy += (wy - walk.vy) * (1 - Math.exp(-dt * ctl));
    walk.vz -= GRAVITY * dt;
    let [x, y, z] = walk.feet;
    let nx = x + walk.vx * dt, ny = y + walk.vy * dt, nz = z + walk.vz * dt;
    // walls: the buildings around, pushed out of sideways (a step up is climbed)
    for (const b of (inCut(nx, ny) ? [] : boxes.near(nx, ny, RADIUS + 1, nearTmp))) {
      const top = boxes.topAt(b, nx, ny);
      const l = boxes.local(b, nx, ny, nz + 0.9);
      const o = b * 10, R = boxes.R, r = b * 9;
      // a box axis is "horizontal" when its world direction (column k of R) lies flat
      const flat = [Math.abs(R[r + 6]) < 0.5, Math.abs(R[r + 7]) < 0.5, Math.abs(R[r + 8]) < 0.5];
      const he = [0, 1, 2].map((k) => B[o + 3 + k] + (flat[k] ? RADIUS : 0.9));
      if (Math.abs(l[0]) > he[0] || Math.abs(l[1]) > he[1] || Math.abs(l[2]) > he[2]) continue;
      const roof = Number.isFinite(top) ? top : B[o + 2] + boxes.E[b * 3 + 2];
      if (nz >= roof - STEP) continue;        // on it, or stepping onto it
      // out along the horizontal box axis that needs the least push
      let ax = -1, pen = Infinity;
      for (let k = 0; k < 3; k++) {
        if (!flat[k]) continue;
        const pk = he[k] - Math.abs(l[k]);
        if (pk < pen) { pen = pk; ax = k; }
      }
      if (ax < 0) continue;
      const sgn = Math.sign(l[ax]) || 1;
      const wx = R[r + ax], wy = R[r + 3 + ax];
      const wn = Math.hypot(wx, wy) || 1;
      nx += wx / wn * sgn * (pen + 0.001);
      ny += wy / wn * sgn * (pen + 0.001);
      // keep the speed along the wall, drop the part into it
      const into = (walk.vx * wx + walk.vy * wy) / wn * sgn;
      if (into < 0) { walk.vx -= wx / wn * sgn * into; walk.vy -= wy / wn * sgn * into; }
    }
    const gz = surfaceZ(nx, ny, Math.max(z, nz) + STEP);
    const floor = Number.isFinite(gz) ? Math.max(gz, -3) : -3;
    if (nz <= floor) {
      nz = floor;
      walk.vz = 0;
      walk.onGround = true;
      walk.jumps = 1;
    } else {
      walk.onGround = nz - floor < 0.05;
    }
    walk.feet = [nx, ny, nz];
    const bob = walk.onGround && n > 0 ? Math.sin(U.uTime.value * (run ? 14 : 9)) * (run ? 0.05 : 0.03) : 0;
    g2t(nx, ny, nz + EYE + bob, camera.position);
  }

  function updateHover() {
    hoverDirty = false;
    let hit = null;
    if (mode === "orbit" && mouse && !drag) hit = rayFromScreen(mouse.x, mouse.y);
    else if (mode !== "orbit") {
      camera.getWorldDirection(fwd);
      const [x, y, z] = t2g(camera.position);
      const [dx, dy, dz] = t2g(fwd);
      hit = raycast(x, y, z, dx, dy, dz, 5000);
    }
    hover = hit;
    hoverUniform.value = hit && hit.kind === "building" ? hit.box : -1;
    if (hit && hit.kind !== "building" && mode === "orbit") U.uCursor.value.set(hit.p[0], hit.p[1], 1);
    else U.uCursor.value.z = 0;
    updateGhost();
  }

  const proj = new THREE.Vector3();
  function placeLabel(e, p, maxDist, minDist = 0) {
    g2t(p[0], p[1], p[2], proj);
    const d = proj.distanceTo(camera.position);
    proj.project(camera);
    if (proj.z > 1 || d > maxDist || d < minDist || Math.abs(proj.x) > 1.2 || Math.abs(proj.y) > 1.2) { e.style.display = "none"; return false; }
    e.style.display = "";
    e.style.transform = `translate(${((proj.x + 1) / 2 * W).toFixed(1)}px, ${((1 - proj.y) / 2 * H).toFixed(1)}px)`;
    e.style.opacity = String(THREE.MathUtils.clamp(1 - (d - maxDist * 0.7) / (maxDist * 0.3), 0, 1));
    return true;
  }
  function updateLabels() {
    if (!prefs.labels) return;
    const k = lensK(camera.fov);              // distances as seen through the lens
    for (const mk of markers.values()) placeLabel(mk.label, mk.top, 30000 * k);
    for (const mk of playerMarks.values()) placeLabel(mk.label, mk.top, 9000 * k);
    if (ghost && ghost.top && ghost.group.visible) placeLabel(ghost.label, ghost.top, 30000 * k);
    const [, , cz] = t2g(camera.position);
    for (const { d, e } of districtLabels) {
      if (!prefs.districts || mode === "walk") { e.style.display = "none"; continue; }
      if (d.top) placeLabel(e, [d.center[0], d.center[1], d.center[2] + 60], 16000 * k, (mode === "walk" ? 600 : 400) * k);
      else placeLabel(e, [d.center[0], d.center[1], d.center[2] + 20], cz > 2500 * k ? 0 : 4200 * k, 80 * k);
    }
  }

  let hudAt = 0;
  let lastDistrict = "";
  function updateHud(alt, dt) {
    const now = performance.now();
    if (now - hudAt < 80) return;
    hudAt = now;
    const [x, y, z] = t2g(camera.position);
    const look = hover ? hover.p : null;
    const heading = mode === "orbit"
      ? (() => { const o = camera.getWorldDirection(fwd); return (Math.atan2(o.x, -o.z) * 180 / Math.PI + 360) % 360; })()
      : ((-fly.yaw * 180 / Math.PI) % 360 + 360) % 360;
    drawCompass(heading);
    clear(coords);
    coords.append(
      h("div", h("span", "CAM"), `${x.toFixed(0)}, ${y.toFixed(0)}, ${z.toFixed(0)}`),
      h("div", h("span", "ALT"), `${alt.toFixed(alt < 100 ? 1 : 0)} m`));
    if (look) coords.append(h("div", h("span", hover.kind === "building" ? "BLDG" : "AIM"), `${look[0].toFixed(1)}, ${look[1].toFixed(1)}, ${look[2].toFixed(1)}`));
    const v = mode === "fly" ? fly.vel.length() : mode === "walk" ? Math.hypot(walk.vx, walk.vy) : 0;
    speedEl.textContent = mode === "orbit" ? "" : `${(v * 3.6).toFixed(0)} km/h${mode === "fly" ? ` · cruise ${Math.round(fly.speed)} m/s` : ""}`;
    const tg = t2g(controls.target);
    const at = mode === "orbit" ? (look || tg) : [x, y];
    const { top, sub } = districtAt(at[0], at[1]);
    const label = top ? (sub ? `${sub.label} · ${top.label}` : top.label) : "Badlands";
    if (label !== lastDistrict) {
      lastDistrict = label;
      clear(where).append(h("span.c3-where-k", mode === "orbit" ? "OVER" : "IN"), h("b", label.toUpperCase()));
      for (const [id, line] of districtLines) line.material.uniforms.uIntensity.value = line.userData.base * ((top && top.id === id) || (sub && sub.id === id) ? 2.2 : 1);
    }
  }
  const cg = compass.getContext("2d");
  function drawCompass(hd) {
    const w = 300, hh = 25;
    cg.setTransform(2, 0, 0, 2, 0, 0);
    cg.clearRect(0, 0, w, hh);
    cg.font = "10px 'IBM Plex Mono', monospace";
    cg.textAlign = "center";
    const span = 120;            // degrees across
    for (let a = Math.floor((hd - span / 2) / 5) * 5; a <= hd + span / 2; a += 5) {
      const px = w / 2 + (a - hd) / span * w;
      const n = ((a % 360) + 360) % 360;
      const major = n % 45 === 0;
      cg.fillStyle = major ? "rgba(158,243,237,0.95)" : "rgba(158,243,237,0.35)";
      cg.fillRect(px, major ? 4 : 8, 1, major ? 8 : 4);
      if (major) {
        const lbl = { 0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW" }[n];
        cg.fillStyle = n === 0 ? "#ffd23f" : "rgba(220,250,255,0.9)";
        cg.fillText(lbl, px, 22);
      }
    }
    cg.fillStyle = "#ffd23f";
    cg.beginPath(); cg.moveTo(w / 2 - 5, 0); cg.lineTo(w / 2 + 5, 0); cg.lineTo(w / 2, 5); cg.fill();
    cg.fillStyle = "rgba(220,250,255,0.8)";
    cg.textAlign = "right";
    cg.fillText(`${Math.round(hd).toString().padStart(3, "0")}°`, w - 2, 22);
  }

  // ---------------------------------------------------------------- the API
  function update(sceneIn) {
    for (const mk of markers.values()) mk.seen = false;
    for (const it of sceneIn.items || []) if (it.anchor) setItem(it);
    for (const [k, mk] of markers) {
      if (!mk.seen) { markerGroup.remove(mk.group); disposeObj(mk.group); mk.label.remove(); markers.delete(k); }
    }
    for (const mk of playerMarks.values()) mk.seen = false;
    for (const p of sceneIn.players || []) if (Number.isFinite(p.x)) setPlayer(p);
    for (const [k, mk] of playerMarks) {
      if (!mk.seen) { markerGroup.remove(mk.group); disposeObj(mk.group); mk.label.remove(); playerMarks.delete(k); }
    }
    focusKey = sceneIn.focus || null;
    if (focusKey && focusDone !== focusKey && findFocus(focusKey)) {
      focusItem(focusKey, focusDone !== null);
      focusDone = focusKey;
    }
    poke();
  }

  setMode("orbit", true);
  markView(prefs.autotilt ? "map" : prefs.view);
  poke();
  // for tests and the curious: the state of the view
  root.c3update = update;
  // (tests: put the orbit at { x, y, z, az, el, d } at once)
  root.c3go = (g) => {
    if (mode !== "orbit") setMode("orbit");
    tween = null;
    g2t(g.x, g.y, g.z || 0, controls.target);
    const az = THREE.MathUtils.degToRad(g.az || 0), el = THREE.MathUtils.degToRad(g.el == null ? 58 : g.el);
    camera.position.copy(controls.target).add(new THREE.Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)).multiplyScalar(g.d || 1000));
    controls.update();
    lastOrbitD = sphOf().d;
    poke();
  };
  root.c3state = () => ({ mode, keys: [...keys], locked, tween: !!tween, cam: t2g(camera.position), vel: fly.vel.toArray(), yaw: fly.yaw, pitch: fly.pitch,
    orbit: (() => { const o = sphOf(); return { az: o.az * 180 / Math.PI, el: o.el * 180 / Math.PI, d: o.d }; })(), fov: camera.fov, look: prefs.look,
    blocks: blockSets.reduce((a, b) => a + b.mesh.count, 0), blockSets: blockSets.length, real: real.state(),
    feet: walk.feet.slice(), wv: [walk.vx, walk.vy, walk.vz], onGround: walk.onGround, frames, hover: hover ? { kind: hover.kind, p: hover.p } : null });

  function dispose() {
    disposed = true;
    cancelAnimationFrame(raf);
    ro.disconnect();
    io.disconnect();
    window.removeEventListener("keydown", kd);
    window.removeEventListener("keyup", ku);
    window.removeEventListener("blur", blur);
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    document.removeEventListener("pointerlockchange", onLock);
    document.removeEventListener("click", closeMenu);
    if (locked) document.exitPointerLock();
    controls.dispose();
    real.dispose();
    scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    for (const b of blockSets) { for (const m of [b.game, b.neon, b.holo]) m.dispose(); if (b.tex) b.tex.dispose(); }
    composer.dispose && composer.dispose();
    rt.dispose();
    renderer.dispose();
    canvas.removeEventListener("webglcontextlost", onLost);
    try { renderer.forceContextLoss(); } catch (_) { /* already gone */ }   // give the context back now, not at GC
    canvas.remove();
    hud.remove();
    labelLayer.remove();
    cursorGlow.remove();
  }

  return { update, dispose, focusItem: (k) => focusItem(k, true), setMode };
}
