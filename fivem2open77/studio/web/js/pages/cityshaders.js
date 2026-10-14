// GLSL of the 3D city.  Every shader works in Night City's frame (the city group turns it to
// three.js' y-up): vG is the game position (x east, y north, z up).  All write the logarithmic
// depth of the renderer (a 16 km city and a 1.8 m walker share one depth buffer) and fade into the
// night fog themselves.
export const LIB = /* glsl */ `
uniform float uTime;
uniform vec3 uFogColor;
uniform float uFogDensity;
uniform vec4 uPulse;      // x, y (game), start time, strength
uniform vec3 uCursor;     // x, y (game), on
float hash12(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * .1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float hash13(vec3 p3) { p3 = fract(p3 * .1031); p3 += dot(p3, p3.zyx + 31.32); return fract((p3.x + p3.y) * p3.z); }
// lines every 'step' metres, 'halfw' metres wide (at least a pixel), fading out before they alias
float gridLine(vec2 p, float step, float halfw) {
  vec2 d = abs(fract(p / step - 0.5) - 0.5) * step;
  vec2 fw = max(fwidth(p), vec2(1e-4));
  vec2 l = 1.0 - smoothstep(vec2(halfw), vec2(halfw) + fw * 1.5, d);
  float fade = 1.0 - smoothstep(step * 0.08, step * 0.3, max(fw.x, fw.y));
  return max(l.x, l.y) * fade;
}
vec3 fogged(vec3 c, float dist) {
  float f = 1.0 - exp(-pow(dist * uFogDensity, 2.0));
  return mix(c, uFogColor, clamp(f, 0.0, 1.0));
}
// the scan wave sent out from a spot (a map is focused, a spot is set)
float pulseAt(vec2 g) {
  float t = uTime - uPulse.z;
  if (uPulse.w <= 0.0 || t < 0.0 || t > 5.0) return 0.0;
  float r = distance(g, uPulse.xy);
  float R = t * 750.0;
  return exp(-pow((r - R) / 28.0, 2.0)) * (1.0 - t / 5.0) * uPulse.w + exp(-pow((r - R * 0.6) / 10.0, 2.0)) * (1.0 - t / 5.0) * 0.35 * uPulse.w;
}
float cursorAt(vec2 g) {
  if (uCursor.z <= 0.0) return 0.0;
  float d = distance(g, uCursor.xy);
  float ring = exp(-pow((d - 26.0) / 2.2, 2.0)) * (0.75 + 0.25 * sin(uTime * 6.0));
  float inner = exp(-pow((d - 9.0) / 1.2, 2.0)) * 0.6;
  vec2 q = abs(g - uCursor.xy);
  float cross = (1.0 - smoothstep(0.4, 0.4 + fwidth(q.x) * 1.5, q.x)) * step(q.y, 60.0) * step(14.0, q.y)
              + (1.0 - smoothstep(0.4, 0.4 + fwidth(q.y) * 1.5, q.y)) * step(q.x, 60.0) * step(14.0, q.x);
  return ring + inner + cross * 0.7 + exp(-d * d / 900.0) * 0.18;
}
`;

const VHEAD = /* glsl */ `
#include <common>
#include <logdepthbuf_pars_vertex>
`;
const FHEAD = /* glsl */ `
#include <common>
#include <logdepthbuf_pars_fragment>
`;

// ---------------------------------------------------------------- terrain and water
export const terrainVS = VHEAD + /* glsl */ `
varying vec3 vG; varying vec3 vN; varying float vDist;
void main() {
  vG = position; vN = normal;
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vDist = distance(wp.xyz, cameraPosition);
  gl_Position = projectionMatrix * viewMatrix * wp;
  #include <logdepthbuf_vertex>
}`;

export const terrainFS = FHEAD + LIB + /* glsl */ `
uniform float uGrid;
varying vec3 vG; varying vec3 vN; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  vec3 n = normalize(vN);
  float h = vG.z;
  vec3 col = mix(vec3(0.0025, 0.005, 0.011), vec3(0.009, 0.019, 0.03), smoothstep(-25.0, 420.0, h));
  float diff = max(dot(n, normalize(vec3(-0.38, 0.42, 0.82))), 0.0);
  col *= 0.5 + 0.95 * diff;
  col += vec3(0.0, 0.03, 0.05) * smoothstep(0.25, 0.85, 1.0 - n.z);          // slopes, cliffs
  col *= mix(0.55, 1.0, smoothstep(-12.0, -2.0, h));                         // under the sea
  // the survey grid: 100 m, and every kilometre brighter
  float g1 = gridLine(vG.xy, 100.0, 0.45) * (1.0 - smoothstep(1200.0, 5000.0, vDist));
  float g2 = gridLine(vG.xy, 1000.0, 1.2);
  col += (vec3(0.03, 0.26, 0.36) * g1 * 0.3 + vec3(0.06, 0.45, 0.65) * g2 * 0.22) * uGrid;
  // height lines every 20 m
  float c = abs(fract(h / 20.0 - 0.5) - 0.5) * 20.0;
  float cw = max(fwidth(h), 1e-4);
  col += vec3(0.0, 0.2, 0.24) * (1.0 - smoothstep(0.0, cw * 1.5, c)) * 0.22 * (1.0 - smoothstep(1.5, 8.0, cw)) * uGrid;
  col += vec3(0.25, 0.95, 1.0) * pulseAt(vG.xy) * 1.2;
  col += vec3(0.3, 1.0, 0.9) * cursorAt(vG.xy);
  gl_FragColor = vec4(fogged(col, vDist), 1.0);
}`;

export const waterFS = FHEAD + LIB + /* glsl */ `
varying vec3 vG; varying vec3 vN; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  vec3 col = vec3(0.004, 0.012, 0.03);
  // drifting scan lines and a sparse glint grid
  float s = abs(fract((vG.y + vG.x * 0.25) / 18.0 - uTime * 0.035) - 0.5);
  col += vec3(0.0, 0.06, 0.1) * (1.0 - smoothstep(0.0, 0.08 + fwidth(vG.y) / 18.0, s)) * (1.0 - smoothstep(600.0, 3500.0, vDist));
  float g = gridLine(vG.xy, 250.0, 0.6);
  col += vec3(0.02, 0.14, 0.22) * g * 0.6;
  float glint = step(0.985, hash12(floor(vG.xy / 24.0) + floor(uTime * 0.5))) * (1.0 - smoothstep(300.0, 2000.0, vDist));
  col += vec3(0.2, 0.6, 0.9) * glint * 0.25;
  col += vec3(0.25, 0.95, 1.0) * pulseAt(vG.xy) * 0.8;
  col += vec3(0.3, 1.0, 0.9) * cursorAt(vG.xy) * 0.8;
  gl_FragColor = vec4(fogged(col, vDist), 1.0);
}`;

// ---------------------------------------------------------------- roads, kerbs, metro (glow)
export const glowVS = VHEAD + /* glsl */ `
attribute vec4 aCol;
uniform float uLift;
varying vec3 vG; varying float vDist; varying vec4 vC;
void main() {
  vec3 p = position;
  vec4 w0 = modelMatrix * vec4(p, 1.0);
  float d = distance(w0.xyz, cameraPosition);
  p.z += uLift + d * 0.0006;                     // over the ground (more far away: depth)
  vG = p; vC = aCol;
  vec4 wp = modelMatrix * vec4(p, 1.0);
  vDist = d;
  gl_Position = projectionMatrix * viewMatrix * wp;
  #include <logdepthbuf_vertex>
}`;

export const roadFS = FHEAD + LIB + /* glsl */ `
uniform vec3 uColor; uniform vec3 uColor2; uniform float uIntensity;
varying vec3 vG; varying float vDist; varying vec4 vC;
void main() {
  #include <logdepthbuf_fragment>
  float major = smoothstep(0.5, 0.9, vC.r) * (1.0 - smoothstep(0.2, 0.5, vC.g));
  vec3 base = mix(uColor, uColor2, major);
  // traffic: light packets running through the streets
  float lane = sin((vG.x + vG.y) * 0.045 - uTime * 2.6) * sin((vG.x - vG.y) * 0.037 + uTime * 1.9);
  float packets = smoothstep(0.9, 1.0, lane) * smoothstep(60.0, 300.0, vDist) * (1.0 - smoothstep(600.0, 3000.0, vDist));
  float flick = 0.9 + 0.1 * sin(uTime * 1.7 + hash12(floor(vG.xy / 60.0)) * 6.283);
  float far = 1.0 - 0.45 * smoothstep(1500.0, 9000.0, vDist);         // dense far streets do not add up to white
  far *= mix(0.12, 1.0, smoothstep(20.0, 450.0, vDist));               // up close a street is dark tarmac with a sheen
  vec3 col = base * uIntensity * flick * far + mix(vec3(1.0, 0.85, 0.6), base, 0.35) * packets * 0.45;
  col += vec3(0.4, 1.0, 1.0) * pulseAt(vG.xy) * 1.5;
  col *= 1.0 - smoothstep(9000.0, 22000.0, vDist);
  gl_FragColor = vec4(col, 1.0);
}`;

export const metroFS = FHEAD + LIB + /* glsl */ `
uniform float uIntensity;
varying vec3 vG; varying float vDist; varying vec4 vC;
void main() {
  #include <logdepthbuf_fragment>
  // NCART lines keep the game's colours (red, green, blue in the data)
  vec3 c = vC.r > 0.5 ? vec3(1.0, 0.14, 0.38) : (vC.g > 0.5 ? vec3(0.22, 1.0, 0.5) : vec3(0.2, 0.62, 1.0));
  float train = smoothstep(0.93, 1.0, sin((vG.x * 0.8 + vG.y * 0.6) * 0.02 - uTime * 3.2));
  vec3 col = c * uIntensity * (0.85 + 0.15 * sin(uTime * 3.0)) + vec3(1.0) * train * 0.9;
  col *= 1.0 - smoothstep(9000.0, 22000.0, vDist);
  gl_FragColor = vec4(col, 1.0);
}`;

// ---------------------------------------------------------------- solid holograms (cliffs, landmarks)
export const solidFS = FHEAD + LIB + /* glsl */ `
uniform vec3 uColor; uniform vec3 uRim;
varying vec3 vG; varying vec3 vN; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  vec3 n = normalize(vN);
  float diff = max(dot(n, normalize(vec3(-0.38, 0.42, 0.82))), 0.0);
  vec3 col = uColor * (0.25 + 0.6 * diff);
  float scan = 1.0 - smoothstep(0.0, 0.15 + fwidth(vG.z) * 0.6, abs(fract(vG.z / 4.0) - 0.5));
  col += uRim * scan * 0.18 * (1.0 - smoothstep(300.0, 2500.0, vDist));
  float up = smoothstep(0.6, 0.95, n.z);
  col += uRim * up * 0.12;
  col += vec3(0.25, 0.95, 1.0) * pulseAt(vG.xy);
  gl_FragColor = vec4(fogged(col, vDist), 1.0);
}`;

// ---------------------------------------------------------------- buildings (instanced boxes)
export const buildingVS = VHEAD + /* glsl */ `
attribute vec3 aSize; attribute vec3 aTint; attribute float aSeed; attribute float aId;
varying vec3 vL; varying vec3 vS; varying vec3 vNl; varying vec3 vG; varying vec3 vW; varying vec3 vNw;
varying float vDist; varying float vSeed; varying float vId; varying vec3 vTint;
void main() {
  vL = position; vS = aSize; vNl = normal; vSeed = aSeed; vId = aId; vTint = aTint;
  vec4 gp = instanceMatrix * vec4(position, 1.0);
  vG = gp.xyz;
  vec4 wp = modelMatrix * gp;
  vW = wp.xyz;
  vNw = normalize(mat3(modelMatrix) * mat3(instanceMatrix) * normal);
  vDist = distance(wp.xyz, cameraPosition);
  gl_Position = projectionMatrix * viewMatrix * wp;
  #include <logdepthbuf_vertex>
}`;

export const buildingFS = FHEAD + LIB + /* glsl */ `
uniform float uHover; uniform float uHolo; uniform float uTint;
varying vec3 vL; varying vec3 vS; varying vec3 vNl; varying vec3 vG; varying vec3 vW; varying vec3 vNw;
varying float vDist; varying float vSeed; varying float vId; varying vec3 vTint;
void main() {
  #include <logdepthbuf_fragment>
  vec3 m = (vL + 0.5) * vS;                  // metres from the box's own corner
  vec3 an = abs(vNl);
  vec2 uv; vec2 sz;
  if (an.z > 0.5) { uv = m.xy; sz = vS.xy; }
  else if (an.x > 0.5) { uv = m.yz; sz = vS.yz; }
  else { uv = m.xz; sz = vS.xz; }
  vec2 fw = max(fwidth(uv), vec2(1e-4));
  vec2 e2 = min(uv, sz - uv);
  float ew = 0.3 + vDist * 0.00045;
  vec2 el = 1.0 - smoothstep(vec2(ew), vec2(ew) + fw * 1.5, e2);
  float edge = max(el.x, el.y);
  float top = step(0.5, vNl.z);
  float side = 1.0 - step(0.5, an.z);
  float hgt = vS.z;
  float tall = smoothstep(70.0, 260.0, hgt);
  vec3 cyan = vec3(0.0, 0.78, 1.0);
  vec3 accent = mix(cyan, vTint, uTint * 0.55);
  accent = mix(accent, vec3(1.0, 0.2, 0.62), tall * 0.45);
  float vf = side > 0.5 ? clamp(uv.y / max(sz.y, 1.0), 0.0, 1.0) : 1.0;
  // (linear colours: these are very dark navies on screen)
  vec3 col = mix(vec3(0.0012, 0.0024, 0.0065), vec3(0.0035, 0.0075, 0.017), vf);
  // floors and windows: some lit, flickering; far away they melt into a soft glow
  vec2 cellSz = vec2(2.9, 3.7);
  vec2 cell = uv / cellSz;
  vec2 f = fract(cell);
  vec2 id = floor(cell);
  float detail = (1.0 - smoothstep(0.3, 0.85, max(fw.x / cellSz.x, fw.y / cellSz.y))) * side;
  float win = step(0.16, f.x) * step(f.x, 0.84) * step(0.2, f.y) * step(f.y, 0.76);
  float lit = step(0.83 - 0.12 * vSeed, hash13(vec3(id, floor(vSeed * 977.0))));
  vec3 wcol = mix(vec3(1.0, 0.72, 0.34), vec3(0.35, 0.92, 1.0), step(0.55, hash12(id + vSeed * 31.0)));
  wcol = mix(wcol, vec3(1.0, 0.3, 0.7), step(0.92, hash12(id.yx + vSeed * 7.0)));
  float flick = 0.8 + 0.2 * sin(uTime * (0.6 + hash12(id) * 2.5) + hash12(id.yx) * 6.283);
  col += detail * win * lit * wcol * 0.6 * flick;
  col += detail * win * (1.0 - lit) * vec3(0.002, 0.006, 0.012);         // dark glass
  col += side * (1.0 - detail) * wcol * 0.022 * (0.6 + vSeed);           // far: the windows as one glow
  // a roof band and the edges
  float farE = mix(1.0, 0.4, smoothstep(700.0, 6000.0, vDist));
  col += accent * edge * (top > 0.5 ? 1.25 : 0.8) * (0.4 + 0.6 * vf) * farE;
  col += top * accent * (0.006 + 0.012 * tall);
  vec3 V = normalize(cameraPosition - vW);
  float fres = pow(1.0 - max(dot(normalize(vNw), V), 0.0), 3.0);
  col += accent * fres * 0.06;
  // a scan sweep climbing the towers
  float sweep = exp(-pow((vG.z - mod(uTime * 45.0, 700.0) + 50.0) / 2.5, 2.0));
  col += accent * sweep * side * 0.5;
  col += vec3(0.3, 1.0, 1.0) * pulseAt(vG.xy) * (0.6 + edge);
  if (abs(vId - uHover) < 0.5) col += vec3(0.45, 1.0, 1.0) * (0.035 + edge * 1.1) + vec3(0.03, 0.12, 0.15) * win * detail;
  col = fogged(col, vDist);
  float a = 1.0;
  if (uHolo > 0.5) {
    a = clamp(0.16 + edge * 0.84 + fres * 0.3 + top * 0.12, 0.0, 1.0);
    col *= 1.25;
  }
  gl_FragColor = vec4(col, a);
}`;

// ---------------------------------------------------------------- glowing lines (districts, footprints)
export const lineVS = VHEAD + /* glsl */ `
attribute vec2 aDir; attribute float aSide; attribute float aAlong;
uniform float uWidth; uniform float uPx; uniform float uPxScale;
varying float vSide; varying float vAlong; varying float vDist; varying vec3 vG;
void main() {
  vec4 w0 = modelMatrix * vec4(position, 1.0);
  float d = distance(w0.xyz, cameraPosition);
  float w = max(uWidth, d * uPxScale * uPx);
  vec2 perp = normalize(vec2(-aDir.y, aDir.x) + 1e-6);
  vec3 p = position + vec3(perp * aSide * w * 0.5, 0.0);
  vSide = aSide; vAlong = aAlong; vDist = d;
  vG = (modelMatrix * vec4(p, 1.0)).xyz;
  gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(p, 1.0);
  #include <logdepthbuf_vertex>
}`;

export const lineFS = FHEAD + LIB + /* glsl */ `
uniform vec3 uColor; uniform float uIntensity; uniform float uDash; uniform float uFlow;
varying float vSide; varying float vAlong; varying float vDist; varying vec3 vG;
void main() {
  #include <logdepthbuf_fragment>
  float prof = exp(-vSide * vSide * 2.6);
  float dash = uDash > 0.0 ? step(fract(vAlong / uDash), 0.62) : 1.0;
  float flow = uFlow > 0.0 ? 0.65 + 0.35 * sin(vAlong * 0.03 - uTime * uFlow) : 1.0;
  vec3 col = uColor * uIntensity * prof * dash * flow;
  gl_FragColor = vec4(col, 1.0);
}`;

// ---------------------------------------------------------------- markers (beams, footprints, rings)
export const beamVS = VHEAD + /* glsl */ `
uniform float uPxScale; uniform float uRadius;
varying vec2 vUv; varying float vDist;
void main() {
  vUv = uv;
  vec4 c = modelMatrix * vec4(0.0, 0.0, position.z, 1.0);
  float d = distance(c.xyz, cameraPosition);
  float r = max(uRadius, d * uPxScale * 3.2);
  vec3 p = vec3(position.xy * r, position.z);
  vDist = d;
  gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(p, 1.0);
  #include <logdepthbuf_vertex>
}`;

export const beamFS = FHEAD + /* glsl */ `
uniform float uTime; uniform vec3 uColor; uniform float uIntensity;
varying vec2 vUv; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  float y = vUv.y;
  float fade = pow(1.0 - y, 1.6);
  float bands = 0.6 + 0.4 * smoothstep(0.6, 1.0, sin(y * 90.0 - uTime * 5.0));
  float core = 0.5 + 0.5 * pow(abs(sin(vUv.x * 3.14159 * 2.0)), 0.5);
  vec3 col = uColor * uIntensity * fade * bands * (0.6 + 0.4 * core);
  gl_FragColor = vec4(col, 1.0);
}`;

export const padVS = VHEAD + /* glsl */ `
varying vec2 vP; varying float vDist;
uniform vec2 uHalf;
void main() {
  vP = position.xy;
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vDist = distance(wp.xyz, cameraPosition);
  gl_Position = projectionMatrix * viewMatrix * wp;
  #include <logdepthbuf_vertex>
}`;

// a map's footprint: a faint grid, the outline, corner brackets, a scan line
export const padFS = FHEAD + /* glsl */ `
uniform float uTime; uniform vec3 uColor; uniform vec2 uMin; uniform vec2 uMax; uniform float uIntensity; uniform float uPxScale;
varying vec2 vP; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  vec2 size = uMax - uMin;
  vec2 q = vP - uMin;
  vec2 fw = max(fwidth(vP), vec2(1e-4));
  vec2 e = min(q, size - q);
  float w = max(0.25, vDist * uPxScale * 1.4);
  float outline = 1.0 - smoothstep(w, w + fw.x * 1.5, min(e.x, e.y));
  float bl = max(min(size.x, size.y) * 0.18, w * 6.0);
  float corner = step(min(e.x, e.y), w * 2.6) * step(max(e.x, e.y), bl);
  vec2 g = abs(fract(q / 5.0 - 0.5) - 0.5) * 5.0;
  float grid = (1.0 - smoothstep(0.06, 0.06 + max(fw.x, fw.y) * 1.5, min(g.x, g.y))) * (1.0 - smoothstep(60.0, 400.0, vDist));
  float scanY = fract(uTime * 0.25) * size.y;
  float scan = exp(-pow((q.y - scanY) / max(0.6, size.y * 0.01), 2.0));
  vec3 col = uColor * uIntensity * (0.05 + grid * 0.12 + outline * 0.9 + corner * 1.6 + scan * 0.35);
  gl_FragColor = vec4(col, 1.0);
}`;

export const ringFS = FHEAD + /* glsl */ `
uniform float uTime; uniform vec3 uColor; uniform float uIntensity; uniform float uPhase;
varying vec2 vP; varying float vDist;
void main() {
  #include <logdepthbuf_fragment>
  float r = length(vP);
  float t = fract(uTime * 0.45 + uPhase);
  float R = t;
  float ring = exp(-pow((r - R) / 0.035, 2.0)) * (1.0 - t);
  float inner = exp(-pow((r - 0.08) / 0.02, 2.0)) * 0.8;
  vec3 col = uColor * uIntensity * (ring + inner);
  gl_FragColor = vec4(col, 1.0);
}`;

// ---------------------------------------------------------------- the night sky
export const skyVS = /* glsl */ `
varying vec3 vDir;
void main() {
  vDir = normalize(position);
  vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  gl_Position = p.xyww;
}`;

export const skyFS = /* glsl */ `
uniform float uTime;
varying vec3 vDir;
float hash13(vec3 p3) { p3 = fract(p3 * .1031); p3 += dot(p3, p3.zyx + 31.32); return fract((p3.x + p3.y) * p3.z); }
void main() {
  vec3 d = normalize(vDir);
  float up = clamp(d.y, -1.0, 1.0);
  vec3 zenith = vec3(0.004, 0.006, 0.016), horizon = vec3(0.03, 0.035, 0.07);
  vec3 col = mix(horizon, zenith, smoothstep(0.0, 0.55, up));
  // the city's glow on the haze: magenta to the west, cyan to the east
  float az = atan(d.x, -d.z);
  float band = exp(-pow(up / 0.09, 2.0));
  col += band * mix(vec3(0.16, 0.03, 0.12), vec3(0.02, 0.1, 0.14), 0.5 + 0.5 * sin(az)) * 0.9;
  col += exp(-pow((up + 0.02) / 0.025, 2.0)) * vec3(0.05, 0.08, 0.12);
  // stars
  vec3 s = floor(d * 380.0);
  float st = step(0.9975, hash13(s)) * smoothstep(0.05, 0.4, up);
  col += st * (0.35 + 0.65 * hash13(s + 1.7)) * (0.7 + 0.3 * sin(uTime * (1.0 + hash13(s) * 3.0))) * vec3(0.7, 0.8, 1.0);
  gl_FragColor = vec4(col, 1.0);
}`;
