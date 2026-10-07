#!/usr/bin/env python3
"""Build a tileset and open the tiled 360 viewer in your browser.

    python3 demo/run_demo.py                      # generates a 3840x1920 test video, tiles it, opens the viewer
    python3 demo/run_demo.py --input my360.mp4    # your own equirect video (size must divide into the grid)
    python3 demo/run_demo.py --no-tile            # reuse the tileset from the last run

Needs: python3, ffmpeg + ffprobe on PATH, and a browser with WebGL (Chrome, Edge, Firefox, Safari).
"""
import argparse
import os
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, HERE)
import resources  # noqa: E402
import serve  # noqa: E402
import tiler  # noqa: E402


def make_sample(path, width, height, seconds):
    """Synthetic 'world': drifting colour gradients with a 10/30 degree grid, a labelled compass and a clock,
    so sharpness, tile seams and sync are all easy to see. Needs only ffmpeg (labels are skipped if drawtext fails)."""
    print("generating %dx%d sample video (%ds)..." % (width, height, seconds))
    grid = ("drawgrid=w=iw/36:h=ih/18:t=2:c=white@0.55,"      # every 10 degrees
            "drawgrid=w=iw/12:h=ih/6:t=6:c=yellow@0.85,"      # every 30 degrees
            "drawbox=x=0:y=ih/2-3:w=iw:h=6:c=red@0.9:t=fill")  # horizon
    labels = ",".join(
        "drawtext=text='%s':x=%d*w/360-tw/2:y=h/2-th-12:fontsize=h/14:fontcolor=white:box=1:boxcolor=black@0.6"
        % (name, 180 + deg) for name, deg in [("AHEAD 0", 0), ("RIGHT 90", 90), ("BEHIND 180", 180), ("LEFT -90", -90)]
    ) + ",drawtext=text='%{pts\\:hms}':x=(w-tw)/2:y=h/2+40:fontsize=h/10:fontcolor=white:box=1:boxcolor=black@0.6"
    src = ["-f", "lavfi", "-i", "gradients=size=%dx%d:rate=30:duration=%d:speed=0.03:nb_colors=5" % (width, height, seconds),
           "-f", "lavfi", "-i", "sine=frequency=330:duration=%d" % seconds]
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-g", "30",
           "-c:a", "aac", "-shortest", path]
    threads = ["-threads", str(max(1, (os.cpu_count() or 2) // 2))]   # half the cores, at low priority
    for vf in (grid + "," + labels, grid):
        rc, err = resources.run_low_priority(["ffmpeg", "-y", "-v", "error"] + threads + src + ["-vf", vf] + enc)
        if rc == 0:
            return
    raise RuntimeError("could not generate the sample video: " + err[-500:])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", help="equirectangular 360 video (default: generate a test video)")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--codec", choices=tiler.CODECS, default="h264")
    p.add_argument("--preset", choices=tiler.PRESETS, default="fast", help="encode speed/quality tier (default: fast)")
    p.add_argument("--headroom", type=float, default=0.30, help="fraction of CPU kept free for other programs (default 0.30)")
    p.add_argument("--no-throttle", action="store_true", help="use every core at full speed (can make the desktop stutter)")
    p.add_argument("--seconds", type=int, default=20, help="length of the generated sample")
    p.add_argument("--size", default="3840x1920", help="size of the generated sample")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-tile", action="store_true", help="skip tiling, reuse demo/tileset")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    a = p.parse_args()

    out = os.path.join(HERE, "tileset")
    if not a.no_tile:
        src = a.input
        if not src:
            src = os.path.join(HERE, "sample_360.mp4")
            w, h = (int(x) for x in a.size.split("x"))
            make_sample(src, w, h, a.seconds)
        print("tiling into %dx%d tiles (this encodes %d small videos)..." % (a.cols, a.rows, a.cols * a.rows + 1))
        rc = tiler.main([src, out, "--cols", str(a.cols), "--rows", str(a.rows), "--codec", a.codec,
                         "--preset", a.preset, "--bitrate-k", "16000", "--base-bitrate-k", "1500"] +
                        (["--no-throttle"] if a.no_throttle else ["--headroom", str(a.headroom)]))
        if rc:
            return rc
    elif not os.path.exists(os.path.join(out, "manifest.json")):
        print("no tileset found; run without --no-tile first", file=sys.stderr)
        return 1

    server = serve.make_server(HERE, a.port)
    url = "http://127.0.0.1:%d/index.html" % a.port
    print("\nviewer: %s   (Ctrl+C to stop)" % url)
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
