import test from 'node:test';
import assert from 'node:assert/strict';
import { percentile, FrameStats, TimeMean, benchOrder, decodedMpxPerSecond } from '../demo/perf.js';
import { Grid } from '../demo/tiling.js';

test('percentiles interpolate', () => {
  assert.equal(percentile([1, 2, 3, 4, 5], 0.5), 3);
  assert.equal(percentile([10, 20], 0.5), 15);
  assert.equal(percentile([], 0.5), null);
});

test('frame stats count hitches and fps', () => {
  const f = new FrameStats();
  for (let i = 0; i < 98; i++) f.add(16.67);
  f.add(50); f.add(120);
  const s = f.summary();
  assert.equal(s.frames, 100); assert.equal(s.hitches, 2); assert.equal(s.max, 120);
  assert.ok(Math.abs(s.p50 - 16.67) < 1e-9);
  assert.ok(s.fps > 55 && s.fps < 60);
});

test('time mean weights by duration', () => {
  const m = new TimeMean(); m.add(10, 1); m.add(0, 3);
  assert.equal(m.mean(), 2.5);
  assert.equal(new TimeMean().mean(), null);
});

test('benchmark alternates mode order between rounds', () => {
  assert.deepEqual(benchOrder(2).map((w) => w.mode), ['full', 'tiled', 'tiled', 'full']);
  assert.deepEqual(benchOrder(1).map((w) => w.round), [1, 1]);
});

test('decoded pixel rate model matches the viewer panel (12 slots of 8x4 3840x1920 -> 54%)', () => {
  const grid = new Grid(8, 4, 480, 480, 16);
  const common = { grid, base: { width: 1280, height: 640 }, fps: 30, fullWidth: 3840, fullHeight: 1920 };
  const full = decodedMpxPerSecond({ ...common, mode: 'full', slots: 0 });
  const tiled = decodedMpxPerSecond({ ...common, mode: 'tiled', slots: 12 });
  assert.ok(Math.abs(full - 221.184) < 1e-6);
  assert.ok(Math.abs(tiled / full - 0.5378) < 0.001);
});
