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
| **Browser demo** | `demo/` | `run_demo.py` + `viewer.js`: the pipeline above running in a browser tab. `demo/tiling.js` is a port of the core geometry/planner/scheduler, tested against the same cases (`node --test tests/tiling.test.mjs`). |
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

It is the same method as the headset design: the whole-sphere `base.mp4` plays as the master clock; a pool of
`<video>` "decoder slots" is pointed at only the tiles you can see (plus a prefetch margin and a short look-ahead
along your head motion); everything else stays on the low-res base layer. The side panel shows how many slots are
in use, the share of full-frame pixels being decoded, tile join time and sync error against the base clock, and a
live map of the grid (green = drawn, amber = joining, grey = lingering, red outline = wanted but out of slots).
Try the **Decoder budget** slider to see what happens when slots run out, **Tint** to see exactly which tiles are
decoding, and **Tiling on** to compare against the base layer alone. `--codec vp9` is available if your browser
lacks H.264. The "pixels decoded" figure is computed from the active tiles, not measured from the browser, and
the demo has not been run on a real GPU yet (only in a headless software-GL browser).

## Choosing a codec (default: H.264)

`tools/bench_codecs.py` encodes one tile-sized clip with every codec and tier and reports encode CPU, software
decode CPU, PSNR/SSIM at equal bitrate, and whether keyframes land exactly on the GOP grid. Numbers from a 4-core
machine (ffmpeg 6.1, one thread per encode, 512x512 tile, CPU-seconds per second of video, tiers `fast` / `balanced`):

| codec | encode CPU-s | decode CPU-s (software) | quality at ~400 kbit/s (SSIM) | hardware decode where we need it |
|---|---|---|---|---|
| **H.264** (x264) | **0.45 / 0.61** | **0.03** | 0.893 / 0.903 | Go: yes (measured). Every laptop GPU, every browser. |
| HEVC (x265) | 0.85 / 0.90 | 0.05 | 0.898 / 0.901 | Go: yes (measured). Chrome/Edge/Safari with a GPU; not Firefox on Linux. |
| VP9 (libvpx) | 0.56 / 1.73 | 0.03 | 0.896 / 0.910 | Go: **not measured, assume no**. Newer GPUs only. |
| AV1 (SVT-AV1) | 1.15 / 2.05 | 0.05 | 0.917 / 0.918 | Go: **no**. Only recent GPUs (2020+). |

All four hit the keyframe grid exactly, so any of them tiles correctly. The hardware-decode column comes from the
player repo's measurements (Go) and general browser/GPU support, not from tests here.

**H.264 is the default**, because the decision is made by hardware decoding and encode cost, not by compression:
the Go's hardware decoders (per `docs/EXECUTION_PLAN.md` in the player repo) are AVC and HEVC only, so VP9/AV1 can't
play there; H.264 is the only codec every browser decodes in hardware on every laptop; it encodes 1.5-4x cheaper
than the others; and it is the cheapest to decode. HEVC is the one to switch to (`--codec hevc`) if tile storage or
Wi-Fi transfer to the headsets becomes the bottleneck, since it is also hardware-decoded on the Go. VP9 stays
as a fallback for browsers without H.264; AV1 is not recommended.

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
