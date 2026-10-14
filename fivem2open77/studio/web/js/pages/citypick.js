// A whole city (Liberty V and the like) is converted a district at a time: the city seen from above
// (where its objects stand), its districts, and the area to convert (a circle: click to move it).
import { api } from "../api.js";
import { h, clear, toast } from "../ui.js";
import { jobCard, follow } from "../imports.js";

const SIZE = 760;
const PAD = 18;

function cityName(base, district) {
  const c = (base || "city").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "city";
  let d = (district || "district").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "district";
  if (d.startsWith(c)) d = d.slice(c.length).replace(/^_+/, "") || "district";
  let n = `${c}_${d}`.replace(/_+/g, "_");
  if (!/^[a-z]/.test(n)) n = "m_" + n;
  return n.slice(0, 48).replace(/_+$/, "");
}

function titled(s) {
  const w = String(s || "").replace(/[_-]+/g, " ").trim();
  return w === w.toLowerCase() ? w.replace(/\b\w/g, (c) => c.toUpperCase()) : w;
}

// opts: { pid, city (the city look), current: [x, y, r] | null, newProject: bool, onDone(projectId) }
export function cityPicker(opts) {
  const city = opts.city;
  const ov = city.overview || {};
  const ext = ov.extent || [-1000, -1000, 1000, 1000];
  const grid = ov.grid || { cell: 32, cells: [] };
  const districts = city.districts || [];
  const spanX = Math.max(1, ext[2] - ext[0]);
  const spanY = Math.max(1, ext[3] - ext[1]);
  const s = Math.min((SIZE - 2 * PAD) / spanX, (SIZE - 2 * PAD) / spanY);
  const ox = PAD + ((SIZE - 2 * PAD) - s * spanX) / 2;
  const oy = PAD + ((SIZE - 2 * PAD) - s * spanY) / 2;
  const toPx = (x, y) => [ox + (x - ext[0]) * s, SIZE - (oy + (y - ext[1]) * s)];
  const toWorld = (px, py) => [ext[0] + (px - ox) / s, ext[1] + ((SIZE - py) - oy) / s];

  // where the objects stand, drawn once
  const dens = document.createElement("canvas");
  dens.width = dens.height = SIZE;
  {
    const g = dens.getContext("2d");
    g.fillStyle = "#04070d";
    g.fillRect(0, 0, SIZE, SIZE);
    let max = 1;
    for (const c of grid.cells) if (c[2] > max) max = c[2];
    const lm = Math.log(1 + max);
    const w = Math.max(1.2, grid.cell * s);
    for (const [ix, iy, n] of grid.cells) {
      const t = Math.log(1 + n) / lm;
      const [px, py] = toPx(ix * grid.cell, (iy + 1) * grid.cell);
      const r = Math.round(20 + 200 * Math.max(0, t - 0.55) * 2.2);
      const gg = Math.round(60 + 170 * t);
      const b = Math.round(110 + 140 * t);
      g.fillStyle = `rgba(${Math.min(255, r)},${Math.min(255, gg)},${Math.min(255, b)},${0.35 + 0.65 * t})`;
      g.fillRect(px, py, w, w);
    }
  }

  const canvas = h("canvas.city-canvas", { width: SIZE, height: SIZE, title: "click: the area's centre" });
  const ctx2 = canvas.getContext("2d");
  // the districts converted already (other projects reading the same city)
  const taken = city.taken || [];
  const takenNames = new Set(taken.map((t) => t.name));
  const isTaken = (x, y) => taken.some((t) => (t.area[0] - x) ** 2 + (t.area[1] - y) ** 2 < (t.area[2] * 0.8) ** 2);
  const first = districts.find((d) => !isTaken(d.center[0], d.center[1])) || districts[0];
  const cur = opts.current || (first ? [first.center[0], first.center[1], Math.min(first.radius, city.radius || 400)] : [0, 0, 400]);
  let area = [Number(cur[0]), Number(cur[1]), Number(cur[2])];
  let picked = opts.current ? null : (first ? first.name : null);
  let hover = null;

  const radius = h("input", { type: "range", min: 50, max: 1500, step: 10, value: area[2] });
  const radiusVal = h("b");
  const xIn = h("input", { type: "number", step: 1, value: area[0].toFixed(0), style: { width: "100%" } });
  const yIn = h("input", { type: "number", step: 1, value: area[1].toFixed(0), style: { width: "100%" } });
  const nameIn = h("input", { type: "text", style: { width: "100%" } });
  const dispIn = h("input", { type: "text", style: { width: "100%" } });
  const est = h("div.help");
  const where = h("div.help.faint", " ");
  let nameTouched = false;
  nameIn.addEventListener("input", () => { nameTouched = true; });
  dispIn.addEventListener("input", () => { nameTouched = true; });

  function estimate() {
    const [x, y, r] = area;
    let n = 0;
    for (const [ix, iy, c] of grid.cells) {
      const cx = (ix + 0.5) * grid.cell - x;
      const cy = (iy + 0.5) * grid.cell - y;
      if (cx * cx + cy * cy <= r * r) n += c;
    }
    return n;
  }

  function nearest(x, y) {
    let best = null;
    let bd = Infinity;
    for (const d of districts) {
      const dd = (d.center[0] - x) ** 2 + (d.center[1] - y) ** 2;
      if (dd < bd) { bd = dd; best = d; }
    }
    return best;
  }

  function names() {
    if (nameTouched) return;
    const d = picked || (nearest(area[0], area[1]) || {}).name || `x${Math.round(area[0])}_y${Math.round(area[1])}`;
    let n = cityName(city.map, d);
    let k = 2;
    while (takenNames.has(n)) n = `${cityName(city.map, d).slice(0, 45)}_${k++}`;     // a second piece of a district
    nameIn.value = n;
    dispIn.value = `${city.display_name} ${titled(d)}${k > 2 ? ` ${k - 1}` : ""}`.slice(0, 80);
  }

  function draw() {
    ctx2.drawImage(dens, 0, 0);
    ctx2.font = "11px Segoe UI, sans-serif";
    ctx2.textAlign = "center";
    for (const t of taken) {
      const [tx, ty] = toPx(t.area[0], t.area[1]);
      ctx2.beginPath();
      ctx2.arc(tx, ty, t.area[2] * s, 0, Math.PI * 2);
      ctx2.fillStyle = "rgba(120,255,180,0.08)";
      ctx2.fill();
      ctx2.setLineDash([4, 4]);
      ctx2.lineWidth = 1.5;
      ctx2.strokeStyle = "rgba(120,255,180,0.7)";
      ctx2.stroke();
      ctx2.setLineDash([]);
      ctx2.fillStyle = "rgba(160,255,200,0.9)";
      ctx2.fillText(`${t.display_name || t.name} (done)`, tx, ty + t.area[2] * s + 12);
    }
    for (const d of districts.slice(0, 24)) {
      const [px, py] = toPx(d.center[0], d.center[1]);
      ctx2.fillStyle = d.name === picked ? "#ffe066" : "rgba(220,235,255,0.75)";
      ctx2.fillRect(px - 1.5, py - 1.5, 3, 3);
      ctx2.fillText(d.name, px, py - 6);
    }
    const [cx, cy] = toPx(area[0], area[1]);
    ctx2.beginPath();
    ctx2.arc(cx, cy, area[2] * s, 0, Math.PI * 2);
    ctx2.fillStyle = "rgba(255,224,102,0.12)";
    ctx2.fill();
    ctx2.lineWidth = 2;
    ctx2.strokeStyle = "#ffe066";
    ctx2.stroke();
    ctx2.beginPath();
    ctx2.moveTo(cx - 6, cy); ctx2.lineTo(cx + 6, cy); ctx2.moveTo(cx, cy - 6); ctx2.lineTo(cx, cy + 6);
    ctx2.stroke();
    if (hover) {
      ctx2.fillStyle = "rgba(255,255,255,0.9)";
      ctx2.textAlign = "left";
      ctx2.fillText(`${hover[0].toFixed(0)}, ${hover[1].toFixed(0)}`, 8, SIZE - 8);
    }
    // north up, a 500 m bar
    ctx2.textAlign = "left";
    ctx2.fillStyle = "rgba(220,235,255,0.8)";
    ctx2.fillText("N ↑", SIZE - 34, 18);
    const bar = 500 * s;
    ctx2.fillRect(SIZE - 16 - bar, SIZE - 14, bar, 2);
    ctx2.fillText("500 m", SIZE - 16 - bar, SIZE - 20);
    radiusVal.textContent = `${area[2].toFixed(0)} m`;
    const n = estimate();
    est.textContent = `about ${n.toLocaleString()} of the city's ${(ov.entities || 0).toLocaleString()} map entities stand in this area`;
    est.className = n > 40000 ? "help err" : "help";
    if (n > 40000) est.textContent += ": too many for one build, make the radius smaller";
    if (n === 0) { est.className = "help warn"; est.textContent = "nothing of the city stands here: move the area onto it"; }
  }

  function set(x, y, r, district) {
    area = [x, y, r == null ? area[2] : r];
    picked = district || null;
    xIn.value = area[0].toFixed(0);
    yIn.value = area[1].toFixed(0);
    radius.value = area[2];
    names();
    draw();
  }

  const scale = (e) => {
    const r = canvas.getBoundingClientRect();
    return [(e.clientX - r.left) * SIZE / r.width, (e.clientY - r.top) * SIZE / r.height];
  };
  canvas.addEventListener("click", (e) => {
    const [x, y] = toWorld(...scale(e));
    nameTouched = false;
    set(x, y, null, null);
  });
  canvas.addEventListener("mousemove", (e) => {
    hover = toWorld(...scale(e));
    const d = nearest(hover[0], hover[1]);
    where.textContent = d ? `near ${d.name} (${d.entities.toLocaleString()} entities)` : " ";
    draw();
  });
  canvas.addEventListener("mouseleave", () => { hover = null; draw(); });
  radius.addEventListener("input", () => { area[2] = Number(radius.value); draw(); });
  for (const inp of [xIn, yIn]) inp.addEventListener("change", () => set(Number(xIn.value), Number(yIn.value), null, null));

  const listBody = h("tbody");
  for (const d of districts.slice(0, 60)) {
    listBody.append(h("tr.link", { onclick: () => { nameTouched = false; set(d.center[0], d.center[1], Math.min(d.radius, city.radius || 400), d.name); } },
      h("td", h("code", d.name)), h("td.num", d.entities.toLocaleString()), h("td.num", d.ymaps),
      h("td.num.muted", `${d.center[0].toFixed(0)}, ${d.center[1].toFixed(0)}`), h("td.num.muted", `${Math.round(d.radius)} m`)));
  }

  const status = h("div");
  const go = h("button.btn.primary", { onclick: submit }, opts.newProject ? "Convert as a new project" : "Convert this district");
  async function submit() {
    const body = { area: [Math.round(area[0] * 10) / 10, Math.round(area[1] * 10) / 10, Math.round(area[2])],
      name: nameIn.value.trim().toLowerCase(), display_name: dispIn.value.trim(), district: picked, new_project: !!opts.newProject };
    if (!/^[a-z][a-z0-9_]{2,47}$/.test(body.name)) return toast("the resource id: 3-48 characters a-z 0-9 _, starting with a letter", "err");
    go.disabled = true;
    const card = jobCard(`Looking at ${body.display_name}`, "The district's map files are read, its parts built and previews drawn (a few minutes for a 400 m district).");
    clear(status).append(card.el);
    try {
      const r = await api.post(`/projects/${opts.pid}/map/district`, body);
      const j = await follow(card, r.job.id);
      card.finish(j.state, j.error || "");
      if (j.state === "done") opts.onDone(r.project.id);
    } catch (e) {
      card.finish("failed");
      card.extra.append(h("div.err", e.errors && e.errors.length ? e.errors.join("; ") : e.message));
    } finally {
      go.disabled = false;
    }
  }

  names();
  draw();
  return h("div",
    h("div.split.city-split",
      h("div.card", h("div.row", h("h3", { style: { margin: 0 } }, "The city from above"), h("div.grow"), where),
        canvas,
        h("div.help", { style: { marginTop: "6px" } }, "Brighter is denser. Click to put the area's centre there; the labels are the districts (by their ymap names)"
          + (taken.length ? `; the dashed circles are the ${taken.length} converted already.` : "."))),
      h("div",
        h("div.card", h("h3", opts.newProject ? "Another district" : "The district to convert"),
          h("div.form", { style: { gridTemplateColumns: "repeat(2, minmax(0, 1fr))" } },
            h("label.field", "x (GTA, east)", xIn), h("label.field", "y (GTA, north)", yIn)),
          h("label.field", { style: { marginTop: "8px" } }, h("span", "Radius ", radiusVal), radius),
          est,
          h("div.form", { style: { marginTop: "8px" } },
            h("label.field", "Resource id on the server", nameIn), h("label.field", "Name players see", dispIn)),
          h("p.help", "Each district is a map of its own on the server (its own resource id), so several districts can stand side by side. "
            + "Around 300 to 500 m converts in a few minutes and stays light in game."),
          h("div.row", { style: { marginTop: "10px" } }, h("div.grow"), go), status),
        h("div.card", h("h3", `Districts (${districts.length})`),
          h("div.table-wrap", { style: { maxHeight: "300px", overflow: "auto" } },
            h("table.tbl", h("thead", h("tr", h("th", "District"), h("th.num", "Entities"), h("th.num", "ymaps"), h("th.num", "Centre"), h("th.num", "Size"))), listBody))))));
}
