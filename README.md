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
