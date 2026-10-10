// 3D view of a map as the converter builds it (three.js): every part where the build places it,
// textured, in the map's own frame (the anchor at the origin, north away from the start view).
import * as THREE from "../../vendor/three/three.module.min.js";
import { GLTFLoader } from "../../vendor/three/GLTFLoader.js";
import { OrbitControls } from "../../vendor/three/OrbitControls.js";
import { h, clear, fmtBytes } from "../ui.js";

const KIND_COLORS = { mlo: 0xc7b89e, object: 0x94a885, chunk: 0x808c78, door: 0x338cf2 };
const KIND_LABELS = { mlo: "interiors (MLO)", object: "big objects", chunk: "cells (small objects, terrain)", door: "doors and gates" };

export function mapViewerTab(root, ctx) {
  const list = h("div.partlist");
  const info = h("div.card", { style: { marginTop: "10px", fontSize: "13px" } }, h("span.faint", "Click a piece of the map to see what it is."));
  const stage = h("div.stage");
  const loading = h("div.loading", "Loading the 3D map...");
  stage.append(loading);
  const colorBy = h("input", { type: "checkbox" });
  const wire = h("input", { type: "checkbox" });
  const overlay = h("div.overlay",
    h("label.row.pill", colorBy, "colour by kind"), h("label.row.pill", wire, "wireframe"),
    h("div.grow"), h("button.btn.small", { onclick: () => frame("iso") }, "3/4"), h("button.btn.small", { onclick: () => frame("top") }, "From above"));
  stage.append(overlay);
  root.append(h("div.viewer", h("div.side", h("h4", "Pieces"), list, info), stage));

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance", logarithmicDepthBuffer: true });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  stage.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.3, 30000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.screenSpacePanning = true;
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a2620, 1.5));
  const sun = new THREE.DirectionalLight(0xffffff, 2.0);
  sun.position.set(300, 600, 200);
  scene.add(sun);
  let model = null;
  let needs = true;       // render on the next frame
  const nodes = [];       // { obj, part, kind, source, position, yaw, materials }
  const kindMats = {};
  let selected = null;
  let disposed = false;

  function resize() {
    const r = stage.getBoundingClientRect();
    if (!r.width || !r.height) return;
    renderer.setSize(r.width, r.height, false);
    camera.aspect = r.width / r.height;
    camera.updateProjectionMatrix();
    needs = true;
  }
  const ro = new ResizeObserver(resize);
  ro.observe(stage);

  function frame(how = "iso") {
    if (!model) return;
    const box = new THREE.Box3();
    for (const n of nodes) if (n.obj.visible) box.expandByObject(n.obj);
    if (box.isEmpty()) box.setFromObject(model);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const d = Math.max(size.x, size.z, size.y * 0.6) * 1.1;
    if (how === "top") camera.position.set(center.x, center.y + d * 1.4, center.z + 0.01);
    else camera.position.set(center.x - d * 0.55, center.y + d * 0.65, center.z - d * 0.75);
    controls.target.copy(center);
    controls.update();
    needs = true;
  }

  function applyLook() {
    for (const n of nodes) {
      n.obj.traverse((o) => {
        if (!o.isMesh) return;
        o.material = colorBy.checked ? kindMats[n.kind] : n.materials.get(o);
        if (o.material) o.material.wireframe = wire.checked;
      });
    }
    highlight();
  }
  function highlight() {
    needs = true;
    for (const n of nodes) {
      const sel = selected === n;
      n.obj.traverse((o) => {
        if (!o.isMesh || !o.material || !o.material.emissive) return;
        if (sel) {
          if (!o.userData.hl) { o.userData.hl = true; o.material = o.material.clone(); }
          o.material.emissive.setHex(0x665c00);
        } else if (o.userData.hl) {
          o.userData.hl = false;
          o.material = colorBy.checked ? kindMats[n.kind] : n.materials.get(o);
          if (o.material) o.material.wireframe = wire.checked;
        }
      });
    }
  }
  function select(n) {
    selected = n;
    highlight();
    clear(info);
    if (!n) { info.append(h("span.faint", "Click a piece of the map to see what it is.")); return; }
    const part = (ctx.look.parts || []).find((p) => p.name === n.part) || {};
    info.append(h("h3", h("code", n.part)), h("div.kv",
      h("span", "kind"), h("span", KIND_LABELS[n.kind] || n.kind),
      h("span", "placed at"), h("span.mono", n.position ? `${n.position.map((v) => v.toFixed(1)).join(", ")} · ${n.yaw}°` : "-"),
      h("span", "triangles"), h("span", (part.triangles || 0).toLocaleString()),
      h("span", "colliders"), h("span", part.colliders ?? "-"),
      h("span", "placed"), h("span", `${part.instances || 1} time(s)`),
      h("span", "from"), h("span.mono", n.source || Object.keys(part.sources || {}).slice(0, 4).join(", ") || "-")),
      h("div.help", { style: { marginTop: "8px" } }, "Map-local metres: x east, y north from the anchor (where the map is placed)."));
  }

  colorBy.addEventListener("change", applyLook);
  wire.addEventListener("change", applyLook);
  const ray = new THREE.Raycaster();
  let down = null;
  renderer.domElement.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (!model || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
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
  new GLTFLoader().load(`/api/projects/${ctx.pid}/map/preview.glb?k=${ctx.state.look_key}`, (gltf) => {
    if (disposed) return;
    model = gltf.scene;
    scene.add(model);
    // flat shaded like GLTFLoader's own materials: the preview has no normals (it shades from the faces),
    // and a smooth material without them renders every piece black
    for (const [k, c] of Object.entries(KIND_COLORS)) kindMats[k] = new THREE.MeshStandardMaterial({ color: c, roughness: 0.8, metalness: 0.0, side: THREE.DoubleSide, flatShading: true });
    for (const child of [...model.children]) {
      const ex = child.userData || {};
      const materials = new Map();
      child.traverse((o) => {
        if (o.isMesh) {
          materials.set(o, o.material);
          if (o.material) o.material.side = THREE.DoubleSide;
        }
      });
      nodes.push({ obj: child, part: ex.part || child.name, kind: ex.kind || "chunk", source: ex.source, position: ex.position, yaw: ex.yaw, materials });
    }
    if (!kindMats.glass) kindMats.glass = kindMats.object;
    const box = new THREE.Box3().setFromObject(model);
    const size = box.getSize(new THREE.Vector3());
    const span = Math.ceil(Math.max(size.x, size.z) / 10) * 10 + 40;
    const grid = new THREE.GridHelper(span, span / 10, 0x3a4256, 0x252a36);
    grid.position.y = 0;
    scene.add(grid);
    const axes = new THREE.AxesHelper(6);
    scene.add(axes);
    // the list: by kind, then by part (a part's toggle hides all of its placements)
    clear(list);
    const byKind = {};
    for (const n of nodes) ((byKind[n.kind] = byKind[n.kind] || {})[n.part] = byKind[n.kind][n.part] || []).push(n);
    for (const kind of Object.keys(byKind).sort()) {
      const all = Object.values(byKind[kind]).flat();
      const kcb = h("input", { type: "checkbox", checked: true, onchange: () => {
        for (const n of all) n.obj.visible = kcb.checked;
        for (const cb of list.querySelectorAll(`input[data-kind="${kind}"]`)) cb.checked = kcb.checked;
      } });
      list.append(h("label", { style: { fontWeight: 600, marginTop: "6px" } }, kcb,
        h("span.sw", { style: { background: "#" + (KIND_COLORS[kind] ?? 0x999999).toString(16).padStart(6, "0") } }),
        h("span", KIND_LABELS[kind] || kind), h("span.n", String(all.length))));
      for (const [part, items] of Object.entries(byKind[kind]).sort()) {
        const cb = h("input", { type: "checkbox", checked: true, "data-kind": kind, onclick: (e) => e.stopPropagation(),
          onchange: () => { for (const n of items) n.obj.visible = cb.checked; } });
        list.append(h("label", { "data-name": part, onclick: (e) => { if (e.target !== cb) { e.preventDefault(); select(items[0]); } } }, cb,
          h("span", { style: { paddingLeft: "14px" } }, part), h("span.n", items.length > 1 ? `×${items.length}` : "")));
      }
    }
    loading.remove();
    resize();
    frame("iso");
    const g = ctx.look.glb || {};
    overlay.prepend(h("span.pill", `${nodes.length} placements · ${(g.triangles || 0).toLocaleString()} triangles · ${fmtBytes(g.bytes)} · ${((performance.now() - t0) / 1000).toFixed(1)} s`));
  }, (xhr) => {
    if (xhr.total) loading.textContent = `Loading the 3D map... ${fmtBytes(xhr.loaded)} / ${fmtBytes(xhr.total)}`;
  }, (err) => {
    loading.textContent = "Could not load the 3D map: " + (err.message || err);
  });

  // render on demand: a big map is heavy and does not move by itself
  const requestRender = () => { needs = true; };
  controls.addEventListener("change", requestRender);
  for (const el of [colorBy, wire]) el.addEventListener("change", requestRender);
  list.addEventListener("change", requestRender);
  list.addEventListener("click", requestRender);
  renderer.domElement.addEventListener("pointerup", requestRender);
  function tick() {
    if (disposed) return;
    const moved = controls.update();
    if (needs || moved) {
      needs = false;
      renderer.render(scene, camera);
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  ctx.disposers.push(() => {
    disposed = true;
    ro.disconnect();
    renderer.dispose();
    scene.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
    });
  });
}
