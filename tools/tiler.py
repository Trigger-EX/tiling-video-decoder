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
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resources  # noqa: E402

FORMAT_VERSION = 1
ENCODER_REV = 1                     # bump when encoder arguments change in a way that alters the output
STATE_FILE = "tileset.state.json"   # which video + settings produced the files in an output directory


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (" ".join(cmd), r.stderr[-2000:]))
    return r.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height,r_frame_rate,avg_frame_rate:format=duration", "-of", "json", path])
    j = json.loads(out)
    s = j["streams"][0]
    fps = 0.0
    for key in ("r_frame_rate", "avg_frame_rate"):          # r_frame_rate can be 0/0 for some containers
        num, _, den = s.get(key, "0/0").partition("/")
        if float(den or 1) and float(num) > 0:
            fps = float(num) / float(den or 1)
            break
    if fps <= 0:
        raise ValueError("cannot determine the frame rate of %s" % path)
    return s["width"], s["height"], fps, float(j["format"]["duration"])


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


PRESETS = ("fast", "balanced", "quality")
CODECS = ("h264", "hevc", "vp9", "av1")
_X26X = {"fast": "veryfast", "balanced": "medium", "quality": "slow"}
_VP9 = {"fast": ["-deadline", "realtime", "-cpu-used", "6"], "balanced": ["-deadline", "good", "-cpu-used", "3"],
        "quality": ["-deadline", "good", "-cpu-used", "1"]}
_AV1 = {"fast": "10", "balanced": "8", "quality": "5"}


def encoder_args(codec, gop, bitrate_k, fps, preset="balanced", threads=0):
    """ffmpeg video-encoder arguments. All codecs get the same closed-GOP keyframe grid (no scene-cut
    keyframes, no B-frame reordering), so tiles can start at any keyframe and stay in step.
    `threads` caps the encoder's worker threads (0 = encoder default, i.e. every core)."""
    if preset not in PRESETS:
        raise ValueError("preset must be one of %s" % ", ".join(PRESETS))
    t = ["-threads", str(threads)] if threads else []
    rate = ["-pix_fmt", "yuv420p", "-b:v", "%dk" % bitrate_k,
            "-maxrate", "%dk" % int(bitrate_k * 1.5), "-bufsize", "%dk" % (bitrate_k * 2)]
    if codec == "h264":
        return ["-c:v", "libx264", "-profile:v", "high", "-preset", _X26X[preset], "-bf", "0"] + t + \
               ["-x264-params", "keyint=%d:min-keyint=%d:scenecut=0:open-gop=0" % (gop, gop)] + rate
    if codec == "hevc":
        pools = ":pools=%d" % threads if threads else ""
        return ["-c:v", "libx265", "-preset", _X26X[preset], "-tag:v", "hvc1"] + t + \
               ["-x265-params", "keyint=%d:min-keyint=%d:scenecut=0:open-gop=0:bframes=0:log-level=error%s"
                % (gop, gop, pools)] + rate
    if codec == "vp9":
        return ["-c:v", "libvpx-vp9"] + _VP9[preset] + ["-row-mt", "1", "-lag-in-frames", "0", "-auto-alt-ref", "0",
                "-g", str(gop), "-keyint_min", str(gop), "-force_key_frames", "expr:gte(t,n_forced*%s)" % (gop / fps)] + t + rate
    if codec == "av1":
        return ["-c:v", "libsvtav1", "-preset", _AV1[preset], "-g", str(gop)] + t + \
               ["-svtav1-params", "keyint=%d:scd=0:irefresh-type=2" % gop] + rate
    raise ValueError("unknown codec %s" % codec)


def fingerprint(args, width, height, fps):
    """Everything that changes the bytes we would produce (not CPU/thread/priority options)."""
    st = os.stat(args.input)
    return {"rev": ENCODER_REV, "format": FORMAT_VERSION, "input": [st.st_size, st.st_mtime_ns], "source": [width, height, fps],
            "codec": args.codec, "preset": args.preset, "cols": args.cols, "rows": args.rows, "pad": args.pad,
            "gop_seconds": args.gop_seconds, "base_width": args.base_width, "bitrate_k": args.bitrate_k,
            "tile_bitrate_k": args.tile_bitrate_k, "base_bitrate_k": args.base_bitrate_k}


def _remove_managed(out):
    """Deletes only the files this tool writes, never anything else that happens to be in the directory."""
    for pattern in ("tiles/*.mp4", "tiles/*.part", "base.mp4", "base.mp4.part", "manifest.json", STATE_FILE, STATE_FILE + ".part"):
        for f in glob.glob(os.path.join(glob.escape(out), pattern)):      # escape: the folder name may contain [ ] * ?
            os.remove(f)


def _write_json_atomic(path, obj, **kw):
    """Write then rename, so a crash can never leave a half-written (and therefore 'unknown') state or manifest."""
    with open(path + ".part", "w") as f:
        json.dump(obj, f, **kw)
    os.replace(path + ".part", path)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ffmpeg_major():
    try:
        out = run(["ffmpeg", "-version"]).split()[2]
        return int(out.lstrip("nN").split(".")[0])
    except (RuntimeError, IndexError, ValueError, OSError):
        return 6


class _Unlimited:
    """Stand-in governor for --no-throttle: start everything immediately."""
    busy = mem_mb = None

    def __init__(self, n): self.n = n
    def sample(self): pass
    def may_start(self, running): return True, ""
    def started(self): pass


def _publisher(finals):
    """Callback that moves a finished job's `.part` outputs into place, so a file exists only if it is complete."""
    def publish():
        for f in finals:
            os.replace(f + ".part", f)
    return publish


def build(args):
    width, height, fps, duration = probe(args.input)
    tw, th, cw, ch = plan(width, height, args.cols, args.rows, args.pad)
    gop = max(1, round(fps * args.gop_seconds))
    base_w = args.base_width
    base_h = (base_w // 2) // 2 * 2
    os.makedirs(os.path.join(args.output, "tiles"), exist_ok=True)

    # What is already in the output directory? Reuse it if it was built from this video with these settings,
    # resume it if the build was interrupted, otherwise start clean.
    fp = fingerprint(args, width, height, fps)
    state = _load_json(os.path.join(args.output, STATE_FILE))
    same = bool(state) and state.get("config") == fp
    if args.rebuild or not same:
        _remove_managed(args.output)
        state = None
    else:
        for f in glob.glob(os.path.join(glob.escape(args.output), "**", "*.part"), recursive=True):
            os.remove(f)                                    # half-written output of an interrupted run
    manifest_path = os.path.join(args.output, "manifest.json")
    args.reused = False
    if state and state.get("complete") and os.path.exists(manifest_path):
        existing = _load_json(manifest_path)
        if existing:
            paths = [os.path.join(args.output, t["file"]) for t in existing["tiles"]] + [os.path.join(args.output, existing["base"]["file"])]
            if all(os.path.isfile(x) and os.path.getsize(x) > 0 for x in paths):
                args.reused = True
                return existing
    _write_json_atomic(os.path.join(args.output, STATE_FILE), {"config": fp, "complete": False, "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

    tile_bitrate = args.tile_bitrate_k or max(200, int(args.bitrate_k * (cw * ch) / (width * height) * 1.0))
    gov = resources.Governor(headroom=args.headroom, reserve_mem_mb=args.reserve_mem_mb, max_jobs=args.max_jobs) \
        if args.headroom is not None else None
    cores = gov.cores if gov else (os.cpu_count() or 1)
    threads = args.encoder_threads or max(1, cores // 4)
    per_job = max(1, min(args.tiles_per_job or args.cols, args.cols))

    # One ffmpeg process decodes the source once and writes `per_job` tiles of a row. Decoding the whole
    # frame is most of the cost of a tile, so doing it once per row (not once per tile) uses ~4x less CPU.
    jobs = []
    for r in range(args.rows):
        for i in range(0, args.cols, per_job):
            jobs.append([(r, c) for c in range(i, min(i + per_job, args.cols))])

    cmds, skipped = [], 0
    for chunk in jobs:
        n = len(chunk)
        graph = padded_source_filter(width, height, args.pad)
        if n == 1:
            graph += ";[p]crop=%d:%d:%d:%d[t0]" % (cw, ch, chunk[0][1] * tw, chunk[0][0] * th)
        else:
            graph += ";[p]split=%d%s;" % (n, "".join("[q%d]" % k for k in range(n)))
            graph += ";".join("[q%d]crop=%d:%d:%d:%d[t%d]" % (k, cw, ch, c * tw, r * th, k) for k, (r, c) in enumerate(chunk))
        finals = [os.path.join(args.output, "tiles/t_%d_%d.mp4" % (r, c)) for r, c in chunk]
        if all(os.path.exists(f) for f in finals):          # finished by an earlier (interrupted) run
            skipped += 1
            continue
        cmd = ["ffmpeg", "-y", "-v", "error", "-threads", str(threads), "-filter_complex_threads", "1",
               "-i", args.input, "-filter_complex", graph]
        for k, final in enumerate(finals):
            cmd += ["-map", "[t%d]" % k, "-an"] + encoder_args(args.codec, gop, tile_bitrate, fps, args.preset, threads) + \
                   ["-movflags", "+faststart", "-f", "mp4", final + ".part"]
        cmds.append(("row %d tiles %d-%d" % (chunk[0][0], chunk[0][1], chunk[-1][1]), cmd, _publisher(finals)))

    base_final = os.path.join(args.output, "base.mp4")
    if os.path.exists(base_final):
        skipped += 1
    else:
        base_cmd = ["ffmpeg", "-y", "-v", "error", "-threads", str(threads), "-i", args.input, "-map", "0:v:0"]
        if has_audio(args.input):
            base_cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        base_cmd += ["-vf", "scale=%d:%d" % (base_w, base_h)] + \
            encoder_args(args.codec, gop, args.base_bitrate_k, fps, args.preset, threads) + \
            ["-movflags", "+faststart", "-f", "mp4", base_final + ".part"]
        cmds.append(("base layer", base_cmd, _publisher([base_final])))
    if skipped:
        print("resuming: %d of %d jobs already finished" % (skipped, skipped + len(cmds)))

    if not cmds:
        pass
    elif gov:
        # A job's encoders may each get a thread in newer ffmpeg; plan for the worst case there.
        gov.job_cores = float(threads) if ffmpeg_major() < 7 else float(min(per_job * threads, cores))
        gov.job_mem_mb = width * height * 1.5 * 28 / 2**20 + 100           # ~28 source frames in flight
        resources.run_jobs(cmds, gov, log=print if not args.quiet else (lambda *_: None))
    else:                                                                    # --no-throttle: all at once, as before
        resources.run_jobs(cmds, _Unlimited(len(cmds)), log=print if not args.quiet else (lambda *_: None))

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
    _write_json_atomic(manifest_path, manifest, indent=2)
    _write_json_atomic(os.path.join(args.output, STATE_FILE), {"config": fp, "complete": True, "built": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--pad", type=int, default=16, help="border pixels copied from neighbours (default 16)")
    p.add_argument("--codec", choices=CODECS, default="hevc",
                   help="default hevc: smaller files and hardware-decoded on the Oculus Go; browsers may need h264")
    p.add_argument("--preset", default="balanced", choices=PRESETS, help="encoder speed/quality tier")
    p.add_argument("--encoder-threads", type=int, default=0, help="threads per encoder (0 = encoder default)")
    p.add_argument("--gop-seconds", type=float, default=1.0, help="keyframe interval; bounds tile start-up latency")
    p.add_argument("--base-width", type=int, default=1280, help="whole-sphere fallback layer width (2:1)")
    p.add_argument("--bitrate-k", type=int, default=30000, help="budget for the full frame; tiles get their share")
    p.add_argument("--tile-bitrate-k", type=int, default=0, help="override per-tile bitrate")
    p.add_argument("--base-bitrate-k", type=int, default=2500)
    g = p.add_argument_group("CPU/memory limits (default: stay out of the way of the rest of the system)")
    g.add_argument("--headroom", type=float, default=0.30,
                   help="fraction of total CPU to keep idle for other programs (default 0.30)")
    g.add_argument("--reserve-mem-mb", type=int, default=2048, help="memory to keep available (default 2048)")
    g.add_argument("--max-jobs", type=int, default=None, help="cap on simultaneous ffmpeg processes")
    g.add_argument("--tiles-per-job", type=int, default=None, help="tiles per ffmpeg process (default: one whole row)")
    g.add_argument("--no-throttle", dest="headroom", action="store_const", const=None,
                   help="start every job at once at normal priority (fastest, can freeze the desktop)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--rebuild", action="store_true", help="ignore anything already built in the output directory")
    args = p.parse_args(argv)
    try:
        m = build(args)
    except (ValueError, RuntimeError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    except FileNotFoundError as e:                               # ffmpeg/ffprobe missing from PATH (or the input file)
        print("error: %s not found. Install ffmpeg (it provides ffprobe) and make sure it is on your PATH." % (e.filename or "a required program"), file=sys.stderr)
        return 1
    print(("tileset already built, reusing %s" if getattr(args, "reused", False) else "wrote %d tiles + base to %s")
          % ((args.output,) if getattr(args, "reused", False) else (len(m["tiles"]), args.output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
