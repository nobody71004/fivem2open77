import { h, clear, icon } from "../ui.js";
import { store } from "../store.js";
import { cityHologram } from "./cityholo.js";

const href = (p) => (p.kind === "map" ? `#/map/${p.id}/overview` : `#/project/${p.id}/overview`);
const thumb = (p) => `/api/projects/${encodeURIComponent(p.id)}/thumb.png?t=${Math.floor(p.updated || 0)}`;

function preview(p, kind, featured = false) {
  const card = h("a.showcase" + (featured ? ".featured" : ""), { href: p ? href(p) : `#/${kind === "map" ? "maps" : "projects"}` });
  const media = h("div.showcase-media");
  if (p?.thumb) {
    const img = h("img", { src: thumb(p), alt: `${p.name} · converted ${kind === "map" ? "MLO" : "car"} preview`, loading: "lazy" });
    img.addEventListener("error", () => { img.remove(); media.append(h("span.preview-missing", "Preview unavailable · open project")); });
    media.append(img);
  } else media.append(h("div.preview-missing", icon(kind === "map" ? "map" : "car"), p ? "Preview not generated" : "Your next creation starts here"));
  const build = p?.last_build;
  card.append(media, h("div.showcase-label", h("span.eyebrow", kind === "map" ? "WORLD / MLO" : "VEHICLE / CUSTOM"),
    h("div.showcase-title", h("h3", p?.name || (kind === "map" ? "Create a new world" : "Build your dream garage")), icon("chevron-right")),
    h("span.showcase-meta", build ? `Build ${build.n} · ${build.ok ? "ready" : "needs attention"}` : p ? "Imported project · explore" : "Import a FiveM resource")));
  return card;
}

export function homePage(root) {
  root.className = "home-page";
  let orbitArt = null;
  let holo = null, gone = false;
  const cosmos = h("canvas.cosmos", { "aria-hidden": "true" });
  const content = h("div.home-content");
  const featured = h("div.featured-grid"), library = h("div.showcase-grid"), stats = h("div.home-stats");
  const motionButton = h("button.btn.small.motion-toggle", { type: "button", "aria-pressed": false }, "Motion: off");
  const heading = h("section.brand-banner", { "aria-label": "F2077 · fivem2open77 Studio" },
    h("div.brand-backdrop", { "aria-hidden": "true" }),
    h("img.brand-logo", { src: "/img/logo.jpg", alt: "F2077", decoding: "async" }),
    h("div.brand-copy", h("div.eyebrow", h("span.signal"), "OPEN77 CREATION SYSTEM"), h("span", "Your universe. No limits.")),
    motionButton);
  content.append(heading,
    h("section.universe-hero", { "aria-labelledby": "home-title" },
      h("div.hero-copy", h("span.eyebrow", "FROM LOS SANTOS TO NIGHT CITY"),
        h("h1", { id: "home-title" }, "Build beyond ", h("br"), h("span", "the ordinary.")),
        h("p", "Exotic machines. Immersive interiors. A city that becomes yours. Bring your creations into a new universe."),
        h("div.hero-actions", h("a.btn.primary", { href: "#/maps" }, icon("map"), "Explore MLOs"),
          h("a.btn", { href: "#/projects" }, icon("car"), "Open garage")),
        h("a.agent-link", { href: "#/agent" }, icon("agent"), "Create with your AI copilot", h("span", "↗"))),
      orbitArt = h("div.orbit-art", { "aria-hidden": "true" }, h("div.orbit.orbit-one"), h("div.orbit.orbit-two"),
        h("div.orbit.orbit-three"), h("div.planet", h("span", "77")), h("span.orbit-tag", "NIGHT CITY / CONNECTED WORLDS"))),
    stats,
    h("section", { "aria-labelledby": "featured-title" }, h("div.section-title", h("div", h("span.eyebrow", "THE SHOWCASE"),
      h("h2", { id: "featured-title" }, "Built for another reality.")), h("a", { href: "#/live" }, "Live worlds ↗")), featured),
    h("section", { "aria-labelledby": "library-title" }, h("div.section-title", h("div", h("span.eyebrow", "YOUR COLLECTION"),
      h("h2", { id: "library-title" }, "Continue the next chapter.")), h("a", { href: "#/world" }, "World edits ↗")), library),
    h("div.home-bottom", h("span", "INSPECT → REFINE → BUILD → PLAY"), h("a", { href: "#/settings/agents" }, "NCWE · native world tools ↗")));
  root.append(cosmos, content);

  function render() {
    const state = store.state || {};
    const projects = (state.projects || []).filter(p => p.kind === "map" || (p.kind || "car") === "car")
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
    const cars = projects.filter(p => (p.kind || "car") === "car"), maps = projects.filter(p => p.kind === "map");
    clear(stats).append(...[[cars.length, "VEHICLES"], [maps.length, "MLOS / MAPS"],
      [(state.projects || []).filter(p => p.kind === "world").length, "WORLD EDITS"],
      [(state.settings?.servers || []).length, "CONFIGURED SERVERS"]].map(([n, label]) => h("div", h("b", n.toString().padStart(2, "0")), h("span", label))));
    clear(featured).append(preview(maps[0], "map", true), preview(cars[0], "car", true));
    const rest = projects.filter(p => p !== maps[0] && p !== cars[0]).slice(0, 6);
    clear(library).append(...(rest.length ? rest.map(p => preview(p, p.kind || "car")) :
      [h("div.collection-empty", "Your imported vehicles and MLOs appear here. ", h("a", { href: "#/maps" }, "Start a collection ↗"))]));
  }
  render();
  const off = store.on(ev => { if (ev.type === "state") render(); });
  const stopUniverse = universe(root, cosmos, motionButton);
  // Night City as a hologram when the 3D city is built (Live maps); the planet otherwise
  cityHologram(orbitArt).then((x) => {
    if (gone) { if (x) x.dispose(); return; }
    holo = x;
    if (x) orbitArt.classList.add("has-city");
  }).catch(() => { /* keep the planet */ });
  return { dispose() { gone = true; off(); stopUniverse(); if (holo) holo.dispose(); } };
}

// At most 110 stars, DPR capped at 1.5, 30fps. No animation off-page, hidden, or motion-disabled.
function universe(root, canvas, button) {
  const ctx = canvas.getContext("2d");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let enabled = !reduced.matches, frame = 0, last = 0, width = 1, height = 1, disposed = false;
  let pointer = { x: 0, y: 0 }, target = { x: 0, y: 0 };
  try { enabled = !reduced.matches && localStorage.getItem("studio-universe-motion") !== "off"; } catch (_) { /* storage optional */ }
  const stars = Array.from({ length: 110 }, () => ({ x: Math.random(), y: Math.random(), depth: .2 + Math.random() * .8, phase: Math.random() * Math.PI * 2 }));
  function paint(t = 0) {
    if (!ctx) return;
    ctx.clearRect(0, 0, width, height);
    for (const s of stars) {
      const x = (s.x * width + pointer.x * s.depth * 20 + width) % width;
      const y = (s.y * height + pointer.y * s.depth * 14 + (enabled ? t * .002 * s.depth : 0) + height) % height;
      ctx.fillStyle = `rgba(192,207,255,${.18 + (Math.sin(t * .0006 + s.phase) + 1) * .22})`;
      ctx.beginPath(); ctx.arc(x, y, .5 + s.depth, 0, Math.PI * 2); ctx.fill();
    }
  }
  function tick(t) {
    frame = 0;
    if (disposed || !enabled || document.hidden) return;
    if (t - last >= 33) {
      pointer.x += (target.x - pointer.x) * .08; pointer.y += (target.y - pointer.y) * .08;
      root.style.setProperty("--pointer-x", `${pointer.x * 12}px`);
      root.style.setProperty("--pointer-y", `${pointer.y * 9}px`);
      paint(t); last = t;
    }
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    cancelAnimationFrame(frame); frame = 0;
    root.classList.toggle("motion-off", !enabled);
    button.textContent = `Motion: ${enabled ? "on" : "off"}`;
    button.setAttribute("aria-pressed", String(enabled));
    if (!enabled) {
      pointer = target = { x: 0, y: 0 }; root.style.setProperty("--pointer-x", "0px"); root.style.setProperty("--pointer-y", "0px"); paint();
    }
    if (enabled && !document.hidden) frame = requestAnimationFrame(tick);
  }
  function resize() {
    width = root.clientWidth; height = root.clientHeight;
    const dpr = Math.min(devicePixelRatio || 1, 1.5);
    canvas.width = Math.max(1, Math.floor(width * dpr)); canvas.height = Math.max(1, Math.floor(height * dpr));
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0); paint();
  }
  function move(e) {
    if (!enabled || e.pointerType === "touch") return;
    const r = root.getBoundingClientRect();
    target = { x: ((e.clientX - r.left) / width - .5) * 2, y: ((e.clientY - r.top) / height - .5) * 2 };
  }
  function leave() { target = { x: 0, y: 0 }; }
  function preference() { enabled = !reduced.matches; sync(); }
  button.onclick = () => {
    enabled = !enabled;
    try { localStorage.setItem("studio-universe-motion", enabled ? "on" : "off"); } catch (_) { /* storage optional */ }
    sync();
  };
  const observer = new ResizeObserver(resize); observer.observe(root);
  root.addEventListener("pointermove", move); root.addEventListener("pointerleave", leave);
  document.addEventListener("visibilitychange", sync); reduced.addEventListener("change", preference);
  resize(); sync();
  return () => {
    disposed = true; cancelAnimationFrame(frame); observer.disconnect();
    root.removeEventListener("pointermove", move); root.removeEventListener("pointerleave", leave);
    document.removeEventListener("visibilitychange", sync); reduced.removeEventListener("change", preference);
    button.onclick = null;
  };
}
