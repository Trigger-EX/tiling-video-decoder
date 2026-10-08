# tiling-video-decoder

Partial-frame decoding for 360° video on the Oculus Go player in
[synchronized-vr-video-playback](https://github.com/Trigger-EX/synchronized-vr-video-playback):
split the equirect video into a grid of independently encoded tiles and keep hardware decoders
running only on the tiles the headset is looking at.

A single H.264/HEVC stream cannot be decoded in part, so the work is split in three places:

| Piece | Where | What it does |
|---|---|---|
| **Tiler** | `tools/tiler.py` (ffmpeg) | Offline: writes `manifest.json`, a low-res whole-sphere `base.mp4` (with the audio), and one video per tile. All streams share one keyframe grid; each tile carries `pad` pixels copied from its neighbours (wrapped at the seam, mirrored at the poles) so filtering never shows seams. |
| **Core** | `core/` (pure Kotlin) | Manifest parser, sphere/tile geometry, `VisibilityPlanner` (frustum rays → ranked tiles, with margin and predicted poses), `TileScheduler` (decoder budget, linger so border jitter doesn't thrash), `TilePool`, `MediaClock`/`TileDecoder` interfaces, `DecodeCost`. |
| **Browser demo + analytics** | `demo/`, `tools/perf_monitor.py` | `run_demo.py` + `viewer.js`: the pipeline above running in a browser tab, plus the tiled-vs-normal benchmark. `demo/tiling.js` is a port of the core geometry/planner/scheduler, tested against the same cases (`node --test tests/tiling.test.mjs`). |
| **Android decoder** | `decoder-android/` | `MediaCodecTileDecoder`: one reusable decoder slot per `Surface`, follows a `MediaClock`, seeks to the preceding keyframe and decodes forward without showing frames until it catches up. `DecoderProbe`: how many hardware decoders the device really runs at once. |

```
python3 tools/tiler.py my_8k_360.mp4 out/ --cols 6 --rows 3      # 7680x3840 -> 18 tiles of 1280x1280 (+16 pad)
```

## Try it on a laptop (browser demo)

```
python3 demo/run_demo.py                     # builds a 3840x1920 test world, tiles it, opens the viewer
python3 demo/run_demo.py --input my360.mp4   # your own equirect video (width/height must divide into the grid)
```

Needs python3 and ffmpeg/ffprobe on your PATH, plus any browser with WebGL; nothing else to install.
Drag to look around (wheel zooms). The first run takes a minute or two while it encodes the 33 small videos.
Tiles are HEVC by default, as on the headset; if your browser can't decode HEVC (Firefox on Linux, for one) the page
says so, and `--codec h264` fixes it.

**Nothing is encoded twice.** Finished tilesets are kept in a per-user cache (`~/.cache/tiling-video-decoder`, or
`%LOCALAPPDATA%` / `~/Library/Caches`; override with `TILING_CACHE_DIR`), in a folder named for the video and the
settings, so opening the same video again starts instantly. Changing the video file (size or modification time) or a
setting (codec, grid, preset) builds a new one beside it. An interrupted run resumes: each row of tiles is written under
a temporary name and only renamed when complete, so a re-run redoes just the unfinished rows. `--rebuild` forces a fresh
encode, `--no-tile` reopens the last tileset without checking anything, and `--clear-cache` deletes the cache (it refuses
to delete a directory it didn't create). The cache also holds the generated sample video.

It is the same method as the headset design: the whole-sphere `base.mp4` plays as the master clock; a pool of
`<video>` "decoder slots" is pointed at only the tiles you can see (plus a prefetch margin and a short look-ahead
along your head motion); everything else stays on the low-res base layer. The side panel shows how many slots are
in use, the share of full-frame pixels being decoded, tile join time and sync error against the base clock, and a
live map of the grid (green = drawn, amber = joining, grey = lingering, red outline = wanted but out of slots).
Try the **Decoder budget** slider to see what happens when slots run out, **Tint** to see exactly which tiles are
decoding, and **Tiling on** to compare against the base layer alone. `--codec vp9` is available if your browser
lacks H.264. The "pixels decoded" figure is computed from the active tiles, not measured from the browser, and
the demo has not been run on a real GPU yet (only in a headless software-GL browser).

## Choosing a codec (default: HEVC)

`tools/bench_codecs.py` encodes one tile-sized clip with every codec and tier and reports encode CPU, software
decode CPU, PSNR/SSIM at equal bitrate, and whether keyframes land exactly on the GOP grid. Numbers from a 4-core
machine (ffmpeg 6.1, one thread per encode, 512x512 tile, CPU-seconds per second of video, tiers `fast` / `balanced`):

| codec | encode CPU-s | decode CPU-s (software) | quality at ~400 kbit/s (SSIM) | hardware decode where we need it |
|---|---|---|---|---|
| H.264 (x264) | 0.45 / 0.61 | 0.03 | 0.893 / 0.903 | Go: yes (measured). Every laptop GPU, every browser. |
| **HEVC** (x265) | 0.85 / 0.90 | 0.05 | 0.898 / 0.901 | Go: yes (measured). Chrome/Edge/Safari with a GPU; not Firefox on Linux. |
| VP9 (libvpx) | 0.56 / 1.73 | 0.03 | 0.896 / 0.910 | Go: **not measured, assume no**. Newer GPUs only. |
| AV1 (SVT-AV1) | 1.15 / 2.05 | 0.05 | 0.917 / 0.918 | Go: **no**. Only recent GPUs (2020+). |

All four hit the keyframe grid exactly, so any of them tiles correctly. The hardware-decode column comes from the
player repo's measurements (Go) and general browser/GPU support, not from tests here.

**HEVC is the default for `tools/tiler.py`**: the Go's hardware decoders are AVC and HEVC only (per `docs/EXECUTION_PLAN.md`
in the player repo), and HEVC is the one that helps with the headsets' storage and transfer limits. H.264 stays
available (`--codec h264`) and is available for browsers that cannot decode HEVC (Firefox on Linux, for one); the viewer
says so instead of failing silently and names the flag. `demo/run_demo.py` also defaults to HEVC so it matches the headset build. VP9 is a fallback for browsers without H.264; AV1 is not recommended.

**Caveat on the quality column:** the only footage available when this was measured was a synthetic clip, which is too
noisy to separate the codecs by much (all within about 0.025 SSIM; AV1 is slightly ahead at low bitrate). The usual
real-footage advantage of HEVC (roughly 25-40% fewer bits at equal quality) did *not* show up here and should not be
assumed from these numbers. Run `python3 tools/bench_codecs.py --input your_360.mp4` to measure on real content.

## Running politely (CPU and memory limits)

Tiling encodes one video per tile, which can take every core. `tools/tiler.py` (and so `demo/run_demo.py`) now
limits itself by default:

* **One decode per row, not per tile.** A row of tiles is cut by a single ffmpeg process that decodes the source once.
  Decoding the full frame was most of the cost of each tile: on a 3840x1920 test this alone cut CPU use from
  142 to 33 CPU-seconds and wall time from 37 s to 21 s.
* **Adaptive concurrency.** `tools/resources.py` samples *whole-system* CPU and free memory (so other programs count) and
  starts another ffmpeg only if its estimated load still fits under `1 - headroom` of the CPU and leaves the memory
  reserve free. The first job always runs, so it always finishes, just slower when the machine is busy.
* **Low priority.** ffmpeg runs at nice 15 (below-normal on Windows) and low disk priority, so the desktop wins
  immediately if you start doing something, without waiting for the next scheduling decision.
* **Capped threads** per ffmpeg (a quarter of the cores) so a single job cannot spread across every core.

Measured on the same 4-core machine, same 3840x1920 8x4 job:

| | wall | CPU used | system CPU mean / peak | wake-up delay p99 |
|---|---|---|---|---|
| before | 37 s | 142 CPU-s | 97% / 100% | 4.8 ms |
| now (headroom 30%) | 21 s | 33 CPU-s | 40% / 53% | 0.5 ms |

With three other busy processes already holding 3 of the 4 cores it ran one job at a time and still finished (33 s).
Options: `--headroom 0.5` keeps half the CPU free, `--reserve-mem-mb`, `--max-jobs`, `--tiles-per-job`, and
`--no-throttle` for the old all-out behaviour. `pip install psutil` is optional (sampling works without it on Linux;
the Windows and macOS sampling paths are written but untested).

## Performance analytics: tiled vs normal playback

```
python3 demo/run_demo.py --analyze                      # sample world, then benchmark and report
python3 demo/run_demo.py --analyze --input my360.mp4    # your own video
python3 demo/run_demo.py --analyze                       # run it again on the same video: the cached tileset is reused
python3 demo/run_demo.py --analyze --duration 30 --rounds 3
```

Opens the viewer in benchmark mode and runs it through a scripted head path (the same path every time, default
half speed) in **normal playback** (the original video, one decoder, same sphere and renderer) and **tiled**
playback, alternating the order each round so thermal drift cancels. While it runs, `tools/perf_monitor.py` samples
the machine twice a second, and the two are lined up by wall-clock time into `demo/perf/report-*.md` (plus the raw
`bench-*.json` / `samples-*.json`). The viewer also has a **Normal playback** radio button and a **Run benchmark**
button if you want to compare by eye or run it by hand (`?bench=1&duration=20&rounds=2&speed=0.5`).

| Reported | Source |
|---|---|
| Browser CPU (cores busy), browser memory | OS (Linux `/proc`, or `psutil` if installed) |
| System CPU | OS |
| Battery power in watts | Linux battery sensor, **only while unplugged** |
| GPU busy / video-decoder busy | `nvidia-smi` or AMD sysfs if present; **Intel is not read** |
| Frame time p50/p95/p99, hitches, fps, dropped video frames | the page |
| Decoded Mpx/s, decoder instances | modeled from the active tiles |
| View drawn at full resolution, tile join time, tile starts, sync error | the page |
| Tileset size vs original video | disk |

The report keeps both sides of the trade: tiling should lower decoded pixels and (hopefully) CPU/power, but costs
more decoder instances, more disk, and some time with low-resolution base layer showing. Read "View drawn at full
resolution" next to the CPU numbers: a CPU saving that costs visible blur is not a clean win. On a laptop whose GPU
decodes 4K easily, expect a small difference; the strong case is a source the hardware can't decode whole.

Only the page-side numbers and the system CPU path have been exercised, in a software-rendered headless
Chromium, whose absolute numbers mean nothing. Power, GPU and the battery path are untested.

## When the machine can't keep up

Every tile is its own decoder, and a machine (or a browser's HEVC path) that decodes slower than real time used to
get stuck: tiles seeked to "now", could not catch up before "now" moved on, and were seeked again, so none ever
showed. The viewer now handles that in `demo/slotsync.js` (pure logic, unit-tested):

* **Seek ahead and hold.** A joining tile seeks to where the clock *will be* (now + a learned `lead`), decodes up to that
  frame while paused, and starts playing when the clock arrives. The lead is learned from real join times (it rises
  at once on a slow join and falls over a few fast ones), so fast machines see no delay and slow ones stop chasing.
* **Join pacing.** Only a few tiles start at once (more while things are calm), most important first.
* **Adaptive decoder limit (off by default; tick "Adapt decoder count to load").** If tiles stall (the decoder produces nothing for 4 s) or keep arriving late, the
  effective decoder limit drops below your slider setting, and probes back up slowly, waiting longer after each failed
  probe. The panel shows "limited to N" and the benchmark report says so, so a run that was throttled can't pass
  for one that wasn't.
* **Give up on stuck tiles** (free the decoder, don't retry that tile for 4 s) instead of retrying forever.

Measured in a software-rendered Chromium on 4 cores with the browser pinned to fewer cores to starve the decoders
(16-decoder budget, scripted head motion, drawn tiles averaged over 40 s, old logic vs new):

| browser limited to | old: tiles drawn / seconds with nothing drawn | new |
|---|---|---|
| 4 cores (healthy) | 13.3 / 0 s | 12.7 / 0 s (within run-to-run noise) |
| 1 core | 0.4 / 30 of 40 s | 2.8 / 1 of 40 s, 2.5x fewer decoder starts |

Treat these as a demonstration of the failure mode and the fix, not as speeds: this machine decodes VP9 in software,
not your HEVC. "Join time" in the viewer and the benchmark now includes the deliberate hold, so it is not comparable
with numbers from before this change.

### Why the demo server cuts range responses

Browsers allow only about six simultaneous connections to one server. A browser asks for `bytes=N-` (the rest of the
file) and reads it only as fast as it plays, so an answer that really sends the rest of the file keeps a connection
open for as long as the `<video>` exists. With a dozen tile videos, the base layer and the original video all
streaming, they used every connection and everything else waited forever: tiles stuck on "joining" with only a couple
working, a benchmark that hung, and a "needs the original video" error. It only shows with videos longer than the
browser's read-ahead (short test clips fit in one read, so they never showed it). `demo/serve.py` now answers each
range request with at most 1 MB (`TILING_MAX_CHUNK`, 0 = unlimited); the browser asks again when it needs more.
Measured on a 150 s test clip with 16 tiles: 2-4 of 16 tiles drawn and 2.2 s joins before, 13-16 of 16 and 0.4 s
joins after. If you serve a tileset from your own web server, make it do the same (or use HTTP/2).

### Comparing runs fairly

Use `python3 demo/run_demo.py --analyze`: it opens a fresh page for every benchmark. Pressing **Run benchmark**
repeatedly inside one page works (it no longer hangs or refuses to run again), but in my testing the first run in a
page was consistently the best (about 99% of the view drawn), with later runs about 15% lower; I could not tell whether
that is the browser's media cache or my sandbox's software rendering, so don't compare a first run with a fifth.

## How it runs on the headset

1. `base.mp4` plays in the existing `ExoVideoPlayer`: it owns the audio and is the master clock (`MediaClock` wraps its position; bump `epoch` on every seek, load and loop).
2. Every frame, `VisibilityPlanner.plan([pose, predictedPose], fov, margin)` ranks the needed tiles; a few times a second `TilePool.update` hands them to `TileScheduler`, which starts/stops slots within the decoder budget.
3. Each slot is a `MediaCodecTileDecoder` writing into its own SurfaceTexture (the player's `ExternalSurface`). The renderer draws the base layer first, then, for each `TilePool.drawable()` entry, a sphere patch over `TileGeometry.azimuthRange/elevationRange` sampling only `innerRect()` of that slot's texture. Tiles that are not decoding yet show the base layer, never black.

This works on the **sphere fallback path** only: the compositor's equirect layer takes a single Surface and cannot be tiled.

## What was verified, and what was not

Verified here (`python3 -m unittest discover -s tests`, `gradle build`):
tiler output (sizes, no audio on tiles, audio on base, **identical keyframe times across all streams**, wrapped padding content); manifest parsing; tile geometry matching the player's sphere convention (az 0 = −Z, +X right, row 0 = top); visibility incl. wrap-around and poles; scheduler and pool behaviour; the Android decoder compiles against the API 25 framework.

**Not verified: anything on a headset.** `MediaCodecTileDecoder` has never decoded a frame; catch-up time, frame timing against the audio clock, tile-to-tile sync and the renderer integration are untested. Nothing in the player repo was changed.

## Sizing: the decoder count is the limit

Average over head poses (pitch ±45°), Go-like 100°×100° field of view, 7680×3840 source, 16 px pad, 1280×640 base layer included:

| grid | tile | tiles visible avg / max | pixels decoded vs full frame |
|---|---|---|---|
| 6×3 | 60° | 7.9 / 10 | 49% |
| 8×4 | 45° | 12.3 / 16 | 44% |
| 12×6 | 30° | 22.2 / 30 | 37% |
| 16×8 | 22.5° | 34 / 44 | 33% |
| 24×12 | 15° | 69 / 88 | 32% |

The wide field of view and equirect stretching away from the equator mean the saving is roughly half to two thirds, and finer grids need tens of simultaneous decoders, which a Snapdragon 821 will not give. Run `DecoderProbe.measure("video/avc", 1312, 1312)` on a Go first and choose the grid so that the visible tile count fits that number. The larger benefit is resolution: the Go's decoders stop at 4096×2048 for a whole frame, while tiles let a 6K–8K source play with only the viewed part decoded.

Known limits: mono equirect only (the manifest rejects stereo); each tile start costs up to one GOP (`--gop-seconds`, default 1 s) of catch-up; audio is only in `base.mp4`.
