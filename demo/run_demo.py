#!/usr/bin/env python3
"""Build a tileset and open the tiled 360 viewer in your browser.

    python3 demo/run_demo.py                      # generates a 3840x1920 test video, tiles it, opens the viewer
    python3 demo/run_demo.py --input my360.mp4    # your own equirect video (size must divide into the grid)
    python3 demo/run_demo.py                      # run it again: reuses the finished tileset (cached per video + settings)
    python3 demo/run_demo.py --no-tile            # open the tileset from the last run without even checking the video
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
import cachedir  # noqa: E402
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
    enc = video + ["-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-shortest", "-f", "mp4", path]
    threads = ["-threads", str(max(1, (os.cpu_count() or 2) // 2))]   # half the cores, at low priority
    for vf in (grid + "," + labels, grid):
        rc, err = resources.run_low_priority(["ffmpeg", "-y", "-v", "error"] + threads + src + ["-vf", vf] + enc)
        if rc == 0:
            return
    raise RuntimeError("could not generate the sample video: " + err[-500:])


def link_source(dest_dir, path):
    """Expose the original video next to its tileset as source.mp4 (the viewer's 'normal playback' mode)."""
    dest = os.path.join(dest_dir, "source.mp4")
    target = os.path.abspath(path)
    if os.path.islink(dest) and os.path.realpath(dest) == os.path.realpath(target):
        return dest
    if os.path.lexists(dest):
        os.remove(dest)
    try:
        os.symlink(target, dest)
    except OSError:                       # e.g. Windows without symlink rights
        import shutil
        shutil.copyfile(path, dest)
    return dest


def report_paths(bench_path):
    """(report.md, samples.json) next to a bench-<stamp>.json, built from the file name only (the folder may contain 'bench-')."""
    folder, name = os.path.split(bench_path)
    stem = os.path.splitext(name[len("bench-"):] if name.startswith("bench-") else name)[0]
    return os.path.join(folder, "report-%s.md" % stem), os.path.join(folder, "samples-%s.json" % stem)


def analyze(a, server, url, tileset_dir, source):
    """Serve, open the benchmark page, sample the machine while it runs, then write the report."""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    windows = 2 * a.rounds
    est = windows * (a.duration + 3 + 2)
    print("\nbenchmark: %d windows of %ds (about %d s). Close other programs, keep the browser tab in the foreground\n"
          "and don't touch the mouse. Plug in or unplug consistently; battery power is only recorded when unplugged." % (windows, a.duration, est))
    mon = perf_monitor.Monitor().start()
    bench_url = "%s&bench=1&duration=%d&rounds=%d" % (url, a.duration, a.rounds)
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
    if bench.get("error"):                                  # the page gave up (e.g. the original video would not play)
        print("\nbenchmark stopped: %s" % bench["error"], file=sys.stderr)
        return 1
    rpath, spath = report_paths(path)
    with open(spath, "w") as f:                             # keep the raw measurements even if the report fails
        json.dump(mon.rows, f)
    try:
        report = perf_monitor.build_report(bench, mon.rows, mon.cores, tileset_dir, source)
    except Exception as e:                                  # noqa: BLE001 - never lose a finished benchmark to a report bug
        print("could not build the report (%s: %s); raw results kept in %s and %s" % (type(e).__name__, e, path, spath), file=sys.stderr)
        return 1
    with open(rpath, "w") as f:
        f.write(report)
    print("\n" + report)
    print("saved: %s" % rpath)
    return 0


def sample_path(root, a):
    """A generated sample is kept in the cache and reused; written under a temporary name so it is never half-made."""
    w, h = (int(x) for x in a.size.split("x"))
    codec = "vp9" if a.codec == "vp9" else "h264"
    path = os.path.join(root, "samples", "sample-%dx%d-%ds-%s.mp4" % (w, h, a.seconds, codec))
    if os.path.exists(path):
        print("using cached sample video %s" % path)
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    make_sample(path + ".part", w, h, a.seconds, codec)
    os.replace(path + ".part", path)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", help="equirectangular 360 video (default: generate a test video)")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--codec", choices=tiler.CODECS, default="hevc",
                   help="tile codec (default hevc, as on the headset). If your browser cannot decode HEVC (e.g. Firefox on Linux) use h264")
    p.add_argument("--preset", choices=tiler.PRESETS, default="fast", help="encode speed/quality tier (default: fast)")
    p.add_argument("--headroom", type=float, default=0.30, help="fraction of CPU kept free for other programs (default 0.30)")
    p.add_argument("--no-throttle", action="store_true", help="use every core at full speed (can make the desktop stutter)")
    p.add_argument("--seconds", type=int, default=20, help="length of the generated sample")
    p.add_argument("--size", default="3840x1920", help="size of the generated sample")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-tile", action="store_true", help="skip tiling; open the tileset from the last run")
    p.add_argument("--rebuild", action="store_true", help="ignore the cached tileset and encode again")
    p.add_argument("--clear-cache", action="store_true", help="delete all cached tilesets and samples, then exit")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--analyze", action="store_true", help="run the tiled-vs-normal benchmark, record system load, write a report, exit")
    p.add_argument("--duration", type=int, default=20, help="seconds per benchmark window (default 20)")
    p.add_argument("--rounds", type=int, default=2, help="benchmark rounds; each round runs both modes (default 2)")
    p.add_argument("--reference", help="original video for normal playback if --input is not playable in the browser")
    a = p.parse_args()

    try:
        root = cachedir.cache_root()
    except RuntimeError as e:
        print("error:", e, file=sys.stderr)
        return 2
    if a.clear_cache:
        print("deleted %.0f MB from %s" % (cachedir.clear_cache(root), root))
        return 0
    tilesets_root = os.path.join(root, "tilesets")
    os.makedirs(tilesets_root, exist_ok=True)
    latest = os.path.join(root, "latest.json")

    if a.no_tile:
        try:
            with open(latest) as f:
                last = json.load(f)
        except (OSError, ValueError):
            print("no tileset from a previous run; run without --no-tile first", file=sys.stderr)
            return 1
        name, src = last["name"], last["source"]
        out = os.path.join(tilesets_root, name)
    else:
        src = a.input or sample_path(root, a)
        name = cachedir.tileset_name(src, a.codec, a.cols, a.rows, a.preset)
        out = os.path.join(tilesets_root, name)
        print("tileset: %s" % out)
        rc = tiler.main([src, out, "--cols", str(a.cols), "--rows", str(a.rows), "--codec", a.codec,
                         "--preset", a.preset, "--bitrate-k", "16000", "--base-bitrate-k", "1500"] +
                        (["--no-throttle"] if a.no_throttle else ["--headroom", str(a.headroom)]) +
                        (["--rebuild"] if a.rebuild else []))
        if rc:
            return rc
        with open(latest, "w") as f:
            json.dump({"name": name, "source": os.path.abspath(src)}, f)

    shown_codec = a.codec
    if a.no_tile:                                           # the tileset being opened may not be the codec on the command line
        try:
            with open(os.path.join(out, "manifest.json")) as f:
                shown_codec = json.load(f)["codec"]
        except (OSError, ValueError, KeyError):
            pass
    if shown_codec == "hevc":
        print("note: HEVC needs a browser that can decode it (Chrome, Edge, Safari). On Firefox for Linux use: run_demo.py --codec h264")
    source_link = link_source(out, a.reference or src)
    server = serve.make_server(HERE, a.port, mounts={"/tilesets": tilesets_root})
    url = "http://127.0.0.1:%d/index.html?tileset=tilesets/%s&source=tilesets/%s/source.mp4" % (a.port, name, name)
    print("cache: %.0f MB in %s  (python3 demo/run_demo.py --clear-cache to delete)" % (cachedir.size_mb(root), root))
    if a.analyze:
        return analyze(a, server, url, out, source_link)
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
