import { Grid, planVisible, Scheduler, rotation, apply, tileKey, rad, readyCoverage } from './tiling.js';
import { FrameStats, TimeMean, benchOrder, decodedMpxPerSecond } from './perf.js';
import { step, newSync, nextLead, wrapDiff, AdaptiveBudget } from './slotsync.js';

const params = new URLSearchParams(location.search);
const base = (params.get('tileset') || 'tileset').replace(/\/$/, '') + '/';
const $ = (id) => document.getElementById(id);
const canvas = $('view');
const gl = canvas.getContext('webgl', { antialias: false, alpha: false });
const msg = (t) => { $('msg').style.display = t ? 'block' : 'none'; $('msg').textContent = t || ''; };

let manifest;
try {
  manifest = await (await fetch(base + 'manifest.json')).json();
} catch (e) {
  msg(`Could not load ${base}manifest.json. Run: python3 demo/run_demo.py`);
  throw e;
}
if (!gl) { msg('WebGL is not available in this browser.'); throw new Error('no webgl'); }

const MIME = { h264: 'video/mp4; codecs="avc1.640028"', hevc: 'video/mp4; codecs="hvc1.1.6.L120.90"', vp9: 'video/mp4; codecs="vp09.00.40.08"', av1: 'video/mp4; codecs="av01.0.08M.08"' };
if (!document.createElement('video').canPlayType(MIME[manifest.codec] || '')) {
  msg(`This browser cannot decode ${manifest.codec.toUpperCase()} video. Use Chrome, Edge or Safari, or run: python3 demo/run_demo.py --codec h264`);
  throw new Error('codec unsupported');
}
const sourceUrl = params.get('source') || 'source.mp4';
const probeSource = async () => !!(await fetch(sourceUrl, { method: 'HEAD' }).catch(() => null))?.ok;
let sourceOk = await probeSource();

const g = manifest.grid;
const grid = new Grid(g.cols, g.rows, g.tileWidth, g.tileHeight, g.pad);
const fileOf = Object.fromEntries(manifest.tiles.map((t) => [tileKey(t), t.file]));
const inner = grid.innerRect();
const fps = manifest.fps;

// ---------------------------------------------------------------- video elements
function makeVideo() {
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.loop = true; v.preload = 'auto';
  v.crossOrigin = 'anonymous';
  return v;
}
const baseVideo = makeVideo();
baseVideo.src = base + manifest.base.file;
// "Normal playback": the original video, one decoder, drawn on the same sphere. Used for comparisons.
const sourceVideo = makeVideo();
// Only a failure while a source is actually loaded counts; some browsers also raise 'error' when we unload one.
sourceVideo.addEventListener('error', () => { if (sourceVideo.getAttribute('src')) { sourceOk = false; $('modeFull').disabled = true; } });
let mode = 'tiled';
let muted = true;
const clockVideo = () => (mode === 'full' ? sourceVideo : baseVideo);

// Decoder frame counters are per <video> load, so keep totals from videos we have unloaded.
const done = { dropped: 0, total: 0 };
function harvest(v) { const q = v.getVideoPlaybackQuality?.(); if (q) { done.dropped += q.droppedVideoFrames; done.total += q.totalVideoFrames; } }
function videoCounters() {
  let dropped = done.dropped, total = done.total;
  for (const v of [baseVideo, sourceVideo, ...slots.map((x) => x.video)]) {
    if (!v.getAttribute('src')) continue;
    const q = v.getVideoPlaybackQuality?.(); if (q) { dropped += q.droppedVideoFrames; total += q.totalVideoFrames; }
  }
  return { dropped, total };
}
function unload(v) { harvest(v); v.pause(); v.removeAttribute('src'); v.load(); }

// A slot is one decoder instance: a <video> element that is pointed at different tiles. Releasing
// a slot (removing its src) frees the browser's hardware decoder, which is the point of tiling.
let slots = [];
const budgetCtl = new AdaptiveBudget(12);        // decoders we dare to run; backs off when tiles stall
const cooldown = new Map();                     // tile key -> time before which it will not be started again
let joinLead = 0.5;                             // seconds ahead of the clock a joining tile seeks to (learned)
function makeSlot() {
  const s = { video: makeVideo(), tile: null, key: null, assignedAt: 0, readyAt: 0, ready: false, dirty: false, frames: 0, texture: createTexture(), dropped: 0, sync: null, releasing: false, lastJoin: 0 };
  if ('requestVideoFrameCallback' in s.video) {
    const onFrame = () => { s.dirty = true; s.frames++; s.video.requestVideoFrameCallback(onFrame); };
    s.video.requestVideoFrameCallback(onFrame);
  }
  s.video.addEventListener('seeked', () => { s.dirty = true; });     // a paused video shows a new frame after a seek
  return s;
}
function setBudget(n) {
  while (slots.length < n) slots.push(makeSlot());
  while (slots.length > n) { const s = slots.pop(); release(s); gl.deleteTexture(s.texture); }
  scheduler = new Scheduler(n, 1500);
  budgetCtl.setMax(n);
  $('vBudget').textContent = n;
}
function assign(s, key, now) {
  const [row, col] = key.split('_').map(Number);
  const dur = baseVideo.duration || manifest.durationSeconds;
  s.tile = { row, col }; s.key = key; s.ready = false; s.frames = 0; s.dirty = true; s.assignedAt = now;
  s.sync = newSync(now, baseVideo.currentTime, dur, joinLead);
  s.video.playbackRate = 1;
  s.video.src = base + fileOf[key] + '#t=' + s.sync.target.toFixed(3);   // start straight at the target; no separate seek after loading
  stats.starts++;
}
function release(s) {
  unload(s.video);
  s.tile = null; s.key = null; s.ready = false; s.sync = null;
  // Tearing a decoder down takes a moment; don't start another in this slot until it has finished (or 100 ms),
  // so a burst of tile changes cannot pile up decoders that are still shutting down.
  s.releasing = true;
  const done = () => { s.releasing = false; };
  s.video.addEventListener('emptied', done, { once: true });
  setTimeout(done, 100);
}

// ---------------------------------------------------------------- WebGL
function createTexture() {
  const t = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([20, 20, 20, 255]));
  for (const [p, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, p, v);
  return t;
}
function program(vs, fs) {
  const p = gl.createProgram();
  for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
    const sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
    gl.attachShader(p, sh);
  }
  gl.linkProgram(p);
  return p;
}
const prog = program(
  `attribute vec3 aPos; attribute vec2 aUv; uniform mat4 uMvp; varying vec2 vUv;
   void main(){ vUv = aUv; gl_Position = uMvp * vec4(aPos, 1.0); }`,
  `precision mediump float; uniform sampler2D uTex; uniform vec4 uTint; varying vec2 vUv;
   void main(){ vec4 c = texture2D(uTex, vUv); gl_FragColor = vec4(mix(c.rgb, uTint.rgb, uTint.a), 1.0); }`);
const loc = { pos: gl.getAttribLocation(prog, 'aPos'), uv: gl.getAttribLocation(prog, 'aUv'), mvp: gl.getUniformLocation(prog, 'uMvp'), tint: gl.getUniformLocation(prog, 'uTint') };
const R = 10;

/** Sphere patch over [a0,a1] x [eBottom,eTop] with uv rectangle uvRect = [u0,v0,u1,v1] (v from the top). */
function patch(a0, a1, eBottom, eTop, uvRect, nx, ny) {
  const verts = [], idx = [];
  for (let i = 0; i <= ny; i++) {
    const f = i / ny, e = eTop + (eBottom - eTop) * f;
    for (let j = 0; j <= nx; j++) {
      const h = j / nx, a = a0 + (a1 - a0) * h;
      verts.push(R * Math.cos(e) * Math.sin(a), R * Math.sin(e), -R * Math.cos(e) * Math.cos(a),
        uvRect[0] + (uvRect[2] - uvRect[0]) * h, uvRect[1] + (uvRect[3] - uvRect[1]) * f);
    }
  }
  for (let i = 0; i < ny; i++) for (let j = 0; j < nx; j++) {
    const k = i * (nx + 1) + j;
    idx.push(k, k + 1, k + nx + 1, k + 1, k + nx + 2, k + nx + 1);
  }
  const vb = gl.createBuffer(), ib = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(verts), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(idx), gl.STATIC_DRAW);
  return { vb, ib, count: idx.length };
}
const sphere = patch(-Math.PI, Math.PI, -Math.PI / 2, Math.PI / 2, [0, 0, 1, 1], 96, 48);
const patches = new Map(grid.all().map((t) => {
  const [a0, a1] = grid.azimuthRange(t), [e0, e1] = grid.elevationRange(t);
  return [tileKey(t), patch(a0, a1, e0, e1, inner, 12, 12)];
}));
const baseTexture = createTexture();
const sourceTexture = createTexture();

function draw(mesh, texture, tint) {
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.bindBuffer(gl.ARRAY_BUFFER, mesh.vb); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.ib);
  gl.enableVertexAttribArray(loc.pos); gl.vertexAttribPointer(loc.pos, 3, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(loc.uv); gl.vertexAttribPointer(loc.uv, 2, gl.FLOAT, false, 20, 12);
  gl.uniform4fv(loc.tint, tint);
  gl.drawElements(gl.TRIANGLES, mesh.count, gl.UNSIGNED_SHORT, 0);
}
function mvp(yaw, pitch, fovV, aspect) {
  const r = rotation(yaw, pitch);              // view -> world; the shader needs world -> view (transpose)
  const f = 1 / Math.tan(fovV / 2), n = 0.1, fa = 100;
  const rt = [r[0], r[3], r[6], r[1], r[4], r[7], r[2], r[5], r[8]];
  // column-major: P * [rt | 0]
  const m = new Float32Array(16);
  for (let c = 0; c < 3; c++) {
    m[c * 4 + 0] = (f / aspect) * rt[0 * 3 + c]; m[c * 4 + 1] = f * rt[1 * 3 + c];
    m[c * 4 + 2] = ((fa + n) / (n - fa)) * rt[2 * 3 + c]; m[c * 4 + 3] = -rt[2 * 3 + c];
  }
  m[14] = (2 * fa * n) / (n - fa); m[15] = 0;
  return m;
}

// ---------------------------------------------------------------- state & input
let yaw = 0, pitch = 0, fovV = rad(90), playing = true, tourOn = false, scheduler;
let vel = { yaw: 0, pitch: 0 }; // rad/s, smoothed
const stats = { starts: 0, joinTotal: 0, joins: 0, tilesWanted: 0, tilesDrawn: 0, maxSync: 0, stalls: 0, rejoins: 0 };
let wantedTiles = [];
const clampPitch = (p) => Math.max(rad(-89), Math.min(rad(89), p));

let drag = null;
canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); tourOn = false; });
canvas.addEventListener('pointermove', (e) => {
  if (!drag) return;
  const k = fovV / canvas.clientHeight;          // radians per pixel
  yaw += (e.clientX - drag.x) * k; pitch = clampPitch(pitch + (e.clientY - drag.y) * k);
  drag = { x: e.clientX, y: e.clientY };
});
canvas.addEventListener('pointerup', () => { drag = null; });
canvas.addEventListener('wheel', (e) => { e.preventDefault(); setFov($('fov').valueAsNumber + Math.sign(e.deltaY) * 4); }, { passive: false });
addEventListener('keydown', (e) => {
  const s = rad(8); tourOn = false;
  if (e.key === 'ArrowLeft') yaw += s; else if (e.key === 'ArrowRight') yaw -= s;
  else if (e.key === 'ArrowUp') pitch = clampPitch(pitch + s); else if (e.key === 'ArrowDown') pitch = clampPitch(pitch - s);
});
function setFov(d) { d = Math.max(40, Math.min(120, d)); $('fov').value = d; fovV = rad(d); $('vFov').textContent = d; }
$('fov').oninput = (e) => setFov(e.target.valueAsNumber);
$('budget').oninput = (e) => setBudget(e.target.valueAsNumber);
$('margin').oninput = (e) => { $('vMargin').textContent = e.target.value; };
$('play').onclick = () => { playing = !playing; $('play').textContent = playing ? 'Pause' : 'Play'; for (const v of [baseVideo, sourceVideo, ...slots.map((s) => s.video)]) { if (!v.getAttribute('src')) continue; playing ? v.play().catch(() => {}) : v.pause(); } };
$('mute').onclick = () => { muted = !muted; baseVideo.muted = sourceVideo.muted = muted; $('mute').textContent = muted ? 'Unmute' : 'Mute'; };
$('tour').onclick = () => { tourOn = !tourOn; tourT = 0; };
let tourT = 0;
// The same head path every time, so two runs (or two modes) see identical motion.
const tourPose = (t) => ({ yaw: rad(110) * Math.sin(t * 0.55) + t * 0.35, pitch: rad(40) * Math.sin(t * 0.8) });
setFov(90); $('vMargin').textContent = $('margin').value; setBudget(12);
baseVideo.play().catch(() => msg('Click anywhere to start playback.'));
addEventListener('pointerdown', () => { msg(''); if (playing) clockVideo().play().catch(() => {}); }, { once: true });

// ---------------------------------------------------------------- per-frame work
let lastPlan = 0, last = performance.now(), lastYaw = yaw, lastPitch = pitch, fpsCount = 0, fpsT = 0, shownFps = 0;

let sharpNow = true, areaNow = 1, needNow = [], tourSpeed = 1;
function updateSlots(now) {
  if (mode === 'full') { wantedTiles = []; sharpNow = true; areaNow = 1; return; }
  const tilingOn = $('tiling').checked;
  // Plan a few times per second, not every frame.
  if (now - lastPlan > 100) {
    lastPlan = now;
    const aspect = canvas.clientWidth / canvas.clientHeight;
    const halfV = fovV / 2, halfH = Math.atan(Math.tan(halfV) * aspect);
    const views = [{ yaw, pitch }];
    if ($('predict').checked) views.push({ yaw: yaw + vel.yaw * 0.4, pitch: clampPitch(pitch + vel.pitch * 0.4) });
    wantedTiles = tilingOn ? planVisible(grid, views, halfH, halfV, rad($('margin').valueAsNumber)) : [];
    needNow = planVisible(grid, [{ yaw, pitch }], halfH, halfV, 0).map(tileKey);   // tiles actually on screen
    areaNow = tilingOn ? readyCoverage(grid, { yaw, pitch }, halfH, halfV, new Set(slots.filter((s) => s.key && s.ready).map((s) => s.key))) : 0;
    const active = new Set(slots.filter((s) => s.key).map((s) => s.key));
    const healthy = !slots.some((s) => s.key && !s.ready && now - s.assignedAt > 2000);
    scheduler.budget = budgetCtl.tick(now, healthy);
    const eligible = wantedTiles.filter((t) => (cooldown.get(tileKey(t)) ?? 0) <= now);   // not recently given up on
    const d = scheduler.update(now, eligible, active);
    for (const k of d.stop) release(slots.find((s) => s.key === k));
    // Start a few tiles at a time, most important first; the rest are picked up on the next tick. Starting
    // every decoder at once is what overloads a machine that is already struggling.
    let joining = slots.filter((s) => s.key && !s.ready).length;
    for (const k of d.start) {
      if (joining >= budgetCtl.maxJoining(now)) break;
      const free = slots.find((s) => !s.key && !s.releasing);
      if (!free) break;
      assign(free, k, now); joining++;
    }
  }
  { const ready = new Set(slots.filter((s) => s.key && s.ready).map((s) => s.key)); sharpNow = tilingOn && needNow.every((k) => ready.has(k)); }
  const clock = baseVideo.currentTime, dur = baseVideo.duration || manifest.durationSeconds;
  let maxSync = 0, stalledNow = 0, lateNow = 0;
  for (const s of slots) {
    if (!s.key || !s.sync) continue;
    const v = s.video;
    const was = s.ready;
    const r = step(s.sync, { nowMs: now, clock, dur, time: v.currentTime, readyState: v.readyState, seeking: v.seeking, paused: v.paused, playing, lead: joinLead });
    if (r.stalled) {                                   // could not get this tile in step; free the decoder and leave the tile alone for a while
      cooldown.set(s.key, now + 4000); stats.stalls++; stalledNow++; release(s); continue;
    }
    if (r.joinedSeconds != null) { joinLead = nextLead(joinLead, r.joinedSeconds); s.lastJoin = r.joinedSeconds; }
    if (r.lateJoin) { stats.rejoins++; if (s.lastJoin > 1.5) lateNow++; }        // the lead was already raised from the measured join time; but late joins also mean the decoders are behind
    if (r.pause && !v.paused) v.pause();
    if (r.seekTo != null) { v.currentTime = r.seekTo; s.dirty = true; }
    if (r.play && v.paused) v.play().catch(() => {});
    if (r.rate != null && Math.abs(v.playbackRate - r.rate) > 0.01) v.playbackRate = r.rate;
    if (r.ready != null) s.ready = r.ready;
    if (s.ready && !was) { s.dirty = true; s.readyAt = now; stats.joinTotal += now - s.assignedAt; stats.joins++; }
    if (s.ready) maxSync = Math.max(maxSync, Math.abs(wrapDiff(v.currentTime, clock, dur)));
    const q = v.getVideoPlaybackQuality?.(); if (q) s.dropped = q.droppedVideoFrames;
  }
  if (stalledNow || lateNow) budgetCtl.stalled(now, stalledNow + lateNow);
  stats.maxSync = maxSync;
  if (win.active) { win.maxSync = Math.max(win.maxSync, maxSync); win.effMin = Math.min(win.effMin, budgetCtl.eff); }
}

function upload(texture, video, dirty) {
  if (!dirty || video.readyState < 2) return;
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
}

function frame(now) {
  const rawMs = now - last, dt = Math.min(0.1, rawMs / 1000); last = now;
  if (tourOn) {                                    // repeatable head sweep: look around, up and down, then behind
    tourT += dt * tourSpeed;
    ({ yaw, pitch } = tourPose(tourT));
  }
  if (dt > 0) {                                     // smoothed head velocity for prefetch
    const k = 0.2;
    vel.yaw += ((yaw - lastYaw) / dt - vel.yaw) * k; vel.pitch += ((pitch - lastPitch) / dt - vel.pitch) * k;
    lastYaw = yaw; lastPitch = pitch;
  }
  updateSlots(now);

  const dpr = Math.min(2, devicePixelRatio || 1);
  const w = Math.floor(canvas.clientWidth * dpr), h = Math.floor(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  gl.viewport(0, 0, w, h); gl.disable(gl.DEPTH_TEST); gl.disable(gl.CULL_FACE);
  gl.useProgram(prog);
  gl.uniformMatrix4fv(loc.mvp, false, mvp(yaw, pitch, fovV, w / h));

  let drawn = 0;
  if (mode === 'full') { upload(sourceTexture, sourceVideo, true); draw(sphere, sourceTexture, [0, 0, 0, 0]); }
  else { upload(baseTexture, baseVideo, true); draw(sphere, baseTexture, [0, 0, 0, 0]); }
  const tint = $('tint').checked ? [0.1, 0.9, 0.5, 0.22] : [0, 0, 0, 0];
  for (const s of slots) {
    if (!s.key || !s.ready) continue;
    upload(s.texture, s.video, s.dirty || !('requestVideoFrameCallback' in s.video)); s.dirty = false;
    draw(patches.get(s.key), s.texture, tint); drawn++;
  }
  stats.tilesDrawn = drawn;
  if (win.active) {
    win.frames.add(rawMs);
    win.slots.add(mode === 'full' ? 0 : slots.filter((s) => s.key).length, dt);
    win.sharp.add(sharpNow ? 1 : 0, dt);
    win.area.add(areaNow, dt);
  }
  fpsCount++; fpsT += dt; if (fpsT > 1) { shownFps = fpsCount / fpsT; fpsCount = 0; fpsT = 0; }
  updatePanel(now);
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- panel
const mapCtx = $('map').getContext('2d');
function updatePanel(now) {
  const active = slots.filter((s) => s.key);
  const codedPx = grid.codedWidth * grid.codedHeight;
  const basePx = manifest.base.width * manifest.base.height;
  const fullPx = grid.width * grid.height * fps, decodedPx = mode === 'full' ? fullPx : (active.length * codedPx + basePx) * fps;
  const pct = (100 * decodedPx) / fullPx;
  $('sSlots').textContent = `${active.length} / ${slots.length}` + (budgetCtl.eff < slots.length ? ` (limited to ${budgetCtl.eff})` : '');
  $('sTiles').textContent = `${Math.min(wantedTiles.length, 999)} / ${stats.tilesDrawn}`;
  $('sPct').textContent = `${pct.toFixed(0)}%`;
  $('pctBar').style.width = `${Math.min(100, pct)}%`;
  $('sRate').textContent = `${(decodedPx / 1e6).toFixed(0)} of ${(fullPx / 1e6).toFixed(0)} Mpx/s`;
  $('sJoin').textContent = `${stats.starts} · ${stats.joins ? Math.round(stats.joinTotal / stats.joins) + ' ms' : '–'}`;
  $('sDrop').textContent = slots.reduce((a, s) => a + s.dropped, 0);
  $('sSync').textContent = stats.tilesDrawn ? `${Math.round(stats.maxSync * 1000)} ms` : '–';

  const W = 288, H = 144, cw = W / grid.cols, ch = H / grid.rows;
  mapCtx.clearRect(0, 0, W, H); mapCtx.fillStyle = '#0b0e13'; mapCtx.fillRect(0, 0, W, H);
  const bySlot = new Map(slots.filter((s) => s.key).map((s) => [s.key, s]));
  const wantedSet = new Set(wantedTiles.map(tileKey));
  for (const t of grid.all()) {
    const k = tileKey(t), s = bySlot.get(k), x = t.col * cw, y = t.row * ch;
    if (s) { mapCtx.fillStyle = s.ready ? '#3ecf8e' : (wantedSet.has(k) ? '#f5a524' : '#5b6472'); mapCtx.fillRect(x + 1, y + 1, cw - 2, ch - 2); if (!wantedSet.has(k) && s.ready) { mapCtx.fillStyle = 'rgba(0,0,0,.35)'; mapCtx.fillRect(x + 1, y + 1, cw - 2, ch - 2); } }
    else if (wantedSet.has(k)) { mapCtx.strokeStyle = '#ef5350'; mapCtx.lineWidth = 1.5; mapCtx.strokeRect(x + 1.5, y + 1.5, cw - 3, ch - 3); }
    else { mapCtx.strokeStyle = '#1e242d'; mapCtx.lineWidth = 1; mapCtx.strokeRect(x + .5, y + .5, cw, ch); }
  }
  // View centre: azimuth 0 is the middle of the picture; yaw is positive to the left.
  const cx = (0.5 - yaw / (2 * Math.PI)) % 1, cxw = ((cx % 1) + 1) % 1;
  mapCtx.fillStyle = '#fff'; mapCtx.beginPath(); mapCtx.arc(cxw * W, (0.5 - pitch / Math.PI) * H, 4, 0, 7); mapCtx.fill();
  window.__stats = { slots: active.length, budget: slots.length, wanted: wantedTiles.length, drawn: stats.tilesDrawn, pct, starts: stats.starts, joinMs: stats.joins ? stats.joinTotal / stats.joins : null, maxSyncMs: stats.maxSync * 1000, fps: shownFps, yaw, pitch, eff: budgetCtl.eff, stalls: stats.stalls, rejoins: stats.rejoins, backoffs: budgetCtl.backoffs, lead: joinLead };
}
requestAnimationFrame(frame);
window.__demo = { setTour: (on) => { tourOn = on; tourT = 0; }, setView: (y, p) => { tourOn = false; yaw = y; pitch = p; } };

// ---------------------------------------------------------------- mode switching and benchmark
const win = { active: false, maxSync: 0 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function setMode(m) {
  if (m === mode) return true;
  if (m === 'full' && !sourceOk) await recheckSource();      // a stale failure flag must not block a source that is fine
  if (m === 'full' && !sourceOk) { msg(`Normal playback needs the original video at ${sourceUrl} (run_demo.py links it automatically).`); return false; }
  const t = clockVideo().currentTime;
  mode = m;
  $('modeFull').checked = m === 'full'; $('modeTiled').checked = m === 'tiled';
  for (const s of slots) if (s.key) release(s);
  const [target, other] = m === 'full' ? [sourceVideo, baseVideo] : [baseVideo, sourceVideo];
  unload(other);
  target.muted = muted;
  target.src = m === 'full' ? sourceUrl : base + manifest.base.file;
  await new Promise((r) => { target.addEventListener('loadedmetadata', r, { once: true }); target.addEventListener('error', r, { once: true }); });
  if (!sourceOk && m === 'full') { await setMode('tiled'); return false; }
  target.currentTime = t % (target.duration || t + 1);
  if (playing) await target.play().catch(() => {});
  await new Promise((r) => ('requestVideoFrameCallback' in target ? target.requestVideoFrameCallback(() => r()) : setTimeout(r, 400)));
  return true;
}

async function recheckSource() {
  sourceOk = await probeSource();
  $('modeFull').disabled = !sourceOk;
}

function perfStart(meta) {
  Object.assign(win, { active: true, meta, frames: new FrameStats(), slots: new TimeMean(), sharp: new TimeMean(), area: new TimeMean(), maxSync: 0,
    startEpoch: Date.now(), v0: videoCounters(), starts0: stats.starts, joinTotal0: stats.joinTotal, joins0: stats.joins,
    effMin: budgetCtl.eff, stalls0: stats.stalls, rejoins0: stats.rejoins, backoffs0: budgetCtl.backoffs });
}
function perfStop() {
  win.active = false;
  const v1 = videoCounters(), f = win.frames.summary(), seconds = (Date.now() - win.startEpoch) / 1000;
  const meanSlots = win.slots.mean() ?? 0, joins = stats.joins - win.joins0;
  return {
    ...win.meta, startEpochMs: win.startEpoch, endEpochMs: Date.now(), seconds,
    frames: f.frames, fps: f.fps, frameMs: { p50: f.p50, p95: f.p95, p99: f.p99, max: f.max }, hitches: f.hitches,
    videoFramesDropped: v1.dropped - win.v0.dropped, videoFramesTotal: v1.total - win.v0.total,
    meanSlots, decodedMpxPerS: decodedMpxPerSecond({ mode: win.meta.mode, slots: meanSlots, grid, base: manifest.base, fps, fullWidth: grid.width, fullHeight: grid.height }),
    sharpCoverage: win.meta.mode === 'full' ? 1 : win.sharp.mean(),            // time with every on-screen tile decoded
    sharpArea: win.meta.mode === 'full' ? 1 : win.area.mean(),                // average share of the view drawn from decoded tiles
    tileStarts: stats.starts - win.starts0, tileStalls: stats.stalls - win.stalls0, lateJoins: stats.rejoins - win.rejoins0,
    budgetBackoffs: budgetCtl.backoffs - win.backoffs0, effectiveBudgetMin: win.meta.mode === 'full' ? null : win.effMin, budgetSetting: slots.length, joinMsAvg: joins ? (stats.joinTotal - win.joinTotal0) / joins : null, maxSyncMs: win.maxSync * 1000,
  };
}

async function runBench() {
  const duration = Number(params.get('duration')) || 20, rounds = Number(params.get('rounds')) || 2, settle = 3;
  if (!sourceOk) await recheckSource();
  if (!sourceOk) { msg(`Benchmark needs the original video at ${sourceUrl}.`); return; }
  if (document.hidden) msg('Keep this tab in the foreground while the benchmark runs.');
  const windows = [];
  const plan = benchOrder(rounds);
  for (const [i, w] of plan.entries()) {
    $('benchStatus').textContent = `Benchmark ${i + 1}/${plan.length}: ${w.mode === 'full' ? 'normal playback' : 'tiled'} (round ${w.round}) — don't touch the mouse`;
    if (!(await setMode(w.mode))) return;
    tourSpeed = Number(params.get('speed')) || 0.5; tourOn = true; tourT = 0;
    await sleep(settle * 1000);                 // let decoders start and tiles join before measuring
    perfStart({ mode: w.mode, round: w.round });
    await sleep(duration * 1000);
    windows.push(perfStop());
  }
  tourOn = false; tourSpeed = 1;
  const result = {
    version: 1, finishedAt: new Date().toISOString(), userAgent: navigator.userAgent,
    display: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio, hz: windows[0] ? Math.round(windows[0].fps) : null },
    tileset: { codec: manifest.codec, cols: grid.cols, rows: grid.rows, width: grid.width, height: grid.height, fps, pad: grid.pad, base: manifest.base },
    settings: { budget: slots.length, marginDeg: Number($('margin').value), fovDeg: Number($('fov').value), durationS: duration, settleS: settle, rounds, tourSpeed: Number(params.get('speed')) || 0.5 },
    windows,
  };
  window.__benchResult = result;
  $('benchStatus').textContent = 'Benchmark finished.';
  try { await fetch('/__perf', { method: 'POST', body: JSON.stringify(result) }); $('benchStatus').textContent += ' Results sent to the local analyzer.'; }
  catch { $('benchStatus').textContent += ' (no analyzer running; use the download link)'; }
  showBench(result);
}

function showBench(r) {
  const by = (m) => r.windows.filter((w) => w.mode === m);
  const avg = (ws, k) => ws.length ? ws.reduce((a, w) => a + (typeof k === 'function' ? k(w) : w[k]), 0) / ws.length : null;
  const rows = [
    ['Frame time p95 (ms)', (ws) => avg(ws, (w) => w.frameMs.p95), 1],
    ['Frame time p99 (ms)', (ws) => avg(ws, (w) => w.frameMs.p99), 1],
    ['Hitches > 33 ms', (ws) => avg(ws, 'hitches'), 0],
    ['Video frames dropped', (ws) => avg(ws, 'videoFramesDropped'), 0],
    ['Decoded Mpx/s (modeled)', (ws) => avg(ws, 'decodedMpxPerS'), 0],
    ['View drawn sharp %', (ws) => 100 * avg(ws, 'sharpArea'), 1],
  ];
  const f = (v, d) => (v == null ? '–' : v.toFixed(d));
  $('benchOut').innerHTML = '<table><tr><th></th><th>Normal</th><th>Tiled</th></tr>' +
    rows.map(([name, fn, d]) => `<tr><td>${name}</td><td>${f(fn(by('full')), d)}</td><td>${f(fn(by('tiled')), d)}</td></tr>`).join('') + '</table>' +
    `<a id="dl" download="tiling-bench.json" href="data:application/json,${encodeURIComponent(JSON.stringify(r, null, 1))}">download JSON</a>`;
}

// ---------------------------------------------------------------- mode UI
$('modeTiled').onchange = () => setMode('tiled');
$('modeFull').onchange = () => setMode('full');
$('modeFull').disabled = !sourceOk;
$('bench').onclick = () => runBench();
window.__demo.setMode = setMode;
window.__demo.sourceVideo = sourceVideo;
window.__demo.runBench = runBench;
if (params.get('mode') === 'full') setMode('full');
if (params.get('bench')) setTimeout(runBench, 1500);
