// Night City's 3D map data (the Studio's cache of the game's own world-map model, see
// world/citymap.py): loading, and the queries the 3D view needs -- the surfaces under a point
// (walking, draping), a ray against the city (picking, the cursor) and the buildings near a point
// (collisions).  Everything here is in Night City's world frame: metres, x east, y north, z up.
import { api } from "../api.js";

let cached = null;          // { key, promise }

// -> { meta, buf } (the cache is read once per Studio session and key)
export function loadCity(key, onProgress) {
  if (cached && cached.key === key) return cached.promise;
  const promise = (async () => {
    const meta = await api.get(`/citymap/citymap.json?k=${encodeURIComponent(key)}`);
    const r = await fetch(`/api/citymap/citymap.bin?k=${encodeURIComponent(key)}`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`the 3D city data did not load (HTTP ${r.status})`);
    const total = +(r.headers.get("Content-Length") || meta.bytes || 0);
    let buf;
    if (r.body && r.body.getReader && total) {
      const out = new Uint8Array(total);
      const rd = r.body.getReader();
      let at = 0;
      for (;;) {
        const { done, value } = await rd.read();
        if (done) break;
        out.set(value.subarray(0, Math.max(0, Math.min(value.length, total - at))), at);
        at += value.length;
        if (onProgress) onProgress(Math.min(1, at / total));
      }
      buf = out.buffer;
    } else {
      buf = await r.arrayBuffer();
    }
    return { meta, buf, grid: null };
  })();
  cached = { key, promise };
  promise.catch(() => { if (cached && cached.promise === promise) cached = null; });
  return promise;
}

export function view(buf, ref, Type) {
  const [off, count] = ref;
  return new Type(buf, off, count);
}

// ---------------------------------------------------------------------- surfaces
// Triangles bucketed on a 2D grid: what is under (x, y), and rays.
export class TriGrid {
  constructor(sources, cell = 32) {
    // sources: [{ pos: Float32Array (n*3), idx: Uint32Array (m*3), kind }]
    this.cell = cell;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const s of sources) {
      for (let i = 0; i < s.pos.length; i += 3) {
        const x = s.pos[i], y = s.pos[i + 1];
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
    }
    this.x0 = Math.floor(minX / cell) * cell;
    this.y0 = Math.floor(minY / cell) * cell;
    this.nx = Math.max(1, Math.ceil((maxX - this.x0) / cell) + 1);
    this.ny = Math.max(1, Math.ceil((maxY - this.y0) / cell) + 1);
    // one flat triangle table: 9 floats each, and its kind
    let ntri = 0;
    for (const s of sources) ntri += s.idx.length / 3;
    const T = new Float32Array(ntri * 9);
    const K = new Uint8Array(ntri);
    let t = 0;
    sources.forEach((s, si) => {
      const p = s.pos, ix = s.idx;
      for (let i = 0; i < ix.length; i += 3, t++) {
        for (let k = 0; k < 3; k++) {
          const v = ix[i + k] * 3;
          T[t * 9 + k * 3] = p[v]; T[t * 9 + k * 3 + 1] = p[v + 1]; T[t * 9 + k * 3 + 2] = p[v + 2];
        }
        K[t] = s.kind ?? si;
      }
    });
    this.T = T;
    this.K = K;
    const nc = this.nx * this.ny;
    const count = new Uint32Array(nc + 1);
    const maxZ = new Float32Array(nc).fill(-Infinity);
    const range = (tt) => {
      const o = tt * 9;
      const ax = Math.min(T[o], T[o + 3], T[o + 6]), bx = Math.max(T[o], T[o + 3], T[o + 6]);
      const ay = Math.min(T[o + 1], T[o + 4], T[o + 7]), by = Math.max(T[o + 1], T[o + 4], T[o + 7]);
      return [Math.max(0, Math.floor((ax - this.x0) / cell)), Math.min(this.nx - 1, Math.floor((bx - this.x0) / cell)),
        Math.max(0, Math.floor((ay - this.y0) / cell)), Math.min(this.ny - 1, Math.floor((by - this.y0) / cell))];
    };
    for (let tt = 0; tt < ntri; tt++) {
      const [i0, i1, j0, j1] = range(tt);
      const z = Math.max(T[tt * 9 + 2], T[tt * 9 + 5], T[tt * 9 + 8]);
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
        const c = j * this.nx + i;
        count[c + 1]++;
        if (z > maxZ[c]) maxZ[c] = z;
      }
    }
    for (let c = 0; c < nc; c++) count[c + 1] += count[c];
    const fill = count.slice(0, nc);
    const items = new Uint32Array(count[nc]);
    for (let tt = 0; tt < ntri; tt++) {
      const [i0, i1, j0, j1] = range(tt);
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) items[fill[j * this.nx + i]++] = tt;
    }
    this.start = count;
    this.items = items;
    this.maxZ = maxZ;
  }

  // the z of every triangle above (x, y) ([z, kind] pairs)
  surfaces(x, y, out = []) {
    out.length = 0;
    const i = Math.floor((x - this.x0) / this.cell), j = Math.floor((y - this.y0) / this.cell);
    if (i < 0 || j < 0 || i >= this.nx || j >= this.ny) return out;
    const c = j * this.nx + i, T = this.T;
    for (let k = this.start[c]; k < this.start[c + 1]; k++) {
      const o = this.items[k] * 9;
      const ax = T[o], ay = T[o + 1], bx = T[o + 3], by = T[o + 4], cx = T[o + 6], cy = T[o + 7];
      const d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
      if (Math.abs(d) < 1e-9) continue;
      const u = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d;
      const v = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d;
      const w = 1 - u - v;
      if (u < -1e-5 || v < -1e-5 || w < -1e-5) continue;
      out.push(u * T[o + 2] + v * T[o + 5] + w * T[o + 8], this.K[this.items[k]]);
    }
    return out;
  }

  // the highest surface at (x, y) not above zMax (Infinity: the top one); -Infinity when none
  heightAt(x, y, zMax = Infinity) {
    const s = this.surfaces(x, y, this._tmp || (this._tmp = []));
    let best = -Infinity;
    for (let k = 0; k < s.length; k += 2) if (s[k] <= zMax && s[k] > best) best = s[k];
    return best;
  }

  // nearest hit of the ray o + t d (t in (0, maxT]) -> { t, kind, tri } | null
  raycast(ox, oy, oz, dx, dy, dz, maxT = 40000) {
    const cell = this.cell;
    let best = null;
    // walk the cells the ray's 2D shadow crosses (DDA), nearest first
    let i = Math.floor((ox - this.x0) / cell), j = Math.floor((oy - this.y0) / cell);
    const stepI = dx > 0 ? 1 : -1, stepJ = dy > 0 ? 1 : -1;
    const tDeltaX = Math.abs(dx) > 1e-12 ? Math.abs(cell / dx) : Infinity;
    const tDeltaY = Math.abs(dy) > 1e-12 ? Math.abs(cell / dy) : Infinity;
    const nextX = this.x0 + (i + (dx > 0 ? 1 : 0)) * cell, nextY = this.y0 + (j + (dy > 0 ? 1 : 0)) * cell;
    let tMaxX = Math.abs(dx) > 1e-12 ? (nextX - ox) / dx : Infinity;
    let tMaxY = Math.abs(dy) > 1e-12 ? (nextY - oy) / dy : Infinity;
    let t0 = 0;
    const T = this.T;
    for (let steps = 0; steps < 4000 && t0 <= maxT; steps++) {
      const t1 = Math.min(tMaxX, tMaxY, maxT);
      if (i >= 0 && j >= 0 && i < this.nx && j < this.ny) {
        const c = j * this.nx + i;
        // the ray's lowest z inside this cell is above everything in it: nothing to hit
        const zLow = Math.min(oz + dz * t0, oz + dz * t1);
        if (zLow <= this.maxZ[c]) {
          for (let k = this.start[c]; k < this.start[c + 1]; k++) {
            const tri = this.items[k], o = tri * 9;
            const t = rayTri(ox, oy, oz, dx, dy, dz, T, o);
            if (t > 0 && t <= maxT && (!best || t < best.t)) best = { t, kind: this.K[tri], tri };
          }
          if (best && best.t <= t1 + 1e-6) return best;
        }
      } else if ((i < 0 && stepI < 0) || (j < 0 && stepJ < 0) || (i >= this.nx && stepI > 0) || (j >= this.ny && stepJ > 0)) {
        break;          // left the grid for good
      }
      t0 = t1;
      if (tMaxX < tMaxY) { tMaxX += tDeltaX; i += stepI; } else { tMaxY += tDeltaY; j += stepJ; }
    }
    return best;
  }
}

function rayTri(ox, oy, oz, dx, dy, dz, T, o) {
  const e1x = T[o + 3] - T[o], e1y = T[o + 4] - T[o + 1], e1z = T[o + 5] - T[o + 2];
  const e2x = T[o + 6] - T[o], e2y = T[o + 7] - T[o + 1], e2z = T[o + 8] - T[o + 2];
  const px = dy * e2z - dz * e2y, py = dz * e2x - dx * e2z, pz = dx * e2y - dy * e2x;
  const det = e1x * px + e1y * py + e1z * pz;
  if (Math.abs(det) < 1e-10) return -1;
  const inv = 1 / det;
  const sx = ox - T[o], sy = oy - T[o + 1], sz = oz - T[o + 2];
  const u = (sx * px + sy * py + sz * pz) * inv;
  if (u < 0 || u > 1) return -1;
  const qx = sy * e1z - sz * e1y, qy = sz * e1x - sx * e1z, qz = sx * e1y - sy * e1x;
  const v = (dx * qx + dy * qy + dz * qz) * inv;
  if (v < 0 || u + v > 1) return -1;
  return (e2x * qx + e2y * qy + e2z * qz) * inv;
}

// ---------------------------------------------------------------------- buildings
// Oriented boxes [cx cy cz hx hy hz qx qy qz qw]; a 2D grid of the ones over each cell.
export class BoxSet {
  constructor(data, cell = 64) {
    this.n = data.length / 10;
    this.d = data;
    this.cell = cell;
    const n = this.n;
    this.R = new Float32Array(n * 9);           // rotation (row-major: world = R local)
    this.E = new Float32Array(n * 3);           // world half extents (AABB)
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let b = 0; b < n; b++) {
      const o = b * 10;
      const [x, y, z, w] = [data[o + 6], data[o + 7], data[o + 8], data[o + 9]];
      const r = [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
        2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
        2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)];
      this.R.set(r, b * 9);
      for (let a = 0; a < 3; a++) {
        this.E[b * 3 + a] = Math.abs(r[a * 3]) * data[o + 3] + Math.abs(r[a * 3 + 1]) * data[o + 4] + Math.abs(r[a * 3 + 2]) * data[o + 5];
      }
      minX = Math.min(minX, data[o] - this.E[b * 3]); maxX = Math.max(maxX, data[o] + this.E[b * 3]);
      minY = Math.min(minY, data[o + 1] - this.E[b * 3 + 1]); maxY = Math.max(maxY, data[o + 1] + this.E[b * 3 + 1]);
    }
    if (!n) { minX = minY = 0; maxX = maxY = 1; }
    this.x0 = Math.floor(minX / cell) * cell;
    this.y0 = Math.floor(minY / cell) * cell;
    this.nx = Math.max(1, Math.ceil((maxX - this.x0) / cell) + 1);
    this.ny = Math.max(1, Math.ceil((maxY - this.y0) / cell) + 1);
    const nc = this.nx * this.ny;
    const count = new Uint32Array(nc + 1);
    const span = (b) => {
      const o = b * 10;
      return [Math.max(0, Math.floor((data[o] - this.E[b * 3] - this.x0) / cell)), Math.min(this.nx - 1, Math.floor((data[o] + this.E[b * 3] - this.x0) / cell)),
        Math.max(0, Math.floor((data[o + 1] - this.E[b * 3 + 1] - this.y0) / cell)), Math.min(this.ny - 1, Math.floor((data[o + 1] + this.E[b * 3 + 1] - this.y0) / cell))];
    };
    for (let b = 0; b < n; b++) {
      const [i0, i1, j0, j1] = span(b);
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) count[j * this.nx + i + 1]++;
    }
    for (let c = 0; c < nc; c++) count[c + 1] += count[c];
    const fill = count.slice(0, nc);
    this.items = new Uint32Array(count[nc]);
    for (let b = 0; b < n; b++) {
      const [i0, i1, j0, j1] = span(b);
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) this.items[fill[j * this.nx + i]++] = b;
    }
    this.start = count;
    this._seen = new Uint32Array(n);
    this._stamp = 0;
  }

  // indices of the boxes whose footprint may cover the disc (x, y, r)
  near(x, y, r, out = []) {
    out.length = 0;
    const s = ++this._stamp;
    const i0 = Math.max(0, Math.floor((x - r - this.x0) / this.cell)), i1 = Math.min(this.nx - 1, Math.floor((x + r - this.x0) / this.cell));
    const j0 = Math.max(0, Math.floor((y - r - this.y0) / this.cell)), j1 = Math.min(this.ny - 1, Math.floor((y + r - this.y0) / this.cell));
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
      const c = j * this.nx + i;
      for (let k = this.start[c]; k < this.start[c + 1]; k++) {
        const b = this.items[k];
        if (this._seen[b] === s) continue;
        this._seen[b] = s;
        out.push(b);
      }
    }
    return out;
  }

  // a point in box b's own frame
  local(b, x, y, z) {
    const o = b * 10, R = this.R, r = b * 9;
    const px = x - this.d[o], py = y - this.d[o + 1], pz = z - this.d[o + 2];
    return [R[r] * px + R[r + 3] * py + R[r + 6] * pz, R[r + 1] * px + R[r + 4] * py + R[r + 7] * pz, R[r + 2] * px + R[r + 5] * py + R[r + 8] * pz];
  }

  // the top of box b over (x, y) (its highest point there), or -Infinity if (x, y) is outside it
  topAt(b, x, y) {
    const o = b * 10, R = this.R, r = b * 9, d = this.d;
    // boxes are upright or turned by 90/180 degrees: the world z axis is one of the box axes
    const zc = [R[r + 6], R[r + 7], R[r + 8]];
    const a = zc.map(Math.abs);
    const ax = a[0] > a[1] ? (a[0] > a[2] ? 0 : 2) : (a[1] > a[2] ? 1 : 2);
    if (a[ax] < 0.98) return -Infinity;                      // a slanted box: no flat roof
    const l = this.local(b, x, y, d[o + 2]);
    const he = [d[o + 3], d[o + 4], d[o + 5]];
    for (let k = 0; k < 3; k++) if (k !== ax && Math.abs(l[k]) > he[k]) return -Infinity;
    return d[o + 2] + he[ax];
  }

  // nearest ray hit -> { t, box } | null (slab test in each box's frame)
  raycast(ox, oy, oz, dx, dy, dz, maxT = 40000) {
    let best = null;
    const tried = ++this._stamp;
    // walk the ray's cells as TriGrid does
    const cell = this.cell;
    let i = Math.floor((ox - this.x0) / cell), j = Math.floor((oy - this.y0) / cell);
    const stepI = dx > 0 ? 1 : -1, stepJ = dy > 0 ? 1 : -1;
    const tDeltaX = Math.abs(dx) > 1e-12 ? Math.abs(cell / dx) : Infinity;
    const tDeltaY = Math.abs(dy) > 1e-12 ? Math.abs(cell / dy) : Infinity;
    let tMaxX = Math.abs(dx) > 1e-12 ? (this.x0 + (i + (dx > 0 ? 1 : 0)) * cell - ox) / dx : Infinity;
    let tMaxY = Math.abs(dy) > 1e-12 ? (this.y0 + (j + (dy > 0 ? 1 : 0)) * cell - oy) / dy : Infinity;
    let t0 = 0;
    for (let steps = 0; steps < 3000 && t0 <= maxT; steps++) {
      const t1 = Math.min(tMaxX, tMaxY, maxT);
      if (i >= 0 && j >= 0 && i < this.nx && j < this.ny) {
        const c = j * this.nx + i;
        for (let k = this.start[c]; k < this.start[c + 1]; k++) {
          const b = this.items[k];
          if (this._seen[b] === tried) continue;
          this._seen[b] = tried;
          const t = this.rayBox(b, ox, oy, oz, dx, dy, dz);
          if (t > 0 && t <= maxT && (!best || t < best.t)) best = { t, box: b };
        }
        if (best && best.t <= t1) return best;
      } else if ((i < 0 && stepI < 0) || (j < 0 && stepJ < 0) || (i >= this.nx && stepI > 0) || (j >= this.ny && stepJ > 0)) {
        break;
      }
      t0 = t1;
      if (tMaxX < tMaxY) { tMaxX += tDeltaX; i += stepI; } else { tMaxY += tDeltaY; j += stepJ; }
    }
    return best;
  }

  rayBox(b, ox, oy, oz, dx, dy, dz) {
    const o = b * 10, R = this.R, r = b * 9, d = this.d;
    const l = this.local(b, ox, oy, oz);
    const ld = [R[r] * dx + R[r + 3] * dy + R[r + 6] * dz, R[r + 1] * dx + R[r + 4] * dy + R[r + 7] * dz, R[r + 2] * dx + R[r + 5] * dy + R[r + 8] * dz];
    let tn = -Infinity, tf = Infinity;
    for (let k = 0; k < 3; k++) {
      const he = d[o + 3 + k];
      if (Math.abs(ld[k]) < 1e-12) {
        if (Math.abs(l[k]) > he) return -1;
        continue;
      }
      let ta = (-he - l[k]) / ld[k], tb = (he - l[k]) / ld[k];
      if (ta > tb) [ta, tb] = [tb, ta];
      if (ta > tn) tn = ta;
      if (tb < tf) tf = tb;
      if (tn > tf) return -1;
    }
    return tn > 0 ? tn : (tf > 0 ? 0 : -1);
  }
}

// polygon helpers (district outlines: [[x, y, z], ...])
export function insidePoly(poly, x, y) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) c = !c;
  }
  return c;
}

export function polyCentroid(poly) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [x0, y0] = poly[j], [x1, y1] = poly[i];
    const f = x0 * y1 - x1 * y0;
    a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f;
  }
  if (Math.abs(a) < 1e-6) {
    const n = poly.length || 1;
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n];
  }
  return [cx / (3 * a), cy / (3 * a)];
}
