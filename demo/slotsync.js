// How a tile's <video> is brought into step with the base clock, and how the number of decoders adapts when
// the machine cannot keep up. Pure logic (no DOM) so it can be tested; viewer.js applies the returned actions.
//
// Why "seek ahead and hold": decoding a tile from its keyframe up to "now" takes real time. If we seek to the
// current clock and the decoder needs longer than our tolerance to catch up, the tile is already late when it
// arrives, gets seeked again, and never joins. Instead a joining tile seeks to where the clock WILL be (now +
// `lead`), decodes up to that frame while paused, waits there, and starts playing when the clock arrives.
// `lead` is learned from how long joins actually take.

export const READY_TOLERANCE = 0.25;     // s: a tile within this of the clock is drawn
export const PLAY_WINDOW = 0.12;         // s: start playing this close before the clock reaches the held frame
export const STALL_MS = 4000;            // the decoder has produced no frame this long after (re)starting: give up on the tile
export const MIN_LEAD = 0.25, MAX_LEAD = 3;

export function wrapDiff(a, b, dur) {   // a - b on a looping timeline
  let d = a - b;
  if (dur > 0) { if (d > dur / 2) d -= dur; else if (d < -dur / 2) d += dur; }
  return d;
}
const mod = (x, dur) => (dur > 0 ? x - Math.floor(x / dur) * dur : x);

export function nextLead(lead, joinSeconds) {
  const wanted = joinSeconds * 1.2 + 0.05;
  const next = wanted > lead ? wanted : lead * 0.6 + wanted * 0.4;      // rise at once, fall within a few joins (a late join only costs a re-seek)
  return Math.max(MIN_LEAD, Math.min(MAX_LEAD, next));
}

export function newSync(nowMs, clock, dur, lead) {
  return { phase: 'loading', target: mod(clock + lead, dur), assignedAt: nowMs, phaseStart: nowMs, holdStart: nowMs, rejoins: 0, lastRejoin: -1e9, lagSince: null, ready: false };
}

/**
 * One decision for one tile.
 * st: state from newSync (mutated). i: { nowMs, clock, dur, time, readyState, seeking, paused, playing, lead }
 * Returns { play, pause, seekTo, rate, ready, stalled, joinedSeconds } (all optional).
 */
export function step(st, i) {
  const out = {};
  // A stall is "the decoder is not producing frames", not "we are waiting for the clock": holding on a decoded frame
  // for up to `lead` seconds is deliberate. (Time spent holding is bounded by the lead plus a little slack.)
  if (!st.ready) {
    const stuck = st.phase === 'loading' ? i.nowMs - st.phaseStart > STALL_MS : st.phase === 'holding' && i.nowMs - st.holdStart > i.lead * 1000 + 2000;
    if (stuck) return { stalled: true, ready: false };
  }

  const rejoin = () => {                          // restart the join further ahead, without tearing the decoder down
    st.rejoins++;
    if (st.rejoins > 3) { out.stalled = true; return; }
    st.phase = 'loading'; st.phaseStart = i.nowMs; st.lastRejoin = i.nowMs; st.lagSince = null; st.ready = false;
    st.target = mod(i.clock + i.lead, i.dur);
    out.pause = true; out.seekTo = st.target; out.ready = false; out.lateJoin = true;
  };

  if (st.phase === 'loading') {
    if (i.readyState >= 2 && !i.seeking) {
      st.phase = 'holding'; st.holdStart = i.nowMs;
      out.joinedSeconds = (i.nowMs - st.phaseStart) / 1000;
    } else { out.ready = false; return out; }
  }

  if (st.phase === 'holding') {
    const wait = wrapDiff(st.target, i.clock, i.dur);                 // seconds until the clock reaches the held frame
    if (wait < -READY_TOLERANCE) { rejoin(); return out; }            // arrived late: try again with a longer lead
    if (!i.playing) {                                                 // user paused: show the held frame once it matches
      out.ready = Math.abs(wait) <= READY_TOLERANCE; st.ready = out.ready; return out;
    }
    out.ready = wait <= READY_TOLERANCE; st.ready = out.ready;       // a frame a little early is better than the blurry base
    if (wait <= PLAY_WINDOW) { st.phase = 'playing'; st.lagSince = null; if (i.paused) out.play = true; }
    return out;
  }

  // playing
  const diff = wrapDiff(i.time, i.clock, i.dur);
  if (!i.playing) {                                                    // paused by the user: just track the clock
    if (!i.paused) out.pause = true;
    out.ready = Math.abs(diff) <= READY_TOLERANCE && i.readyState >= 2 && !i.seeking; st.ready = out.ready;
    if (Math.abs(diff) > 0.05 && !i.seeking) out.seekTo = mod(i.clock, i.dur);
    return out;
  }
  if (i.paused) out.play = true;
  if (diff > READY_TOLERANCE) {                                        // ahead: wait where we are until the clock arrives
    st.phase = 'holding'; st.holdStart = i.nowMs; st.target = i.time; out.pause = true; out.ready = false; st.ready = false; return out;
  }
  if (diff >= -READY_TOLERANCE) {
    st.lagSince = null; out.ready = true; st.ready = true;
    out.rate = Math.max(0.9, Math.min(1.1, 1 - diff * 0.5));
    return out;
  }
  // behind by more than the tolerance
  out.ready = diff > -0.4; st.ready = out.ready;                       // slightly stale is still better than the base layer
  out.rate = 1.15;                                                     // catch up gently
  if (st.lagSince == null) st.lagSince = i.nowMs;
  if (i.nowMs - st.lagSince > 1000 && i.nowMs - st.lastRejoin > 3000) rejoin();   // persistent lag: re-join ahead, at most every 3 s
  return out;
}

/**
 * Number of decoders we dare to run. Starts at the user's setting; when tiles stall (the machine can't decode
 * that many at once) it backs off, then probes upwards again slowly, waiting longer after each failed probe.
 */
export class AdaptiveBudget {
  constructor(max, { min = 3, recoverMs = 12000 } = {}) {
    Object.assign(this, { max, min, eff: max, baseRecover: recoverMs, recoverMs, lastStall: -1e9, lastGrow: -1e9, backoffs: 0, stalls: 0 });
  }
  setMax(max) { this.max = max; this.eff = max; this.recoverMs = this.baseRecover; }     // the user asked for this many
  stalled(nowMs, count = 1) {
    this.stalls += count;
    if (nowMs - this.lastStall < 1500) return;                                        // one back-off per burst of stalls
    this.eff = Math.max(Math.min(this.min, this.max), this.eff - Math.max(1, Math.ceil(count)));
    if (nowMs - this.lastGrow < this.recoverMs) this.recoverMs = Math.min(120000, this.recoverMs * 1.7);   // the last probe failed
    this.lastStall = nowMs; this.backoffs++;
  }
  tick(nowMs, healthy) {
    if (this.eff < this.max && healthy && nowMs - Math.max(this.lastStall, this.lastGrow) > this.recoverMs) { this.eff++; this.lastGrow = nowMs; }
    return this.eff;
  }
  // How many tiles may be starting at once. Generous while things are calm (a view change should fill quickly);
  // tight for a while after trouble, because starting many decoders at once is what overloads a struggling machine.
  maxJoining(nowMs) { return nowMs - this.lastStall > 15000 ? 8 : Math.max(2, Math.min(4, Math.ceil(this.eff / 4))); }
}
