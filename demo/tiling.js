// Tile geometry, visibility planning and decoder scheduling for the browser demo.
// A line-for-line port of core/ (Geometry.kt, Scheduler.kt); keep the two in step.
// Conventions: azimuth 0 looks along -Z, +X is right, u = 0.5 + az / 2pi; tile row 0 is the TOP of
// the picture; yaw about +Y is positive to the left, pitch positive looks up.

export const rad = (d) => (d * Math.PI) / 180;
export const tileKey = (t) => `${t.row}_${t.col}`;

export function azimuth(x, z) { return Math.atan2(x, -z); }
export function elevation(y) { return Math.asin(Math.max(-1, Math.min(1, y))); }
export function dirFromAngles(az, el) { return [Math.cos(el) * Math.sin(az), Math.sin(el), -Math.cos(el) * Math.cos(az)]; }

/** Rotation matrix (row-major 3x3) of yaw about +Y then pitch about X: view space -> world space. */
export function rotation(yaw, pitch) {
  const [cy, sy, cp, sp] = [Math.cos(yaw), Math.sin(yaw), Math.cos(pitch), Math.sin(pitch)];
  const ry = [cy, 0, sy, 0, 1, 0, -sy, 0, cy];
  const rx = [1, 0, 0, 0, cp, -sp, 0, sp, cp];
  const m = new Array(9).fill(0);
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) for (let k = 0; k < 3; k++) m[i * 3 + j] += ry[i * 3 + k] * rx[k * 3 + j];
  return m;
}
export function apply(m, v) {
  return [m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2], m[6] * v[0] + m[7] * v[1] + m[8] * v[2]];
}

export class Grid {
  constructor(cols, rows, tileWidth, tileHeight, pad) { Object.assign(this, { cols, rows, tileWidth, tileHeight, pad }); }
  get width() { return this.cols * this.tileWidth; }
  get height() { return this.rows * this.tileHeight; }
  get codedWidth() { return this.tileWidth + 2 * this.pad; }
  get codedHeight() { return this.tileHeight + 2 * this.pad; }
  all() { const r = []; for (let row = 0; row < this.rows; row++) for (let col = 0; col < this.cols; col++) r.push({ row, col }); return r; }

  tileAt(d) {
    const u = 0.5 + azimuth(d[0], d[2]) / (2 * Math.PI);
    const v = 0.5 - elevation(d[1]) / Math.PI;
    const col = ((Math.floor(u * this.cols) % this.cols) + this.cols) % this.cols;
    const row = Math.max(0, Math.min(this.rows - 1, Math.floor(v * this.rows)));
    return { row, col };
  }
  azimuthRange(t) { return [-Math.PI + (2 * Math.PI * t.col) / this.cols, -Math.PI + (2 * Math.PI * (t.col + 1)) / this.cols]; }
  /** [bottom, top] in radians. */
  elevationRange(t) { return [Math.PI / 2 - (Math.PI * (t.row + 1)) / this.rows, Math.PI / 2 - (Math.PI * t.row) / this.rows]; }
  centerDir(t) { const [a0, a1] = this.azimuthRange(t); const [e0, e1] = this.elevationRange(t); return dirFromAngles((a0 + a1) / 2, (e0 + e1) / 2); }
  /** [u0, v0, u1, v1] of the tile picture inside the padded tile video (v from the top). */
  innerRect() {
    const [cw, ch, p] = [this.codedWidth, this.codedHeight, this.pad];
    return [p / cw, p / ch, (p + this.tileWidth) / cw, (p + this.tileHeight) / ch];
  }
}

const angleBetween = (a, b) => Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));

/**
 * Tiles hit by a grid of rays across the view frustum, for each view pose ({yaw, pitch}); the first
 * pose is the current head pose, later ones are predictions. Sorted by the angle between the first
 * pose's forward axis and the tile centre; tiles seen only by later poses sort after the rest.
 */
export function planVisible(grid, views, halfH, halfV, margin = 0, raysPerAxis = 25) {
  const clamp = (x) => Math.min(x + margin, Math.PI * 0.49);
  const tx = Math.tan(clamp(halfH)), ty = Math.tan(clamp(halfV));
  const forward = apply(rotation(views[0].yaw, views[0].pitch), [0, 0, -1]);
  const best = new Map();
  views.forEach((view, index) => {
    const m = rotation(view.yaw, view.pitch);
    const found = new Map();
    for (let i = 0; i < raysPerAxis; i++) {
      const sx = -1 + (2 * i) / (raysPerAxis - 1);
      for (let j = 0; j < raysPerAxis; j++) {
        const sy = -1 + (2 * j) / (raysPerAxis - 1);
        const v = [tx * sx, ty * sy, -1];
        const n = Math.hypot(...v);
        const t = grid.tileAt(apply(m, [v[0] / n, v[1] / n, v[2] / n]));
        found.set(tileKey(t), t);
      }
    }
    for (const [k, t] of found) {
      const a = angleBetween(forward, grid.centerDir(t)) + (index === 0 ? 0 : Math.PI * index);
      if (!best.has(k) || best.get(k).angle > a) best.set(k, { ...t, angle: a });
    }
  });
  return [...best.values()].sort((a, b) => a.angle - b.angle);
}

/** Chooses which tiles hold a decoder (see Scheduler.kt). Times are in milliseconds. */
export class Scheduler {
  constructor(budget, lingerMs = 1500) { this.budget = budget; this.lingerMs = lingerMs; this.lastWanted = new Map(); }
  update(nowMs, wanted, active) {
    const wantedKeys = wanted.map(tileKey);
    for (const k of wantedKeys) this.lastWanted.set(k, nowMs);
    const keep = new Set(wantedKeys.slice(0, this.budget));
    [...active].filter((k) => !keep.has(k) && nowMs - (this.lastWanted.get(k) ?? -Infinity) <= this.lingerMs)
      .sort((a, b) => this.lastWanted.get(b) - this.lastWanted.get(a))
      .forEach((k) => { if (keep.size < this.budget) keep.add(k); });
    for (const k of [...this.lastWanted.keys()]) if (nowMs - this.lastWanted.get(k) > this.lingerMs && !active.has(k)) this.lastWanted.delete(k);
    return { start: [...keep].filter((k) => !active.has(k)), stop: [...active].filter((k) => !keep.has(k)) };
  }
}
