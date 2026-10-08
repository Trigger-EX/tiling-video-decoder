#!/usr/bin/env python3
"""Compare the tiler's codecs on one tile-sized clip.

    python3 tools/bench_codecs.py                       # synthetic clip (speed is meaningful, bitrate/quality less so)
    python3 tools/bench_codecs.py --input my360.mp4     # crops a tile-sized region out of your footage (recommended)

For every codec/preset/bitrate it reports CPU-seconds spent encoding (user+sys, so thread count does not
hide cost), PSNR/SSIM against the source tile, CPU-seconds to decode in software (a rough proxy; real
playback uses hardware decoders), the file size, and whether keyframes landed exactly on the GOP grid.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import atexit

try:
    import resource
except ImportError:                # Windows: no child CPU accounting, fall back to wall-clock time
    resource = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tiler  # noqa: E402


def cpu_run(cmd):
    """Runs cmd, returns (wall, cpu seconds of the child tree, stderr)."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.time() - t0
    if r.returncode:
        raise RuntimeError("%s\n%s" % (" ".join(cmd), r.stderr[-800:]))
    if not resource:
        return wall, wall, r.stderr
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return wall, (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), r.stderr


def keyframes(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout.split()
    return sorted(round(float(l.split(",")[0]), 2) for l in out if "K" in l.split(",")[1])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input")
    p.add_argument("--size", type=int, default=512, help="tile size incl. padding")
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--bitrates", default="400,1200", help="kbit/s list for a tile of --size")
    p.add_argument("--codecs", default=",".join(tiler.CODECS))
    p.add_argument("--presets", default=",".join(tiler.PRESETS))
    p.add_argument("--grain", type=int, default=2, help="noise added to the synthetic clip (0 = none)")
    p.add_argument("--json")
    a = p.parse_args()
    fps, gop = 30, 30
    tmp = tempfile.mkdtemp(prefix="tilebench_")
    atexit.register(shutil.rmtree, tmp, True)             # the lossless reference clip is large; never leave it behind
    ref = os.path.join(tmp, "ref.mkv")
    if a.input:
        w, h, _, _ = tiler.probe(a.input)
        src = ["-i", a.input]
        vf = "crop=%d:%d:%d:%d,fps=%d" % (a.size, a.size, (w - a.size) // 2, (h - a.size) // 2, fps)
    else:  # organic detail + motion + a little grain
        src = ["-f", "lavfi", "-i", "mandelbrot=size=%dx%d:rate=%d:end_scale=0.02" % (a.size, a.size, fps)]
        vf = ("noise=alls=%d:allf=t," % a.grain if a.grain else "") + "format=yuv420p"
    subprocess.run(["ffmpeg", "-y", "-v", "error"] + src + ["-t", str(a.seconds), "-vf", vf, "-c:v", "ffv1", ref], check=True)

    rows = []
    for codec in a.codecs.split(","):
        for preset in a.presets.split(","):
            for br in (int(x) for x in a.bitrates.split(",")):
                out = os.path.join(tmp, "%s_%s_%d.mp4" % (codec, preset, br))
                try:
                    wall, cpu, _ = cpu_run(["ffmpeg", "-y", "-v", "error", "-i", ref, "-an"] +
                                           tiler.encoder_args(codec, gop, br, fps, preset, threads=1) + [out])
                except RuntimeError as e:
                    print("skip %s/%s: %s" % (codec, preset, str(e).splitlines()[-1]), file=sys.stderr)
                    break
                q = subprocess.run(["ffmpeg", "-v", "info", "-i", out, "-i", ref, "-lavfi", "[0:v][1:v]ssim;[0:v][1:v]psnr",
                                    "-f", "null", "-"], capture_output=True, text=True).stderr
                ssim = float(re.search(r"SSIM.*All:([0-9.]+)", q).group(1))
                psnr = float(re.search(r"PSNR.*average:([0-9.]+)", q).group(1))
                _, dcpu, _ = cpu_run(["ffmpeg", "-v", "error", "-threads", "1", "-i", out, "-f", "null", "-"])
                ks = keyframes(out)
                expect = [round(i * gop / fps, 2) for i in range(int(a.seconds * fps / gop))]
                rows.append(dict(codec=codec, preset=preset, kbps=br, enc_cpu=cpu / a.seconds, dec_cpu=dcpu / a.seconds,
                                 psnr=psnr, ssim=ssim, kb=os.path.getsize(out) * 8 / 1000 / a.seconds, grid_ok=ks == expect))
    print("| codec | preset | target kbps | actual kbps | PSNR dB | SSIM | encode CPU-s per video-s | sw-decode CPU-s per video-s | keyframe grid |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %(codec)s | %(preset)s | %(kbps)d | %(kb).0f | %(psnr).2f | %(ssim).4f | %(enc_cpu).2f | %(dec_cpu).3f | %(g)s |"
              % dict(r, g="exact" if r["grid_ok"] else "OFF"))
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
