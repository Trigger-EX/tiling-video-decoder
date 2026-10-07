// Small statistics helpers for the benchmark (pure, tested with node).

export function percentile(sorted, p) {
  if (!sorted.length) return null;
  const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

/** Frame intervals in milliseconds. */
export class FrameStats {
  constructor() { this.dts = []; }
  add(ms) { this.dts.push(ms); }
  summary(hitchMs = 33.4) {
    const s = [...this.dts].sort((a, b) => a - b);
    const total = this.dts.reduce((a, b) => a + b, 0);
    return {
      frames: s.length,
      fps: total ? (1000 * s.length) / total : 0,
      p50: percentile(s, 0.5), p95: percentile(s, 0.95), p99: percentile(s, 0.99), max: s.length ? s[s.length - 1] : null,
      hitches: s.filter((x) => x > hitchMs).length,      // frames that took longer than ~2 refreshes at 60 Hz
    };
  }
}

/** Time-weighted mean of a value that changes over time. */
export class TimeMean {
  constructor() { this.sum = 0; this.time = 0; }
  add(value, seconds) { this.sum += value * seconds; this.time += seconds; }
  mean() { return this.time ? this.sum / this.time : null; }
}

/** Order of benchmark windows: alternate which mode goes first each round so drift/thermals cancel. */
export function benchOrder(rounds) {
  const out = [];
  for (let r = 0; r < rounds; r++) for (const mode of (r % 2 ? ['tiled', 'full'] : ['full', 'tiled'])) out.push({ mode, round: r + 1 });
  return out;
}

/** Pixels the decoders must produce per second. */
export function decodedMpxPerSecond({ mode, slots, grid, base, fps, fullWidth, fullHeight }) {
  const px = mode === 'full' ? fullWidth * fullHeight : slots * grid.codedWidth * grid.codedHeight + base.width * base.height;
  return (px * fps) / 1e6;
}
