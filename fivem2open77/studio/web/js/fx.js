// The Studio's high-tech layer: a living background and cursor effects, over every page.
//
//   background (WebGL, behind the app)   a hex lattice lit by the cursor, a neon horizon grid,
//                                         drifting data motes, a slow scan, shockwaves on clicks
//   cursor (2D canvas, over the app)     a light trail with sparks, a reticle, target-lock
//                                         brackets around what the cursor is on, neural links
//   elements                             spotlit cards (the border lights where the cursor is),
//                                         magnetic primary buttons, tilting showcase cards,
//                                         decoding page titles, ripples on buttons
//
// Levels (Settings > Appearance): full; lite (a still background, brackets and spotlights
// only); off.  The system's "reduce motion" starts at lite.  Nothing runs while the window is
// hidden, and the loops stop when the cursor rests.  Over 3D views (the city, car and map
// viewers) the cursor is left clean for precise work.
const KEY = "f2o77.fx";
const LEVELS = ["full", "lite", "off"];
const PRECISE = ".c3-canvas, .viewer canvas, .we-canvas, canvas.sheet, .live-stage .plan canvas, .stage canvas, .term";
const LOCKABLE = "a[href], button:not(:disabled), .btn, input:not([type=hidden]), select, textarea, [role=button], tr.link, .showcase, .car-card, .chip, .res, label.choice";
const SPOT = ".card, .showcase, .car-card, .modal, .home-stats > div, .c3-veil-in, .banner, .empty, .dropzone";
const MAGNET = ".btn.primary, .hero-actions .btn";
const TILT = ".showcase, .car-card";

let level = "full";
let started = false;
const listeners = new Set();

export function fxLevel() { return level; }
export function onFxLevel(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function setFxLevel(l) {
  if (!LEVELS.includes(l)) return;
  level = l;
  try { localStorage.setItem(KEY, l); } catch (_) { /* private mode */ }
  apply();
  for (const fn of listeners) fn(l);
}

function initialLevel() {
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (_) { /* private mode */ }
  if (LEVELS.includes(saved)) return saved;
  return matchMedia("(prefers-reduced-motion: reduce)").matches ? "lite" : "full";
}

// ====================================================================== background
const BG_VS = `attribute vec2 p; void main() { gl_Position = vec4(p, 0.0, 1.0); }`;
const BG_FS = `
precision highp float;
uniform vec2 uRes; uniform float uTime; uniform vec2 uMouse; uniform float uMouseOn; uniform float uScale;
uniform vec4 uClicks[4]; uniform vec3 uA; uniform vec3 uB; uniform float uAnim;
float sq(float x) { return x * x; }   // squares by hand: Direct3D's pow is NaN below zero
float hash(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * .1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float noise(vec2 p) { vec2 i = floor(p), f = fract(p); vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y); }
float fbm(vec2 p) { float v = 0.0, a = 0.5; for (int i = 0; i < 4; i++) { v += a * noise(p); p = p * 2.03 + 17.1; a *= 0.5; } return v; }
// distance to the nearest hex edge (0 on the edge) and the cell id
vec3 hexel(vec2 p) {
  const vec2 s = vec2(1.0, 1.7320508);
  vec2 a = mod(p, s) - s * 0.5, b = mod(p - s * 0.5, s) - s * 0.5;
  vec2 g = dot(a, a) < dot(b, b) ? a : b;
  vec2 id = p - g;
  vec2 q = abs(g);
  float d = 0.5 - max(dot(q, s * 0.5), q.x);
  return vec3(d, id);
}
void main() {
  vec2 p = gl_FragCoord.xy;
  vec2 uv = p / uRes;
  float t = uTime;
  vec3 col = mix(vec3(0.010, 0.014, 0.028), vec3(0.020, 0.013, 0.040), clamp(uv.x * 0.7 + (1.0 - uv.y) * 0.5, 0.0, 1.0));
  // nebula
  float n1 = fbm(uv * vec2(2.6, 1.8) + vec2(t * 0.006, -t * 0.004));
  float n2 = fbm(uv * vec2(1.6, 2.4) - vec2(t * 0.004, t * 0.003) + 9.0);
  col += uB * smoothstep(0.5, 0.95, n1) * 0.055 + uA * smoothstep(0.55, 1.0, n2) * 0.04;
  // shockwaves from clicks: a ring that also bends the lattice
  vec2 q = p;
  float ring = 0.0;
  for (int i = 0; i < 4; i++) {
    vec4 c = uClicks[i];
    float ct = t - c.z;
    if (ct > 0.0 && ct < 1.8) {
      vec2 d = p - c.xy;
      float r = length(d);
      float R = ct * 820.0 * uScale;
      float w = exp(-sq((r - R) / (30.0 * uScale))) * (1.0 - ct / 1.8) * c.w;
      ring += w;
      q += d / max(r, 1.0) * w * 9.0 * uScale;
    }
  }
  // the hex lattice: faint everywhere, lit around the cursor
  float hs = 30.0 * uScale;
  vec3 hx = hexel(q / hs);
  float edge = 1.0 - smoothstep(0.0, 0.045, hx.x);
  vec2 md = p - uMouse;
  float dm = length(md);
  float spot = exp(-dm * dm / (2.0 * sq(230.0 * uScale))) * uMouseOn;
  float near = exp(-dm * dm / (2.0 * sq(70.0 * uScale))) * uMouseOn;
  float blink = step(0.992, hash(hx.yz + floor(t * 0.7))) * uAnim;
  float fill = 1.0 - smoothstep(0.0, 0.35, 0.5 - hx.x);
  col += uA * edge * (0.018 + spot * 0.42 + ring * 0.7);
  col += uA * fill * (blink * (0.05 + spot * 0.25) + near * 0.05);
  col += uB * edge * near * 0.25;
  // the horizon grid at the bottom
  float hy = uv.y;
  if (hy < 0.34) {
    float depth = (0.34 - hy) / 0.34;
    float z = 1.0 / max(depth, 0.02);
    vec2 g = vec2((uv.x - 0.5) * z * 2.4, z * 1.2 + t * 0.35 * uAnim);
    vec2 gd = abs(fract(g) - 0.5);
    vec2 fw = fwidth(g);
    float gl = max(1.0 - smoothstep(0.0, fw.x * 1.4, gd.x), 1.0 - smoothstep(0.0, fw.y * 1.4, gd.y));
    float fade = smoothstep(0.0, 0.5, depth) * (1.0 - smoothstep(0.85, 1.0, depth) * 0.3);
    col += mix(uA, uB, 0.55) * gl * fade * 0.11;
    col += uB * exp(-sq((hy - 0.34) * uRes.y / (10.0 * uScale))) * 0.06;      // the horizon line
  }
  // data motes drifting up
  for (int k = 0; k < 2; k++) {
    float sc = k == 0 ? 90.0 : 46.0;
    vec2 m = p / (sc * uScale) + vec2(0.0, -t * (k == 0 ? 0.05 : 0.09) * uAnim);
    vec2 cell = floor(m), f = fract(m) - 0.5;
    float h = hash(cell + float(k) * 7.7);
    if (h > 0.83) {
      vec2 o = vec2(hash(cell + 1.3), hash(cell + 2.9)) - 0.5;
      float d = length(f - o * 0.7);
      float tw = 0.55 + 0.45 * sin(t * (1.0 + h * 3.0) + h * 40.0);
      col += mix(uA, uB, step(0.95, h)) * exp(-d * d * (k == 0 ? 900.0 : 1400.0)) * tw * (0.35 + spot * 1.4);
    }
  }
  // the scan sweeping down every 11 s
  float sy = 1.0 - fract(t / 11.0);
  col += uA * exp(-sq((uv.y - sy) * uRes.y / (2.0 * uScale))) * 0.05 * uAnim;
  col += uA * exp(-sq((uv.y - sy) * uRes.y / (60.0 * uScale))) * 0.012 * uAnim;
  // vignette, grain
  vec2 c = uv - 0.5;
  col *= 1.0 - 0.55 * dot(c, c) * 1.6;
  col += (hash(p + fract(t) * 100.0) - 0.5) * 0.008;
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

function background() {
  const canvas = document.createElement("canvas");
  canvas.id = "fx-bg";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const gl = canvas.getContext("webgl", { antialias: false, alpha: false, depth: false, stencil: false, powerPreference: "low-power", preserveDrawingBuffer: false });
  if (!gl) { canvas.remove(); return null; }
  gl.getExtension("OES_standard_derivatives");
  const sh = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, type === gl.FRAGMENT_SHADER ? "#extension GL_OES_standard_derivatives : enable\n" + src : src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.warn("fx shader:", gl.getShaderInfoLog(s)); return null; }
    return s;
  };
  const vs = sh(gl.VERTEX_SHADER, BG_VS), fs = sh(gl.FRAGMENT_SHADER, BG_FS);
  if (!vs || !fs) { canvas.remove(); return null; }
  const prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { canvas.remove(); return null; }
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const u = {};
  for (const n of ["uRes", "uTime", "uMouse", "uMouseOn", "uScale", "uClicks", "uA", "uB", "uAnim"]) u[n] = gl.getUniformLocation(prog, n);
  gl.uniform3f(u.uA, 0.38, 0.95, 0.93);
  gl.uniform3f(u.uB, 1.0, 0.31, 0.64);
  const clicks = new Float32Array(16);
  let ci = 0;
  let scale = 0.5;
  let W = 1, H = 1;
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    scale = Math.min(0.75, Math.max(0.4, 0.55 * dpr));     // the background is soft: render it small
    W = Math.max(1, Math.round(innerWidth * scale)); H = Math.max(1, Math.round(innerHeight * scale));
    canvas.width = W; canvas.height = H;
    gl.viewport(0, 0, W, H);
  }
  resize();
  return {
    canvas, resize,
    click(x, y, strength = 1) {
      clicks.set([x * scale, (innerHeight - y) * scale, performance.now() / 1000, strength], (ci++ % 4) * 4);
    },
    draw(t, mx, my, on, anim) {
      gl.uniform2f(u.uRes, W, H);
      gl.uniform1f(u.uTime, t);
      gl.uniform2f(u.uMouse, mx * scale, (innerHeight - my) * scale);
      gl.uniform1f(u.uMouseOn, on);
      gl.uniform1f(u.uScale, scale);
      gl.uniform1f(u.uAnim, anim);
      gl.uniform4fv(u.uClicks, clicks);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    },
    dispose() { canvas.remove(); const x = gl.getExtension("WEBGL_lose_context"); if (x) x.loseContext(); },
  };
}

// ====================================================================== cursor layer
function cursorLayer() {
  const canvas = document.createElement("canvas");
  canvas.id = "fx-fg";
  canvas.setAttribute("aria-hidden", "true");
  document.body.append(canvas);
  const g = canvas.getContext("2d");
  let dpr = 1;
  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(innerWidth * dpr); canvas.height = Math.round(innerHeight * dpr);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  return { canvas, g, resize, clear() { g.clearRect(0, 0, innerWidth, innerHeight); } };
}

// ====================================================================== the engine
let bg = null, fg = null;
const st = {
  x: -999, y: -999, px: -999, py: -999, vx: 0, vy: 0, inside: false, precise: false, moved: 0,
  rx: -999, ry: -999,                      // the reticle (eases after the cursor)
  trail: [], sparks: [], waves: [],
  lock: null, box: null,                   // the element locked on, and the brackets' current box
  links: [],
  raf: 0, tm: 0, t0: performance.now(), lastBg: 0, until: 0,
};

function apply() {
  const on = level !== "off";
  document.documentElement.dataset.fx = level;
  if (on && !bg) { bg = background(); if (bg) bg.draw(performance.now() / 1000, -999, -999, 0, 0); }
  if (!on && bg) { bg.dispose(); bg = null; }
  if (on && !fg) fg = cursorLayer();
  if (!on && fg) { fg.canvas.remove(); fg = null; }
  if (on) kick();
}

function kick(ms = 1400) {
  st.until = Math.max(st.until, performance.now() + ms);
  if (!st.raf && level !== "off") st.raf = requestAnimationFrame(loop);
}

function lockKind(el) {
  if (!el) return "";
  if (el.matches("input, textarea")) return "INPUT";
  if (el.matches("select")) return "SELECT";
  if (el.matches(".showcase, .car-card, tr.link")) return "OPEN";
  if (el.matches("a[href]")) return "LINK";
  return "EXEC";
}

let heavyAt = 0, heavy = false;
function loop(now) {
  st.raf = 0;
  if (level === "off") return;
  if (document.hidden) return;                       // resumes on visibility
  const t = (now - st.t0) / 1000;
  const full = level === "full";
  // a 3D view on the page does the heavy drawing: the background behind it slows right down
  if (now - heavyAt > 1000) { heavyAt = now; heavy = !!document.querySelector(".c3-canvas, .viewer canvas, .we-canvas"); }
  const active = now < st.until;
  const fps = heavy ? 8 : active ? 45 : 20;
  // the background: animated in full, redrawn on cursor moves in lite
  if (bg && (full ? now - st.lastBg > 1000 / fps - 2 : st.moved > st.lastBg)) {
    bg.draw(t, st.x, st.y, st.inside && !st.precise ? 1 : 0, full ? 1 : 0);
    st.lastBg = full ? now : st.moved;
  }
  if (fg) drawCursor(now, t, full);
  const busy = st.waves.length || st.sparks.length || (full && st.trail.length) || boxMoving();
  if (busy || active) st.raf = requestAnimationFrame(loop);
  else if (full) {
    // at rest: the background keeps breathing at a low rate
    if (!st.tm) st.tm = setTimeout(() => { st.tm = 0; if (!st.raf) st.raf = requestAnimationFrame(loop); }, 1000 / fps);
  } else if (fg) fg.clear();
}

function boxMoving() {
  if (!st.box) return false;
  const tgt = st.lock ? st.lock.getBoundingClientRect() : null;
  if (!tgt) return st.box.a > 0.01;
  return Math.abs(st.box.x - tgt.left) + Math.abs(st.box.y - tgt.top) + Math.abs(st.box.w - tgt.width) + Math.abs(st.box.h - tgt.height) > 0.5 || st.box.a < 0.99;
}

const A = "158,243,237", M = "255,79,163", Y = "255,210,63";

function drawCursor(now, t, full) {
  const g = fg.g;
  fg.clear();
  const clean = st.precise || !st.inside;
  // neural links: the motes near the cursor wired to it and to each other
  if (full && !clean) {
    if (st.links.length < 64) for (let i = st.links.length; i < 64; i++) st.links.push({ x: Math.random() * innerWidth, y: Math.random() * innerHeight, vx: (Math.random() - 0.5) * 0.25, vy: -0.08 - Math.random() * 0.25 });
    const near = [];
    for (const p of st.links) {
      p.x += p.vx; p.y += p.vy;
      if (p.y < -10) { p.y = innerHeight + 10; p.x = Math.random() * innerWidth; }
      if (p.x < -10) p.x = innerWidth + 10; else if (p.x > innerWidth + 10) p.x = -10;
      const d = Math.hypot(p.x - st.x, p.y - st.y);
      if (d < 190) near.push([p, d]);
    }
    g.lineWidth = 1;
    for (let i = 0; i < near.length; i++) {
      const [p, d] = near[i];
      const a = (1 - d / 190);
      g.strokeStyle = `rgba(${A},${(a * 0.35).toFixed(3)})`;
      g.beginPath(); g.moveTo(st.x, st.y); g.lineTo(p.x, p.y); g.stroke();
      for (let j = i + 1; j < near.length; j++) {
        const [q, e] = near[j];
        const dd = Math.hypot(p.x - q.x, p.y - q.y);
        if (dd < 110) {
          g.strokeStyle = `rgba(${M},${(Math.min(a, 1 - e / 190) * (1 - dd / 110) * 0.4).toFixed(3)})`;
          g.beginPath(); g.moveTo(p.x, p.y); g.lineTo(q.x, q.y); g.stroke();
        }
      }
      g.fillStyle = `rgba(${A},${(a * 0.9).toFixed(3)})`;
      g.fillRect(p.x - 1.2, p.y - 1.2, 2.4, 2.4);
    }
  }
  // the light trail
  const life = 380;
  while (st.trail.length && now - st.trail[0].t > life) st.trail.shift();
  if (full && !clean && st.trail.length > 2) {
    const pts = st.trail;
    for (let pass = 0; pass < 2; pass++) {
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i];
        const k = 1 - (now - b.t) / life;
        if (k <= 0) continue;
        const f = i / pts.length;
        g.strokeStyle = pass === 0 ? `rgba(${f > 0.5 ? A : M},${(k * 0.16).toFixed(3)})` : `rgba(${A},${(k * 0.85).toFixed(3)})`;
        g.lineWidth = pass === 0 ? 7 * k + 1 : 1.6 * k + 0.3;
        g.lineCap = "round";
        g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
      }
    }
  }
  // sparks
  for (let i = st.sparks.length - 1; i >= 0; i--) {
    const s = st.sparks[i];
    const k = 1 - (now - s.t) / s.life;
    if (k <= 0) { st.sparks.splice(i, 1); continue; }
    s.x += s.vx; s.y += s.vy; s.vx *= 0.94; s.vy = s.vy * 0.94 + 0.05;
    g.strokeStyle = `rgba(${s.c},${k.toFixed(3)})`;
    g.lineWidth = 1.2;
    g.beginPath(); g.moveTo(s.x, s.y); g.lineTo(s.x - s.vx * 2.5, s.y - s.vy * 2.5); g.stroke();
  }
  // shockwaves
  for (let i = st.waves.length - 1; i >= 0; i--) {
    const w = st.waves[i];
    const k = (now - w.t) / 650;
    if (k >= 1) { st.waves.splice(i, 1); continue; }
    const e = 1 - Math.pow(1 - k, 3);
    g.strokeStyle = `rgba(${A},${(1 - k).toFixed(3)})`;
    g.lineWidth = 2 * (1 - k) + 0.5;
    g.beginPath(); g.arc(w.x, w.y, 6 + e * 46, 0, Math.PI * 2); g.stroke();
    g.strokeStyle = `rgba(${M},${((1 - k) * 0.7).toFixed(3)})`;
    g.beginPath(); g.arc(w.x, w.y, 4 + e * 26, 0, Math.PI * 2); g.stroke();
    for (let r = 0; r < 8; r++) {
      const ang = r * Math.PI / 4 + w.rot;
      const r0 = 10 + e * 40, r1 = r0 + 7 * (1 - k);
      g.strokeStyle = `rgba(${Y},${(1 - k).toFixed(3)})`;
      g.beginPath(); g.moveTo(w.x + Math.cos(ang) * r0, w.y + Math.sin(ang) * r0); g.lineTo(w.x + Math.cos(ang) * r1, w.y + Math.sin(ang) * r1); g.stroke();
    }
  }
  // the reticle and the target lock
  const ease = 1 - Math.pow(0.0008, 1 / 60);
  st.rx += (st.x - st.rx) * Math.min(1, ease * 9);
  st.ry += (st.y - st.ry) * Math.min(1, ease * 9);
  const lockEl = !st.precise && st.inside ? st.lock : null;
  const r = lockEl ? lockEl.getBoundingClientRect() : null;
  if (r && r.width && r.height) {
    const pad = 4;
    const tgt = { x: r.left - pad, y: r.top - pad, w: r.width + pad * 2, h: r.height + pad * 2 };
    if (!st.box) st.box = { x: st.rx - 12, y: st.ry - 12, w: 24, h: 24, a: 0 };
    const kk = 0.28;
    st.box.x += (tgt.x - st.box.x) * kk; st.box.y += (tgt.y - st.box.y) * kk;
    st.box.w += (tgt.w - st.box.w) * kk; st.box.h += (tgt.h - st.box.h) * kk;
    st.box.a += (1 - st.box.a) * 0.25;
  } else if (st.box) {
    st.box.x += (st.rx - 12 - st.box.x) * 0.3; st.box.y += (st.ry - 12 - st.box.y) * 0.3;
    st.box.w += (24 - st.box.w) * 0.3; st.box.h += (24 - st.box.h) * 0.3;
    st.box.a *= 0.8;
    if (st.box.a < 0.02) st.box = null;
  }
  if (st.box && st.box.a > 0.02) {
    const b = st.box, L = Math.min(12, b.w / 3, b.h / 3);
    g.strokeStyle = `rgba(${A},${(0.95 * b.a).toFixed(3)})`;
    g.lineWidth = 1.5;
    g.beginPath();
    for (const [cx, cy, sx, sy] of [[b.x, b.y, 1, 1], [b.x + b.w, b.y, -1, 1], [b.x, b.y + b.h, 1, -1], [b.x + b.w, b.y + b.h, -1, -1]]) {
      g.moveTo(cx, cy + sy * L); g.lineTo(cx, cy); g.lineTo(cx + sx * L, cy);
    }
    g.stroke();
    if (lockEl && b.a > 0.6 && b.w > 40) {
      const kind = lockKind(lockEl);
      g.font = "600 9px 'IBM Plex Mono', monospace";
      const label = `◢ ${kind}`;
      const tw = g.measureText(label).width;
      g.fillStyle = `rgba(5,9,18,${(0.8 * b.a).toFixed(3)})`;
      g.fillRect(b.x, b.y - 13, tw + 8, 11);
      g.fillStyle = `rgba(${A},${b.a.toFixed(3)})`;
      g.fillText(label, b.x + 4, b.y - 4.5);
      // a scan line running down the locked element
      const sy = b.y + ((now / 900) % 1) * b.h;
      g.strokeStyle = `rgba(${A},${(0.18 * b.a).toFixed(3)})`;
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(b.x + 2, sy); g.lineTo(b.x + b.w - 2, sy); g.stroke();
    }
  }
  if (full && !clean && !lockEl) {
    // a segmented ring that turns, around an exact centre dot
    const rot = t * 1.6;
    g.strokeStyle = `rgba(${A},0.75)`;
    g.lineWidth = 1.2;
    for (let i = 0; i < 4; i++) {
      g.beginPath();
      g.arc(st.rx, st.ry, 13, rot + i * Math.PI / 2, rot + i * Math.PI / 2 + Math.PI / 4);
      g.stroke();
    }
    const sp = Math.min(1, Math.hypot(st.vx, st.vy) / 30);
    g.strokeStyle = `rgba(${M},${(0.3 + sp * 0.6).toFixed(3)})`;
    g.beginPath(); g.arc(st.rx, st.ry, 18 + sp * 6, -rot * 0.7, -rot * 0.7 + Math.PI * 0.6); g.stroke();
  }
}

// ---------------------------------------------------------------------- events
let spotEl = null, magnetEl = null, tiltEl = null;

function onMove(e) {
  if (e.pointerType === "touch") return;
  const x = e.clientX, y = e.clientY;
  st.vx = x - st.x; st.vy = y - st.y;
  st.x = x; st.y = y;
  if (st.rx < -900) { st.rx = x; st.ry = y; }
  st.inside = true;
  st.moved = performance.now();
  const tgt = e.target instanceof Element ? e.target : null;
  st.precise = !!(tgt && tgt.closest(PRECISE));
  st.lock = tgt && !st.precise ? tgt.closest(LOCKABLE) : null;
  if (level === "full" && !st.precise) {
    st.trail.push({ x, y, t: st.moved });
    if (st.trail.length > 40) st.trail.shift();
    const sp = Math.hypot(st.vx, st.vy);
    if (sp > 24 && st.sparks.length < 60) {
      for (let i = 0; i < Math.min(4, sp / 18); i++) {
        const a = Math.atan2(-st.vy, -st.vx) + (Math.random() - 0.5) * 1.6;
        const v = 1 + Math.random() * 3;
        st.sparks.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, t: st.moved, life: 260 + Math.random() * 260, c: Math.random() < 0.7 ? A : M });
      }
    }
  }
  // spotlit cards
  const sp = tgt ? tgt.closest(SPOT) : null;
  if (sp !== spotEl) { if (spotEl) spotEl.classList.remove("fx-lit"); spotEl = sp; if (sp) sp.classList.add("fx-lit"); }
  if (sp) {
    const r = sp.getBoundingClientRect();
    sp.style.setProperty("--mx", `${(x - r.left).toFixed(0)}px`);
    sp.style.setProperty("--my", `${(y - r.top).toFixed(0)}px`);
  }
  // magnetic buttons, tilting cards (full only)
  const mg = level === "full" && tgt ? tgt.closest(MAGNET) : null;
  if (mg !== magnetEl) { if (magnetEl) { magnetEl.style.removeProperty("--tx"); magnetEl.style.removeProperty("--ty"); } magnetEl = mg; }
  if (mg) {
    const r = mg.getBoundingClientRect();
    mg.style.setProperty("--tx", `${((x - r.left - r.width / 2) * 0.14).toFixed(1)}px`);
    mg.style.setProperty("--ty", `${((y - r.top - r.height / 2) * 0.22).toFixed(1)}px`);
  }
  const tl = level === "full" && tgt ? tgt.closest(TILT) : null;
  if (tl !== tiltEl) { if (tiltEl) { tiltEl.style.removeProperty("--rx"); tiltEl.style.removeProperty("--ry"); } tiltEl = tl; }
  if (tl) {
    const r = tl.getBoundingClientRect();
    const nx = (x - r.left) / r.width - 0.5, ny = (y - r.top) / r.height - 0.5;
    tl.style.setProperty("--rx", `${(-ny * 6).toFixed(2)}deg`);
    tl.style.setProperty("--ry", `${(nx * 8).toFixed(2)}deg`);
  }
  kick();
}

function onDown(e) {
  if (e.pointerType === "touch" || e.button !== 0) return;
  const tgt = e.target instanceof Element ? e.target : null;
  if (tgt && tgt.closest(PRECISE)) return;
  st.waves.push({ x: e.clientX, y: e.clientY, t: performance.now(), rot: Math.random() * Math.PI });
  if (st.waves.length > 6) st.waves.shift();
  if (bg) bg.click(e.clientX, e.clientY, 1);
  if (level === "full") {
    for (let i = 0; i < 14; i++) {
      const a = Math.random() * Math.PI * 2, v = 1.5 + Math.random() * 3.5;
      st.sparks.push({ x: e.clientX, y: e.clientY, vx: Math.cos(a) * v, vy: Math.sin(a) * v, t: performance.now(), life: 300 + Math.random() * 300, c: i % 3 ? A : Y });
    }
  }
  // a ripple inside the button pressed
  const btn = tgt ? tgt.closest(".btn, .c3-tool, .c3-modes button, .view-tabs button, #rail a") : null;
  if (btn && level !== "off") {
    const r = btn.getBoundingClientRect();
    const rip = document.createElement("span");
    rip.className = "fx-ripple";
    rip.style.left = `${e.clientX - r.left}px`;
    rip.style.top = `${e.clientY - r.top}px`;
    btn.append(rip);
    setTimeout(() => rip.remove(), 650);
  }
  kick();
}

function onLeave() { st.inside = false; st.lock = null; kick(); }

// ---------------------------------------------------------------------- titles that decode
const GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&*+=/<>[]{}";
export function fxTitle(el) {
  if (!el || level !== "full" || el.dataset.fxDone) return;
  el.dataset.fxDone = "1";
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) if (walker.currentNode.nodeValue.trim()) nodes.push([walker.currentNode, walker.currentNode.nodeValue]);
  const total = nodes.reduce((s, [, v]) => s + v.length, 0);
  if (!total || total > 120) return;
  const t0 = performance.now(), dur = 520;
  (function step(now) {
    const k = Math.min(1, (now - t0) / dur);
    let at = 0;
    for (const [n, v] of nodes) {
      let out = "";
      for (let i = 0; i < v.length; i++, at++) {
        const done = at / total < k * 1.15 - 0.15;
        out += done || v[i] === " " ? v[i] : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      }
      n.nodeValue = out;
    }
    if (k < 1) requestAnimationFrame(step);
    else for (const [n, v] of nodes) n.nodeValue = v;
  })(t0);
}

// called by the router after a page is drawn
export function fxRoute(main) {
  if (level === "off" || !main) return;
  main.classList.remove("fx-enter");
  void main.offsetWidth;            // restart the entrance
  main.classList.add("fx-enter");
  setTimeout(() => main.classList.remove("fx-enter"), 700);
  requestAnimationFrame(() => fxTitle(main.querySelector(".page-head h1, h1")));
}

export function initFx() {
  if (started) return;
  started = true;
  level = initialLevel();
  addEventListener("pointermove", onMove, { passive: true });
  addEventListener("pointerdown", onDown, { passive: true });
  document.documentElement.addEventListener("pointerleave", onLeave);
  addEventListener("blur", onLeave);
  addEventListener("resize", () => { if (bg) bg.resize(); if (fg) fg.resize(); kick(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) kick(); });
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", (e) => {
    let saved = null;
    try { saved = localStorage.getItem(KEY); } catch (_) { /* private mode */ }
    if (!saved) setFxLevel(e.matches ? "lite" : "full");
  });
  apply();
  if (level !== "off") {
    document.body.classList.add("fx-boot");
    setTimeout(() => document.body.classList.remove("fx-boot"), 1500);
  }
}
