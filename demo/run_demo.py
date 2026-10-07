#!/usr/bin/env python3
"""Build a tileset and open the tiled 360 viewer in your browser.

    python3 demo/run_demo.py                      # generates a 3840x1920 test video, tiles it, opens the viewer
    python3 demo/run_demo.py --input my360.mp4    # your own equirect video (size must divide into the grid)
    python3 demo/run_demo.py --no-tile            # reuse the tileset from the last run
    python3 demo/run_demo.py --analyze            # also benchmark tiled vs normal playback and write a report

Needs: python3, ffmpeg + ffprobe on PATH, and a browser with WebGL (Chrome, Edge, Firefox, Safari).
"""
import argparse
import os
import subprocess
import sys
import json
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, HERE)
import perf_monitor  # noqa: E402
import resources  # noqa: E402
import serve  # noqa: E402
import tiler  # noqa: E402


def make_sample(path, width, height, seconds, codec="h264"):
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
    video = (["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-crf", "20", "-b:v", "0"] if codec == "vp9"
             else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"])
    enc = video + ["-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-shortest", path]
    threads = ["-threads", str(max(1, (os.cpu_count() or 2) // 2))]   # half the cores, at low priority
    for vf in (grid + "," + labels, grid):
        rc, err = resources.run_low_priority(["ffmpeg", "-y", "-v", "error"] + threads + src + ["-vf", vf] + enc)
        if rc == 0:
            return
    raise RuntimeError("could not generate the sample video: " + err[-500:])


def link_source(path):
    """Expose the original video to the viewer as demo/source.mp4 (for the 'normal playback' mode)."""
    dest = os.path.join(HERE, "source.mp4")
    if os.path.lexists(dest):
        os.remove(dest)
    try:
        os.symlink(os.path.abspath(path), dest)
    except OSError:                       # e.g. Windows without symlink rights
        import shutil
        shutil.copyfile(path, dest)


def analyze(a, server, url, tileset_dir, source):
    """Serve, open the benchmark page, sample the machine while it runs, then write the report."""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    windows = 2 * a.rounds
    est = windows * (a.duration + 3 + 2)
    print("\nbenchmark: %d windows of %ds (about %d s). Close other programs, keep the browser tab in the foreground\n"
          "and don't touch the mouse. Plug in or unplug consistently; battery power is only recorded when unplugged." % (windows, a.duration, est))
    mon = perf_monitor.Monitor().start()
    bench_url = "%s?bench=1&duration=%d&rounds=%d" % (url, a.duration, a.rounds)
    if not a.no_open:
        webbrowser.open(bench_url)
    else:
        print("open", bench_url)
    try:
        if not server.perf_event.wait(est + 120):
            print("timed out waiting for the benchmark page", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        return 1
    finally:
        mon.stop()
    path, bench = server.perf_results[-1]
    report = perf_monitor.build_report(bench, mon.rows, mon.cores, tileset_dir, os.path.join(HERE, "source.mp4"))
    rpath = path.replace("bench-", "report-").replace(".json", ".md")
    with open(rpath, "w") as f:
        f.write(report)
    with open(path.replace("bench-", "samples-"), "w") as f:
        json.dump(mon.rows, f)
    print("\n" + report)
    print("saved: %s" % rpath)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", help="equirectangular 360 video (default: generate a test video)")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--codec", choices=("auto",) + tiler.CODECS, default="auto",
                   help="tile codec. auto = h264, which every browser decodes; use hevc in Chrome/Edge/Safari to match the headset")
    p.add_argument("--preset", choices=tiler.PRESETS, default="fast", help="encode speed/quality tier (default: fast)")
    p.add_argument("--headroom", type=float, default=0.30, help="fraction of CPU kept free for other programs (default 0.30)")
    p.add_argument("--no-throttle", action="store_true", help="use every core at full speed (can make the desktop stutter)")
    p.add_argument("--seconds", type=int, default=20, help="length of the generated sample")
    p.add_argument("--size", default="3840x1920", help="size of the generated sample")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-tile", action="store_true", help="skip tiling, reuse demo/tileset")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--analyze", action="store_true", help="run the tiled-vs-normal benchmark, record system load, write a report, exit")
    p.add_argument("--duration", type=int, default=20, help="seconds per benchmark window (default 20)")
    p.add_argument("--rounds", type=int, default=2, help="benchmark rounds; each round runs both modes (default 2)")
    p.add_argument("--reference", help="original video for normal playback if --input is not playable in the browser")
    a = p.parse_args()

    if a.codec == "auto":
        a.codec = "h264"
        print("codec: h264 (plays in every browser; use --codec hevc in Chrome/Edge/Safari to match the headset build)")
    out = os.path.join(HERE, "tileset")
    src = a.input
    if not a.no_tile:
        if not src:
            src = os.path.join(HERE, "sample_360.mp4")
            w, h = (int(x) for x in a.size.split("x"))
            make_sample(src, w, h, a.seconds, "vp9" if a.codec == "vp9" else "h264")
        print("tiling into %dx%d tiles (this encodes %d small videos)..." % (a.cols, a.rows, a.cols * a.rows + 1))
        rc = tiler.main([src, out, "--cols", str(a.cols), "--rows", str(a.rows), "--codec", a.codec,
                         "--preset", a.preset, "--bitrate-k", "16000", "--base-bitrate-k", "1500"] +
                        (["--no-throttle"] if a.no_throttle else ["--headroom", str(a.headroom)]))
        if rc:
            return rc
    elif not os.path.exists(os.path.join(out, "manifest.json")):
        print("no tileset found; run without --no-tile first", file=sys.stderr)
        return 1

    src = src or os.path.join(HERE, "sample_360.mp4")
    link_source(a.reference or src)

    server = serve.make_server(HERE, a.port)
    url = "http://127.0.0.1:%d/index.html" % a.port
    if a.analyze:
        return analyze(a, server, url, out, a.reference or src)
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
