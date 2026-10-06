import { Grid, planVisible, Scheduler, rotation, apply, tileKey, rad } from './tiling.js';

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

// A slot is one decoder instance: a <video> element that is pointed at different tiles. Releasing
// a slot (removing its src) frees the browser's hardware decoder, which is the point of tiling.
let slots = [];
function makeSlot() {
  const s = { video: makeVideo(), tile: null, key: null, assignedAt: 0, readyAt: 0, ready: false, dirty: false, frames: 0, texture: createTexture(), dropped: 0 };
  if ('requestVideoFrameCallback' in s.video) {
    const onFrame = () => { s.dirty = true; s.frames++; s.video.requestVideoFrameCallback(onFrame); };
    s.video.requestVideoFrameCallback(onFrame);
  }
  return s;
}
function setBudget(n) {
  while (slots.length < n) slots.push(makeSlot());
  while (slots.length > n) { const s = slots.pop(); release(s); gl.deleteTexture(s.texture); }
  scheduler = new Scheduler(n, 1500);
  $('vBudget').textContent = n;
}
function assign(s, key) {
  const [row, col] = key.split('_').map(Number);
  s.tile = { row, col }; s.key = key; s.ready = false; s.frames = 0; s.dirty = false; s.assignedAt = performance.now();
  s.video.src = base + fileOf[key];
  s.video.addEventListener('loadedmetadata', () => { s.video.currentTime = baseVideo.currentTime; }, { once: true });
  if (playing) s.video.play().catch(() => {});
  stats.starts++;
}
function release(s) {
  s.video.pause(); s.video.removeAttribute('src'); s.video.load();
  s.tile = null; s.key = null; s.ready = false;
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
const stats = { starts: 0, joinTotal: 0, joins: 0, tilesWanted: 0, tilesDrawn: 0, maxSync: 0 };
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
$('play').onclick = () => { playing = !playing; $('play').textContent = playing ? 'Pause' : 'Play'; for (const v of [baseVideo, ...slots.map((s) => s.video)]) { if (!v.src) continue; playing ? v.play().catch(() => {}) : v.pause(); } };
$('mute').onclick = () => { baseVideo.muted = !baseVideo.muted; $('mute').textContent = baseVideo.muted ? 'Unmute' : 'Mute'; };
$('tour').onclick = () => { tourOn = !tourOn; tourT = 0; };
let tourT = 0;
setFov(90); $('vMargin').textContent = $('margin').value; setBudget(12);
baseVideo.play().catch(() => msg('Click anywhere to start playback.'));
addEventListener('pointerdown', () => { msg(''); if (playing) baseVideo.play().catch(() => {}); }, { once: true });

// ---------------------------------------------------------------- per-frame work
let lastPlan = 0, last = performance.now(), lastYaw = yaw, lastPitch = pitch, fpsCount = 0, fpsT = 0, shownFps = 0;

function updateSlots(now) {
  const tilingOn = $('tiling').checked;
  // Plan a few times per second, not every frame.
  if (now - lastPlan > 100) {
    lastPlan = now;
    const aspect = canvas.clientWidth / canvas.clientHeight;
    const halfV = fovV / 2, halfH = Math.atan(Math.tan(halfV) * aspect);
    const views = [{ yaw, pitch }];
    if ($('predict').checked) views.push({ yaw: yaw + vel.yaw * 0.4, pitch: clampPitch(pitch + vel.pitch * 0.4) });
    wantedTiles = tilingOn ? planVisible(grid, views, halfH, halfV, rad($('margin').valueAsNumber)) : [];
    const active = new Set(slots.filter((s) => s.key).map((s) => s.key));
    const d = scheduler.update(now, wantedTiles, active);
    for (const k of d.stop) release(slots.find((s) => s.key === k));
    for (const k of d.start) assign(slots.find((s) => !s.key), k);
  }
  const clock = baseVideo.currentTime, dur = baseVideo.duration || manifest.durationSeconds;
  let maxSync = 0;
  for (const s of slots) {
    if (!s.key) continue;
    const v = s.video;
    let diff = v.currentTime - clock;
    if (diff > dur / 2) diff -= dur; else if (diff < -dur / 2) diff += dur;   // loop wrap
    const ok = v.readyState >= 2 && !v.seeking && s.frames > 0 && Math.abs(diff) < 0.2;
    if (ok && !s.ready) { s.ready = true; s.readyAt = now; stats.joinTotal += now - s.assignedAt; stats.joins++; }
    if (!ok && s.ready && Math.abs(diff) >= 0.2) s.ready = false;
    if (v.readyState >= 1 && playing) {
      if (Math.abs(diff) > 0.3) { v.currentTime = clock; }                                  // re-seek: decoder restarts at the keyframe before
      else v.playbackRate = Math.max(0.9, Math.min(1.1, 1 - diff * 0.5));                   // gentle nudge
    } else if (v.readyState >= 1 && Math.abs(diff) > 0.05) v.currentTime = clock;
    if (s.ready) maxSync = Math.max(maxSync, Math.abs(diff));
    const q = v.getVideoPlaybackQuality?.(); if (q) s.dropped = q.droppedVideoFrames;
  }
  stats.maxSync = maxSync;
}

function upload(texture, video, dirty) {
  if (!dirty || video.readyState < 2) return;
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
}

function frame(now) {
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  if (tourOn) {                                    // repeatable head sweep: look around, up and down, then behind
    tourT += dt;
    yaw = rad(110) * Math.sin(tourT * 0.55) + tourT * 0.35; pitch = rad(40) * Math.sin(tourT * 0.8);
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

  upload(baseTexture, baseVideo, true);
  draw(sphere, baseTexture, [0, 0, 0, 0]);
  let drawn = 0;
  const tint = $('tint').checked ? [0.1, 0.9, 0.5, 0.22] : [0, 0, 0, 0];
  for (const s of slots) {
    if (!s.key || !s.ready) continue;
    upload(s.texture, s.video, s.dirty || !('requestVideoFrameCallback' in s.video)); s.dirty = false;
    draw(patches.get(s.key), s.texture, tint); drawn++;
  }
  stats.tilesDrawn = drawn;
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
  const decodedPx = (active.length * codedPx + basePx) * fps, fullPx = grid.width * grid.height * fps;
  const pct = (100 * decodedPx) / fullPx;
  $('sSlots').textContent = `${active.length} / ${slots.length}`;
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
  window.__stats = { slots: active.length, budget: slots.length, wanted: wantedTiles.length, drawn: stats.tilesDrawn, pct, starts: stats.starts, joinMs: stats.joins ? stats.joinTotal / stats.joins : null, maxSyncMs: stats.maxSync * 1000, fps: shownFps, yaw, pitch };
}
requestAnimationFrame(frame);
window.__demo = { setTour: (on) => { tourOn = on; tourT = 0; }, setView: (y, p) => { tourOn = false; yaw = y; pitch = p; } };
