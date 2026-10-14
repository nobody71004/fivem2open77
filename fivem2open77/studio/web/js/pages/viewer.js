// 3D preview of the car as the conversion assembles it (three.js).
import * as THREE from "../../vendor/three/three.module.min.js";
import { GLTFLoader } from "../../vendor/three/GLTFLoader.js";
import { OrbitControls } from "../../vendor/three/OrbitControls.js";
import { h, clear, fmtBytes } from "../ui.js";

const KIND_COLORS = { body: 0x8c9199, interior: 0x403833, glass: 0x8cb3d9, door: 0x338cf2, window: 0x80bfff, hood: 0xf29926,
  trunk: 0xf25933, wheel: 0xbfbfc7, tire: 0x141414, hub: 0x808080, lights: 0xfff299, steering: 0x9933cc, neon: 0xff0dd9 };

export function viewerTab(root, ctx) {
  const list = h("div.partlist");
  const info = h("div.card", { style: { marginTop: "10px", fontSize: "13px" } }, h("span.faint", "Click a part to see what it is."));
  const stage = h("div.stage");
  const loading = h("div.loading", "Loading the 3D model...");
  stage.append(loading);
  const colorBy = h("input", { type: "checkbox" });
  const wire = h("input", { type: "checkbox" });
  const spin = h("input", { type: "checkbox" });
  const overlay = h("div.overlay",
    h("label.row.pill", colorBy, "colour by part"), h("label.row.pill", wire, "wireframe"), h("label.row.pill", spin, "turn"),
    h("div.grow"), h("button.btn.small", { onclick: () => frame() }, "Reset view"));
  stage.append(overlay);
  root.append(h("div.viewer", h("div.side", h("h4", "Parts"), list, info), stage));

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  stage.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.05, 200);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x20232b, 1.3));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(4, 8, 6);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0x9fc7ff, 0.8);
  fill.position.set(-6, 3, -4);
  scene.add(fill);
  let grid = null;
  let model = null;
  const nodes = [];      // { obj, name, kind, materials }
  let selected = null;
  let disposed = false;

  function resize() {
    const r = stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    renderer.setSize(r.width, r.height, false);
    camera.aspect = r.width / r.height;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(stage);

  function frame() {
    if (!model) return;
    const box = new THREE.Box3().setFromObject(model);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const d = Math.max(size.x, size.y, size.z) * 1.35;
    camera.position.set(center.x + d * 0.75, center.y + d * 0.42, center.z + d * 0.85);
    controls.target.copy(center);
    controls.update();
  }

  function applyLook() {
    for (const n of nodes) {
      n.obj.traverse((o) => {
        if (!o.isMesh) return;
        o.material = colorBy.checked ? n.kindMat : n.materials.get(o);
        if (o.material) o.material.wireframe = wire.checked;
      });
    }
    highlight();
  }

  function highlight() {
    for (const n of nodes) {
      const sel = selected === n;
      n.obj.traverse((o) => {
        if (!o.isMesh || !o.material || !o.material.emissive) return;
        if (sel) {
          if (!o.userData.hl) { o.userData.hl = true; o.material = o.material.clone(); }
          o.material.emissive.setHex(0x665c00);
        } else if (o.userData.hl) {
          o.userData.hl = false;
          o.material = colorBy.checked ? n.kindMat : n.materials.get(o);
          if (o.material) o.material.wireframe = wire.checked;
        }
      });
    }
    for (const el of list.querySelectorAll("label")) el.classList.toggle("sel", selected && el.dataset.name === selected.name);
  }

  function select(n) {
    selected = n;
    highlight();
    clear(info);
    if (!n) { info.append(h("span.faint", "Click a part to see what it is.")); return; }
    const part = ctx.inspect.parts[n.part] || {};
    info.append(h("h3", h("code", n.name)), h("div.kv",
      h("span", "kind"), h("span", n.kind), h("span", "vertices"), h("span", part.verts ?? "-"),
      h("span", "GTA bones"), h("span.mono", (part.gta_bones || []).join(", ") || "-")),
      h("div.row", { style: { marginTop: "10px" } }, h("a.btn.small", { href: `#/project/${ctx.pid}/parts` }, "Change mapping")));
  }

  colorBy.addEventListener("change", applyLook);
  wire.addEventListener("change", applyLook);

  const ray = new THREE.Raycaster();
  renderer.domElement.addEventListener("click", (e) => {
    if (!model) return;
    const r = renderer.domElement.getBoundingClientRect();
    const p = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(p, camera);
    const hits = ray.intersectObjects(nodes.filter((n) => n.obj.visible).map((n) => n.obj), true);
    if (!hits.length) return select(null);
    let o = hits[0].object;
    while (o && !nodes.find((n) => n.obj === o)) o = o.parent;
    select(nodes.find((n) => n.obj === o) || null);
  });

  const t0 = performance.now();
  new GLTFLoader().load(`/api/projects/${ctx.pid}/preview.glb?v=${ctx.version}`, (gltf) => {
    if (disposed) return;
    model = gltf.scene;
    scene.add(model);
    for (const child of [...model.children]) {
      const ex = child.userData || {};
      const kind = ex.kind || "body";
      const materials = new Map();
      child.traverse((o) => { if (o.isMesh) materials.set(o, o.material); });
      const kindMat = new THREE.MeshStandardMaterial({ color: KIND_COLORS[kind] ?? 0x999999, roughness: 0.55, metalness: 0.1 });
      nodes.push({ obj: child, name: child.name, part: ex.part || child.name, kind, materials, kindMat });
    }
    const box = new THREE.Box3().setFromObject(model);
    grid = new THREE.GridHelper(12, 24, 0x3a4256, 0x252a36);
    grid.position.y = box.min.y;
    scene.add(grid);
    clear(list);
    const byKind = {};
    for (const n of nodes) (byKind[n.kind] = byKind[n.kind] || []).push(n);
    for (const kind of Object.keys(byKind).sort()) {
      for (const n of byKind[kind]) {
        const cb = h("input", { type: "checkbox", checked: true, onclick: (e) => e.stopPropagation(),
          onchange: () => { n.obj.visible = cb.checked; } });
        const verts = (ctx.inspect.parts[n.part] || {}).verts;
        list.append(h("label", { "data-name": n.name, onclick: (e) => { if (e.target !== cb) { e.preventDefault(); select(n); } } }, cb,
          h("span.sw", { style: { background: "#" + (KIND_COLORS[n.kind] ?? 0x999999).toString(16).padStart(6, "0") } }),
          h("span", n.name), h("span.n", verts ? `${(verts / 1000).toFixed(1)}k` : "")));
      }
    }
    loading.remove();
    resize();
    frame();
    overlay.prepend(h("span.pill", `${nodes.length} parts · ${((performance.now() - t0) / 1000).toFixed(1)} s`));
  }, (xhr) => {
    if (xhr.total) loading.textContent = `Loading the 3D model... ${fmtBytes(xhr.loaded)} / ${fmtBytes(xhr.total)}`;
  }, (err) => {
    loading.textContent = "Could not load the 3D model: " + (err.message || err);
  });

  function tick() {
    if (disposed) return;
    if (spin.checked && model) model.rotation.y += 0.004;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  ctx.disposers.push(() => {
    disposed = true;
    ro.disconnect();
    renderer.dispose();
    try { renderer.forceContextLoss(); } catch (_) { /* already gone */ }
    scene.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
    });
  });
}
