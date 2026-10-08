import test from 'node:test';
import assert from 'node:assert/strict';
import { step, newSync, nextLead, wrapDiff, AdaptiveBudget, STALL_MS, MAX_LEAD, MIN_LEAD } from '../demo/slotsync.js';

const base = { dur: 30, playing: true, paused: true, readyState: 4, seeking: false, lead: 0.6 };
const at = (nowMs, clock, extra = {}) => ({ ...base, nowMs, clock, time: clock, ...extra });

test('wrapDiff handles the loop point', () => {
  assert.ok(Math.abs(wrapDiff(0.1, 29.9, 30) - 0.2) < 1e-9);
  assert.ok(Math.abs(wrapDiff(29.9, 0.1, 30) + 0.2) < 1e-9);
});

test('a joining tile waits for the decoder, holds on the target frame, then plays when the clock arrives', () => {
  const st = newSync(0, 10, 30, 0.6);                                   // asks for t = 10.6
  assert.equal(st.target, 10.6);
  assert.deepEqual(step(st, at(100, 10.1, { readyState: 0 })), { ready: false });         // still loading
  let r = step(st, at(300, 10.3));                                      // decoded and paused at 10.6, clock 10.3
  assert.equal(st.phase, 'holding'); assert.equal(r.ready, false); assert.ok(!r.play);   // 0.3 s early: not drawn yet
  assert.ok(Math.abs(r.joinedSeconds - 0.3) < 1e-9);
  r = step(st, at(450, 10.45));                                         // within the 0.25 s tolerance: drawn, still held
  assert.equal(r.ready, true); assert.ok(!r.play);
  r = step(st, at(500, 10.5));                                          // within the play window: start playing
  assert.equal(r.play, true); assert.equal(st.phase, 'playing');
});

test('a tile that arrives after its target re-joins further ahead instead of chasing the clock', () => {
  const st = newSync(0, 10, 30, 0.6);
  step(st, at(2000, 10.2));                                             // loading finished at 2 s -> holding at 10.6
  const r = step(st, at(2400, 12.4, { lead: 2.8 }));                    // clock is now 1.8 s past the held frame
  assert.equal(r.lateJoin, true); assert.equal(r.pause, true); assert.equal(st.phase, 'loading');
  assert.ok(Math.abs(r.seekTo - 15.2) < 1e-9);                          // 12.4 + 2.8
  assert.equal(st.rejoins, 1);
});

test('lead is learned from real join times: rises fast, falls slowly, stays within bounds', () => {
  assert.ok(nextLead(0.5, 2) > 2.4);                                   // one slow join raises the lead immediately
  assert.ok(nextLead(3, 0.1) > 1.5 && nextLead(3, 0.1) < 3);            // and it comes down over a few fast joins, not at once
  assert.equal(nextLead(0.25, 0.05), MIN_LEAD);
  assert.equal(nextLead(1, 60), MAX_LEAD);
});

test('a playing tile in step is drawn and nudged; far ahead it holds; far behind it eventually re-joins', () => {
  const st = newSync(0, 10, 30, 0.3); st.phase = 'playing'; st.ready = true;
  let r = step(st, { ...at(1000, 10, { paused: false }), time: 10.05 });
  assert.equal(r.ready, true); assert.ok(r.rate < 1);
  r = step(st, { ...at(1100, 10, { paused: false }), time: 10.6 });      // 0.6 s ahead
  assert.equal(r.pause, true); assert.equal(st.phase, 'holding');
  Object.assign(st, { phase: 'playing', lagSince: null, lastRejoin: -1e9 });
  r = step(st, { ...at(2000, 12, { paused: false }), time: 11.5 });      // 0.5 s behind: draws nothing stale, speeds up
  assert.equal(r.ready, false); assert.ok(r.rate > 1);
  r = step(st, { ...at(3200, 13.2, { paused: false }), time: 12.7 });    // still behind 1.2 s later: re-join
  assert.equal(r.lateJoin, true);
});

test('stalled means the decoder produces nothing; waiting on the clock is not a stall', () => {
  const st = newSync(0, 10, 30, 3);                                     // a 3 s lead: holding for ~3 s is by design
  step(st, at(500, 10, { lead: 3 }));                                   // frame decoded at 0.5 s -> holding at 13
  assert.equal(st.phase, 'holding');
  assert.notEqual(step(st, at(4500, 10.5, { lead: 3 })).stalled, true); // 4 s after assignment, still waiting: fine
  assert.equal(step(st, at(500 + 3000 + 2001, 10.5, { lead: 3 })).stalled, true);   // held far longer than the lead allows
});

test('repeated re-joins and plain timeouts give up (stalled) instead of looping forever', () => {
  const st = newSync(0, 10, 30, 0.6);
  assert.deepEqual(step(st, at(STALL_MS + 1, 10, { readyState: 0 })), { stalled: true, ready: false });
  const st2 = newSync(0, 10, 30, 0.6); st2.ready = true; st2.phase = 'holding'; st2.rejoins = 3; st2.target = 5;
  assert.equal(step(st2, at(100, 10)).stalled, true);
});

test('pausing playback tracks the clock and still shows the tile', () => {
  const st = newSync(0, 10, 30, 0.3); st.phase = 'playing';
  const r = step(st, { ...at(500, 10, { playing: false, paused: false }), time: 10.02 });
  assert.equal(r.pause, true); assert.equal(r.ready, true);
});

test('adaptive budget backs off on stalls, probes upward slowly, and waits longer after a failed probe', () => {
  const b = new AdaptiveBudget(16, { min: 3, recoverMs: 10000 });
  assert.equal(b.eff, 16);
  b.stalled(1000, 4); assert.equal(b.eff, 12);
  b.stalled(1500, 4); assert.equal(b.eff, 12);                          // same burst: one back-off only
  b.stalled(5000, 2); assert.equal(b.eff, 10);
  assert.equal(b.tick(8000, true), 10);                                 // too soon to probe
  assert.equal(b.tick(16000, false), 10);                               // not healthy
  assert.equal(b.tick(16000, true), 11); const grew = 16000;
  b.stalled(grew + 3000, 1); assert.equal(b.eff, 10);                   // the probe failed quickly
  assert.ok(b.recoverMs > 10000);                                       // so the next probe waits longer
  b.stalled(60000, 99); assert.equal(b.eff, 3);                         // never below the floor
  b.setMax(8); assert.equal(b.eff, 8);                                  // user changes the budget: respected
  assert.ok(b.maxJoining(60000 + 1000) >= 2 && b.maxJoining(60000 + 1000) <= 4);   // just after trouble: tight
  assert.equal(b.maxJoining(60000 + 20000), 8);                                      // calm for 15 s: generous
});
