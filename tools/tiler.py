#!/usr/bin/env python3
"""Build a tileset from an equirectangular video.

    python3 tools/tiler.py input.mp4 out_dir --cols 8 --rows 4

Output (out_dir):
    manifest.json   grid, sizes, file names, keyframe interval
    base.mp4        low-resolution whole-sphere layer WITH the audio track (the master clock)
    tiles/t_<row>_<col>.mp4   one independently decodable video per tile, no audio

Every stream shares one keyframe grid (a closed GOP every `--gop-seconds`), so a decoder can start
any tile at the keyframe at or before the current time and be in step with every other tile.
Each tile is stored with `pad` extra pixels on every side, copied from its neighbours (wrapping
horizontally, mirrored over the poles), so bilinear filtering at tile borders never shows a seam.
Coded tile sizes must be multiples of 16 (H.264 macroblocks / hardware decoder alignment).
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

FORMAT_VERSION = 1


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(cmd), r.stderr[-2000:]))
    return r.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height,r_frame_rate:format=duration", "-of", "json", path])
    j = json.loads(out)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return s["width"], s["height"], float(num) / float(den), float(j["format"]["duration"])


def has_audio(path):
    return bool(run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                     "stream=index", "-of", "csv=p=0", path]).strip())


def plan(width, height, cols, rows, pad):
    """Validates the grid and returns (tile_w, tile_h, coded_w, coded_h)."""
    if width % cols or height % rows:
        raise ValueError("video %dx%d is not divisible by a %dx%d grid" % (width, height, cols, rows))
    tw, th = width // cols, height // rows
    cw, ch = tw + 2 * pad, th + 2 * pad
    if cw % 16 or ch % 16:
        raise ValueError("coded tile size %dx%d (tile %dx%d + 2*pad %d) must be a multiple of 16"
                         % (cw, ch, tw, th, pad))
    return tw, th, cw, ch


def padded_source_filter(width, height, pad):
    """Filter graph text producing [p]: the source with `pad` px wrapped left/right and mirrored top/bottom."""
    if pad == 0:
        return "[0:v]null[p]"
    return (
        "[0:v]split=3[a][b][c];"
        "[a]crop=%d:%d:%d:0[l];[c]crop=%d:%d:0:0[r];[l][b][r]hstack=3[w];"
        "[w]pad=%d:%d:0:%d,fillborders=top=%d:bottom=%d:mode=mirror[p]"
        % (pad, height, width - pad, pad, height, width + 2 * pad, height + 2 * pad, pad, pad, pad)
    )


def x264_args(codec, gop, bitrate_k, fps):
    common = ["-pix_fmt", "yuv420p", "-b:v", "%dk" % bitrate_k,
              "-maxrate", "%dk" % int(bitrate_k * 1.5), "-bufsize", "%dk" % (bitrate_k * 2)]
    if codec == "h264":
        return ["-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-bf", "0",
                "-x264-params", "keyint=%d:min-keyint=%d:scenecut=0:open-gop=0" % (gop, gop)] + common
    return ["-c:v", "libx265", "-preset", "medium", "-tag:v", "hvc1",
            "-x265-params", "keyint=%d:min-keyint=%d:scenecut=0:open-gop=0:bframes=0:log-level=error"
            % (gop, gop)] + common


def build(args):
    width, height, fps, duration = probe(args.input)
    tw, th, cw, ch = plan(width, height, args.cols, args.rows, args.pad)
    gop = max(1, round(fps * args.gop_seconds))
    base_w = args.base_width
    base_h = (base_w // 2) // 2 * 2
    os.makedirs(os.path.join(args.output, "tiles"), exist_ok=True)

    tile_bitrate = args.tile_bitrate_k or max(200, int(args.bitrate_k * (cw * ch) / (width * height) * 1.0))
    jobs = []
    for r in range(args.rows):
        for c in range(args.cols):
            name = "tiles/t_%d_%d.mp4" % (r, c)
            graph = "%s;[p]crop=%d:%d:%d:%d[t]" % (padded_source_filter(width, height, args.pad),
                                                   cw, ch, c * tw, r * th)
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", args.input, "-filter_complex", graph,
                   "-map", "[t]", "-an"] + x264_args(args.codec, gop, tile_bitrate, fps) + \
                  ["-movflags", "+faststart", os.path.join(args.output, name)]
            jobs.append((name, cmd))

    base_cmd = ["ffmpeg", "-y", "-v", "error", "-i", args.input, "-map", "0:v:0"]
    if has_audio(args.input):
        base_cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
    base_cmd += ["-vf", "scale=%d:%d" % (base_w, base_h)] + x264_args(args.codec, gop, args.base_bitrate_k, fps) + \
                ["-movflags", "+faststart", os.path.join(args.output, "base.mp4")]
    jobs.append(("base.mp4", base_cmd))

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for f in [ex.submit(run, cmd) for _, cmd in jobs]:
            f.result()

    manifest = {
        "version": FORMAT_VERSION,
        "projection": "equirect",
        "stereo": "none",
        "width": width, "height": height, "fps": fps, "durationSeconds": duration,
        "codec": args.codec,
        "gopFrames": gop, "gopSeconds": gop / fps,
        "grid": {"cols": args.cols, "rows": args.rows, "tileWidth": tw, "tileHeight": th, "pad": args.pad},
        "base": {"file": "base.mp4", "width": base_w, "height": base_h},
        "tiles": [{"row": r, "col": c, "file": "tiles/t_%d_%d.mp4" % (r, c)}
                  for r in range(args.rows) for c in range(args.cols)],
    }
    with open(os.path.join(args.output, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--pad", type=int, default=16, help="border pixels copied from neighbours (default 16)")
    p.add_argument("--codec", choices=["h264", "hevc"], default="h264")
    p.add_argument("--gop-seconds", type=float, default=1.0, help="keyframe interval; bounds tile start-up latency")
    p.add_argument("--base-width", type=int, default=1280, help="whole-sphere fallback layer width (2:1)")
    p.add_argument("--bitrate-k", type=int, default=30000, help="budget for the full frame; tiles get their share")
    p.add_argument("--tile-bitrate-k", type=int, default=0, help="override per-tile bitrate")
    p.add_argument("--base-bitrate-k", type=int, default=2500)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    args = p.parse_args(argv)
    try:
        m = build(args)
    except (ValueError, RuntimeError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    print("wrote %d tiles + base to %s" % (len(m["tiles"]), args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
