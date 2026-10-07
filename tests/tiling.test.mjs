import test from 'node:test';
import assert from 'node:assert/strict';
import { Grid, planVisible, Scheduler, rotation, apply, azimuth, elevation, dirFromAngles, rad, tileKey } from '../demo/tiling.js';

const grid = new Grid(8, 4, 1024, 1024, 16);
const fov = rad(50);
const level = [{ yaw: 0, pitch: 0 }];

test('tile lookup matches the Kotlin core', () => {
  assert.equal(grid.tileAt(dirFromAngles(rad(70), 0)).col, 5);
  assert.equal(grid.tileAt([0.001, 0, -1]).col, 4);
  assert.equal(grid.tileAt([-0.001, 0, -1]).col, 3);
  assert.equal(grid.tileAt(dirFromAngles(0.1, rad(80))).row, 0);
  assert.equal(grid.tileAt(dirFromAngles(Math.PI - 0.01, 0.1)).col, 7);
  assert.equal(grid.tileAt(dirFromAngles(-Math.PI + 0.01, 0.1)).col, 0);
  for (const t of grid.all()) assert.deepEqual(grid.tileAt(grid.centerDir(t)), t);
});

test('yaw turns left, pitch looks up', () => {
  const left = apply(rotation(rad(90), 0), [0, 0, -1]);
  assert.ok(left[0] < -0.99);
  const both = apply(rotation(rad(90), rad(45)), [0, 0, -1]);
  assert.ok(Math.abs(elevation(both[1]) - rad(45)) < 1e-9);
  assert.ok(Math.abs(azimuth(both[0], both[2]) - rad(-90)) < 1e-9);
});

test('level view of a 100x100 degree field sees 12 tiles, centre first', () => {
  const tiles = planVisible(grid, level, fov, fov);
  assert.equal(tiles.length, 12);
  assert.deepEqual(new Set(tiles.map((t) => t.row)), new Set([0, 1, 2, 3]));
  assert.ok([3, 4].includes(tiles[0].col) && [1, 2].includes(tiles[0].row));
  assert.deepEqual(tiles.map((t) => t.angle), tiles.map((t) => t.angle).sort((a, b) => a - b));
});

test('looking back wraps; margin and prediction add tiles after the current ones', () => {
  const cols = new Set(planVisible(grid, [{ yaw: Math.PI, pitch: 0 }], fov, fov).map((t) => t.col));
  assert.ok(cols.has(0) && cols.has(7));
  const base = planVisible(grid, level, fov, fov).map(tileKey);
  const predicted = planVisible(grid, [...level, { yaw: rad(-40), pitch: 0 }], fov, fov).map(tileKey);
  assert.ok(predicted.length > base.length);
  assert.deepEqual(new Set(predicted.slice(0, base.length)), new Set(base));
  assert.ok(planVisible(grid, level, fov, fov, rad(20)).length > base.length);
});

test('scheduler respects the budget, lingers, and evicts lower-ranked tiles first', () => {
  const t = (c) => ({ row: 0, col: c });
  let s = new Scheduler(3);
  assert.deepEqual(s.update(0, [t(0), t(1), t(2), t(3), t(4)], new Set()).start, ['0_0', '0_1', '0_2']);
  s = new Scheduler(4, 1000);
  s.update(0, [t(0), t(1)], new Set());
  const still = s.update(500, [t(1)], new Set(['0_0', '0_1']));
  assert.deepEqual([still.start, still.stop], [[], []]);
  assert.deepEqual(s.update(2000, [t(1)], new Set(['0_0', '0_1'])).stop, ['0_0']);
  s = new Scheduler(2);
  s.update(0, [t(0), t(1)], new Set());
  const d = s.update(10, [t(2), t(1), t(0)], new Set(['0_0', '0_1']));
  assert.deepEqual([d.stop, d.start], [['0_0'], ['0_2']]);
});

import { readyCoverage } from '../demo/tiling.js';

test('ready coverage is the share of the view covered by decoded tiles', () => {
  const all = new Set(grid.all().map(tileKey));
  assert.equal(readyCoverage(grid, level[0], fov, fov, all), 1);
  assert.equal(readyCoverage(grid, level[0], fov, fov, new Set()), 0);
  const needed = planVisible(grid, level, fov, fov);
  const half = new Set(needed.slice(0, 6).map(tileKey));
  const c = readyCoverage(grid, level[0], fov, fov, half);
  assert.ok(c > 0.8 && c < 0.95, `got ${c}`);      // 2 rows x 3 columns hold most of a 100-degree view; the outer rows are only slivers
  const one = readyCoverage(grid, level[0], fov, fov, new Set([tileKey(needed[0])]));
  assert.ok(one > 0.05 && one < 0.3, `one tile covered ${one}`);
});
