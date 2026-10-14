// The home page's centrepiece: Night City as a hologram on a projector pad -- its ~10,000
// buildings, streets and NCART lines, turning slowly, leaning towards the cursor.  The data is the
// 3D city of Live maps (the game's own world map, read once from the player's game); without it
// the caller keeps its planet.  30 fps at most, and nothing runs off screen or with effects off.
import * as THREE from "../../vendor/three/three.module.min.js";
import { api } from "../api.js";
import { fxLevel, onFxLevel } from "../fx.js";
import { loadCity, view } from "./citydata.js";
import * as S from "./cityshaders.js";
import { diag } from "../diag.js";

const PAD_FS = S.MATH + /* glsl */ `
uniform float uTime; varying vec2 vUv;
void main() {
  vec2 p = vUv * 2.0 - 1.0;
  float r = length(p);
  if (r > 1.0) discard;
  float a = atan(p.y, p.x);
  float rings = exp(-sq((r - 0.98) / 0.012)) * 1.2 + exp(-sq((r - 0.86) / 0.006)) * 0.6
    + exp(-sq((r - fract(uTime * 0.18)) / 0.01)) * 0.45 * (1.0 - fract(uTime * 0.18));
  float ticks = step(0.92, r) * step(r, 0.97) * step(0.75, fract(a / 6.2831853 * 72.0));
  float sweep = smoothstep(0.0, 1.0, 1.0 - fract((a / 6.2831853) - uTime * 0.12)) * step(r, 0.86) * 0.10;
  float grid = (1.0 - smoothstep(0.0, 0.05, abs(fract(p.x * 12.0) - 0.5) * 2.0 - 0.9)) * 0.0;
  vec3 col = vec3(0.38, 0.95, 0.93) * (rings + ticks * 0.5 + sweep) + vec3(1.0, 0.31, 0.64) * exp(-sq((r - 0.74) / 0.004)) * 0.5;
  col += vec3(0.1, 0.5, 0.55) * (1.0 - r) * 0.08 + grid;
  gl_FragColor = vec4(col, 1.0);
}`;
const PAD_VS = /* glsl */ `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

export async function cityHologram(host, { onOpen } = {}) {
  let st;
  try { st = await api.get("/citymap"); } catch (_) { return null; }
  if (!st || !st.ready) return null;
  const data = await loadCity(st.key);
  const { meta, buf } = data;
  const wrap = document.createElement("div");
  wrap.className = "city-holo";
  wrap.title = "Night City in 3D: open Live maps";
  const tag = document.createElement("span");
  tag.className = "tag-line";
  tag.textContent = "NIGHT CITY / LIVE 3D";
  wrap.append(tag);
  host.append(wrap);
  wrap.addEventListener("click", () => (onOpen ? onOpen() : (location.hash = "#/live")));

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  wrap.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 10, 80000);
  const U = { uTime: { value: 0 }, uFogColor: { value: new THREE.Color(0x000000) }, uFogDensity: { value: 0 },
    uPulse: { value: new THREE.Vector4(0, 0, -100, 0) }, uCursor: { value: new THREE.Vector3() }, uPxScale: { value: 0.001 } };
  const additive = { transparent: true, depthWrite: false, blending: THREE.AdditiveBlending };
  const mat = (vs, fs, extra = {}, more = {}) => new THREE.ShaderMaterial(Object.assign({ vertexShader: vs, fragmentShader: fs, uniforms: Object.assign({}, U, extra) }, more));

  // the city on its pad, centred on its buildings; the pad holds 96 % of them
  const B = meta.buildings ? view(buf, meta.buildings.data, Float32Array) : new Float32Array(0);
  const n = B.length / 10;
  let cx = 0, cy = 0;
  for (let b = 0; b < n; b++) { cx += B[b * 10]; cy += B[b * 10 + 1]; }
  if (n) { cx /= n; cy /= n; } else { cx = (meta.city.min[0] + meta.city.max[0]) / 2; cy = (meta.city.min[1] + meta.city.max[1]) / 2; }
  const radii = [];
  for (let b = 0; b < n; b++) radii.push(Math.hypot(B[b * 10] - cx, B[b * 10 + 1] - cy));
  radii.sort((a, b) => a - b);
  const R = (radii.length ? radii[Math.floor(radii.length * 0.96)] : 4000) * 1.08;
  const pivot = new THREE.Group();            // turns about the up axis
  const city = new THREE.Group();
  city.rotation.x = -Math.PI / 2;
  city.scale.set(1, 1, 2.4);                  // heights stretched: a table model reads better
  city.position.set(-cx, 0, cy);              // game (cx, cy) at the pivot
  pivot.add(city);
  scene.add(pivot);
  const L = meta.layers;
  // a layer cut to the pad's disc
  const geo = (name) => {
    const e = L[name];
    if (!e) return null;
    const pos = view(buf, e.pos, Float32Array), idx = view(buf, e.idx, Uint32Array);
    const keep = [];
    for (let i = 0; i < idx.length; i += 3) {
      const a = idx[i] * 3;
      if (Math.hypot(pos[a] - cx, pos[a + 1] - cy) < R * 0.985) keep.push(idx[i], idx[i + 1], idx[i + 2]);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setIndex(new THREE.BufferAttribute(new Uint32Array(keep), 1));
    if (e.col) g.setAttribute("aCol", new THREE.BufferAttribute(view(buf, e.col, Uint8Array), 4, true));
    return g;
  };
  const roads = geo("roads");
  if (roads) {
    const m = new THREE.Mesh(roads, mat(S.glowVS, S.roadFS, { uLift: { value: 2 }, uColor: { value: new THREE.Color(0x14c8ff) },
      uColor2: { value: new THREE.Color(0xff3d7a) }, uIntensity: { value: 0.9 } }, additive));
    m.frustumCulled = false;
    city.add(m);
  }
  const metro = geo("metro");
  if (metro) {
    const m = new THREE.Mesh(metro, mat(S.glowVS, S.metroFS, { uLift: { value: 3 }, uIntensity: { value: 1.4 } }));
    m.frustumCulled = false;
    city.add(m);
  }
  if (n) {
    const g = new THREE.BoxGeometry(1, 1, 1);
    const size = new Float32Array(n * 3), tint = new Float32Array(n * 3), seed = new Float32Array(n), ids = new Float32Array(n).fill(-5);
    const mesh = new THREE.InstancedMesh(g, null, n);
    const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3();
    for (let b = 0; b < n; b++) {
      const o = b * 10;
      if (Math.hypot(B[o] - cx, B[o + 1] - cy) > R * 0.985) { m4.makeScale(0, 0, 0); mesh.setMatrixAt(b, m4); continue; }
      p.set(B[o], B[o + 1], B[o + 2]);
      s.set(B[o + 3] * 2, B[o + 4] * 2, B[o + 5] * 2);
      q.set(B[o + 6], B[o + 7], B[o + 8], B[o + 9]).normalize();
      m4.compose(p, q, s);
      mesh.setMatrixAt(b, m4);
      size.set([s.x, s.y, s.z], b * 3);
      tint.set([0.62, 0.95, 0.93], b * 3);
      seed[b] = ((Math.sin(b * 12.9898) * 43758.5453) % 1 + 1) % 1;
    }
    g.setAttribute("aSize", new THREE.InstancedBufferAttribute(size, 3));
    g.setAttribute("aTint", new THREE.InstancedBufferAttribute(tint, 3));
    g.setAttribute("aSeed", new THREE.InstancedBufferAttribute(seed, 1));
    g.setAttribute("aId", new THREE.InstancedBufferAttribute(ids, 1));
    mesh.material = mat(S.buildingVS, S.buildingFS, { uHover: { value: -1 }, uHolo: { value: 1 }, uTint: { value: 0 } }, additive);
    mesh.frustumCulled = false;
    mesh.instanceMatrix.needsUpdate = true;
    city.add(mesh);
  }
  // the projector pad under it
  const pad = new THREE.Mesh(new THREE.PlaneGeometry(R * 2, R * 2), new THREE.ShaderMaterial({ vertexShader: PAD_VS, fragmentShader: PAD_FS,
    uniforms: { uTime: U.uTime }, ...additive, side: THREE.DoubleSide }));
  pad.rotation.x = -Math.PI / 2;
  pad.position.y = -40;
  scene.add(pad);

  let W = 1, H = 1;
  function resize() {
    const r = wrap.getBoundingClientRect();
    if (!r.width || !r.height) return;
    W = r.width; H = r.height;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setSize(W, H, false);
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
    U.uPxScale.value = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) / H;
    draw(performance.now());
  }
  let tx = 0, ty = 0, px = 0, py = 0;
  const onMove = (e) => {
    const r = wrap.getBoundingClientRect();
    tx = ((e.clientX - r.left) / Math.max(1, r.width) - 0.5) * 2;
    ty = ((e.clientY - r.top) / Math.max(1, r.height) - 0.5) * 2;
  };
  window.addEventListener("pointermove", onMove, { passive: true });
  const dist = R * 2.55;
  function draw(now) {
    const animate = fxLevel() === "full";
    U.uTime.value = now / 1000;
    px += (tx - px) * 0.05; py += (ty - py) * 0.05;
    pivot.rotation.y = (animate ? now / 1000 * 0.06 : 0.6) + px * 0.25;
    const el = THREE.MathUtils.degToRad(31 + py * 6);
    camera.position.set(0, Math.sin(el) * dist, Math.cos(el) * dist);
    camera.lookAt(0, -R * 0.05, 0);
    renderer.render(scene, camera);
  }
  let raf = 0, last = 0, visible = true, disposed = false, lost = false;
  // a driver reset: three.js restores the context and its resources by itself; draw nothing meanwhile
  renderer.domElement.addEventListener("webglcontextlost", (e) => { e.preventDefault(); if (disposed) return; lost = true; diag("webgl", "home hologram: the graphics context was lost"); });
  renderer.domElement.addEventListener("webglcontextrestored", () => { lost = false; if (!disposed && !raf) raf = requestAnimationFrame(loop); });
  function loop(now) {
    raf = 0;
    if (disposed) return;
    if (visible && !document.hidden && !lost && now - last > 33) { draw(now); last = now; }
    if (fxLevel() === "full" && visible) raf = requestAnimationFrame(loop);
  }
  const io = new IntersectionObserver((ents) => { visible = ents.some((x) => x.isIntersecting); if (visible && !raf) raf = requestAnimationFrame(loop); });
  io.observe(wrap);
  const ro = new ResizeObserver(resize);
  ro.observe(wrap);
  const offLevel = onFxLevel(() => { if (!raf) raf = requestAnimationFrame(loop); draw(performance.now()); });
  const onVis = () => { if (!document.hidden && !raf) raf = requestAnimationFrame(loop); };
  document.addEventListener("visibilitychange", onVis);
  resize();
  raf = requestAnimationFrame(loop);
  return {
    el: wrap,
    dispose() {
      disposed = true;
      cancelAnimationFrame(raf);
      io.disconnect(); ro.disconnect(); offLevel();
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("visibilitychange", onVis);
      scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      renderer.dispose();
      try { renderer.forceContextLoss(); } catch (_) { /* already gone */ }   // give the context back now
      wrap.remove();
    },
  };
}
