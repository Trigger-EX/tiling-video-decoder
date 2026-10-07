"""Records what the machine is doing while the viewer's benchmark runs, and builds the comparison report.

The viewer measures what a web page can see (frame pacing, dropped video frames, how many pixels it asks the
decoders for). It cannot see CPU, memory, power or the GPU video engine, so this samples them from the OS twice a
second and the report lines the samples up with each benchmark window by wall-clock time.

    CPU     system-wide, plus the summed CPU of the browser's processes (Linux /proc or psutil)
    memory  summed resident memory of the browser's processes
    power   battery discharge rate in watts (Linux; only meaningful while unplugged)
    GPU     NVIDIA (nvidia-smi) or AMD (sysfs) utilisation when available; Intel is not read

Only the system-wide CPU path is portable; the rest is best effort and reported as "n/a" when unavailable.
"""
import glob
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resources  # noqa: E402

BROWSERS = ("firefox", "headless_shell", "chrome", "chromium", "msedge", "brave", "opera", "vivaldi", "epiphany", "safari", "webkit")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def browser_pids():
    """PIDs of browser processes (main + helpers share the executable name)."""
    pids = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "exe"]):
            n = (p.info.get("name") or "").lower()
            if any(b in n for b in BROWSERS):
                pids.append(p.info["pid"])
        return pids
    except ImportError:
        pass
    for d in glob.glob("/proc/[0-9]*"):
        cmd = _read(d + "/cmdline")
        if cmd and any(b in os.path.basename(cmd.split("\0")[0]).lower() for b in BROWSERS):
            pids.append(int(os.path.basename(d)))
    return pids


def process_cpu_seconds_and_rss(pids):
    """(total CPU seconds consumed so far, total resident MB) over pids; Linux /proc or psutil."""
    cpu, rss = 0.0, 0.0
    try:
        import psutil
        for pid in pids:
            try:
                p = psutil.Process(pid)
                t = p.cpu_times()
                cpu += t.user + t.system
                rss += p.memory_info().rss / 2**20
            except psutil.Error:
                pass
        return cpu, rss
    except ImportError:
        pass
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for pid in pids:
        st = _read("/proc/%d/stat" % pid)
        if not st:
            continue
        f = st[st.rindex(")") + 2:].split()          # fields after the command name
        cpu += (int(f[11]) + int(f[12])) / tick
        sm = _read("/proc/%d/statm" % pid)
        if sm:
            rss += int(sm.split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    return cpu, rss


def battery_watts():
    """Discharge power in W, or None when plugged in / no battery / unreadable."""
    for b in glob.glob("/sys/class/power_supply/BAT*"):
        if (_read(b + "/status") or "").lower() != "discharging":
            continue
        p = _read(b + "/power_now")
        if p and int(p) > 0:
            return int(p) / 1e6
        i, v = _read(b + "/current_now"), _read(b + "/voltage_now")
        if i and v and int(i) > 0:
            return int(i) * int(v) / 1e12
    return None


def gpu_util():
    """GPU busy percent (and video-decoder percent when the driver reports it), or (None, None)."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.decoder", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=2).stdout.split("\n")[0].split(",")
            num = lambda x: float(x) if x.strip().replace(".", "").isdigit() else None  # noqa: E731
            return num(out[0]), num(out[1]) if len(out) > 1 else None
        except (subprocess.SubprocessError, OSError, IndexError):
            pass
    for f in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        v = _read(f)
        if v and v.isdigit():
            return float(v), None
    return None, None


class Monitor:
    """Samples the machine every `interval` seconds on a background thread."""

    def __init__(self, interval=0.5):
        self.interval, self.rows, self._stop = interval, [], threading.Event()
        self.cores = os.cpu_count() or 1
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(2)

    def _run(self):
        sampler = resources.SystemSampler()
        sampler.cpu_busy()
        prev = None
        gpu_at = 0.0
        gpu = (None, None)
        while not self._stop.wait(self.interval):
            now = time.time()
            pids = browser_pids()
            cpu_s, rss = process_cpu_seconds_and_rss(pids)
            browser_cores = None
            if prev and now > prev[0]:
                browser_cores = max(0.0, (cpu_s - prev[1]) / (now - prev[0])) if cpu_s >= prev[1] else None
            prev = (now, cpu_s)
            if now - gpu_at > 1.0:
                gpu, gpu_at = gpu_util(), now
            self.rows.append({"t": now, "sys_cpu": sampler.cpu_busy(), "browser_cores": browser_cores,
                              "browser_mb": rss or None, "watts": battery_watts(), "gpu": gpu[0], "gpu_dec": gpu[1]})


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def window_stats(rows, start_ms, end_ms):
    """Mean of each sampled metric over the benchmark window [start_ms, end_ms] (epoch ms)."""
    sel = [r for r in rows if start_ms / 1000 <= r["t"] < end_ms / 1000]
    return {k: _mean(r[k] for r in sel) for k in ("sys_cpu", "browser_cores", "browser_mb", "watts", "gpu", "gpu_dec")} | {"samples": len(sel)}


def _fmt(v, d=1, suffix=""):
    return "n/a" if v is None else ("%.*f%s" % (d, v, suffix))


def _delta(a, b):
    if a is None or b is None or a == 0:
        return ""
    return "%+.0f%%" % (100 * (b - a) / a)


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):                  # e.g. source.mp4 links to the original video
                total += os.path.getsize(fp)
    return total


def build_report(bench, rows, cores, tileset_dir=None, source_path=None):
    """Markdown comparing normal playback with tiled playback from the viewer's results + monitor rows."""
    ws = bench["windows"]
    per = {"full": [], "tiled": []}
    for w in ws:
        sysm = window_stats(rows, w["startEpochMs"], w["endEpochMs"])
        per[w["mode"]].append((w, sysm))

    def agg(mode, fn):
        return _mean(fn(w, m) for w, m in per[mode])

    metrics = [
        ("Browser CPU (cores busy)", lambda w, m: m["browser_cores"], 2, ""),
        ("System CPU", lambda w, m: None if m["sys_cpu"] is None else 100 * m["sys_cpu"], 0, "%"),
        ("Browser memory", lambda w, m: m["browser_mb"], 0, " MB"),
        ("Battery power (unplugged only)", lambda w, m: m["watts"], 1, " W"),
        ("GPU busy", lambda w, m: m["gpu"], 0, "%"),
        ("GPU video decoder busy", lambda w, m: m["gpu_dec"], 0, "%"),
        ("Frame time p95", lambda w, m: w["frameMs"]["p95"], 1, " ms"),
        ("Frame time p99", lambda w, m: w["frameMs"]["p99"], 1, " ms"),
        ("Hitches (frames > 33 ms) per minute", lambda w, m: 60 * w["hitches"] / w["seconds"], 1, ""),
        ("Video frames dropped (% of decoded)", lambda w, m: 100 * w["videoFramesDropped"] / w["videoFramesTotal"] if w["videoFramesTotal"] else None, 1, "%"),
        ("Decoded pixels (modeled)", lambda w, m: w["decodedMpxPerS"], 0, " Mpx/s"),
        ("Decoder instances (mean)", lambda w, m: 1 if w["mode"] == "full" else w["meanSlots"] + 1, 1, ""),
        ("View drawn at full resolution (average)", lambda w, m: 100 * w.get("sharpArea", w["sharpCoverage"]), 1, "%"),
        ("Time with the whole view at full resolution", lambda w, m: 100 * w["sharpCoverage"], 1, "%"),
    ]
    t = bench["tileset"]
    lines = ["# Tiled vs normal playback", "",
             "Tileset: %s, %dx%d source, %dx%d grid, %d fps. Display %dx%d @%gx. Windows: %d x %ds, alternating order (%s)." % (
                 t["codec"].upper(), t["width"], t["height"], t["cols"], t["rows"], t["fps"], bench["display"]["width"],
                 bench["display"]["height"], bench["display"]["dpr"], len(ws), bench["settings"]["durationS"],
                 ", ".join("%s r%d" % (w["mode"], w["round"]) for w in ws)),
             "Decoder budget %d, prefetch margin %d deg, vertical FOV %d deg, scripted head path at %gx speed." % (
                 bench["settings"]["budget"], bench["settings"]["marginDeg"], bench["settings"]["fovDeg"], bench["settings"].get("tourSpeed", 1)),
             "Browser: `%s`" % bench["userAgent"], "",
             "| Metric | Normal playback | Tiled | Change |", "|---|---|---|---|"]
    for name, fn, d, suf in metrics:
        a, b = agg("full", fn), agg("tiled", fn)
        if a is None and b is None:
            continue
        lines.append("| %s | %s | %s | %s |" % (name, _fmt(a, d, suf), _fmt(b, d, suf), _delta(a, b)))
    jt = agg("tiled", lambda w, m: w["joinMsAvg"])
    lines += ["", "Tiled only: avg tile join time %s ms, %s tile starts per minute, worst sync error vs base clock %s ms." % (
        _fmt(jt, 0), _fmt(agg("tiled", lambda w, m: 60 * w["tileStarts"] / w["seconds"]), 0), _fmt(max([w["maxSyncMs"] for w, _ in per["tiled"]] or [None]), 0))]
    if len(per["full"]) > 1 and len(per["tiled"]) > 1:
        sp = lambda mode: [m["browser_cores"] for _, m in per[mode] if m["browser_cores"] is not None]  # noqa: E731
        if len(sp("full")) > 1 and len(sp("tiled")) > 1:
            lines.append("Run-to-run spread of browser CPU (cores): normal %.2f-%.2f, tiled %.2f-%.2f; treat differences smaller than this as noise." % (
                min(sp("full")), max(sp("full")), min(sp("tiled")), max(sp("tiled"))))
    if tileset_dir and os.path.isdir(tileset_dir):
        ts = dir_size(tileset_dir)
        line = "Tileset on disk: %.1f MB" % (ts / 2**20)
        if source_path and os.path.exists(source_path):
            src = os.path.getsize(os.path.realpath(source_path))
            line += " vs %.1f MB for the original video (%.2fx)" % (src / 2**20, ts / src)
        lines += ["", "## Storage", "", line + ". Tiles carry padding for seamless edges and the base layer is extra, so a tileset is "
                  "larger than one video at the same quality; HEVC narrows that gap."]
    lines += ["", "## How to read this", "",
              "* Normal playback = the original video in one decoder, drawn on the same sphere with the same renderer and head path.",
              "* CPU, memory, power and GPU come from the OS, not the page. Close other programs, keep the tab in the foreground and plug in or unplug consistently. Power is only reported on battery.",
              "* Modeled decoded pixels are computed from the active tiles, not measured in the hardware decoder.",
              "* \"View drawn at full resolution\" is the average share of the screen covered by decoded tiles; the rest showed the low-resolution base layer. Lower CPU is only a win if this stays high. Normal playback is 100% by definition.",
              "* Dropped frames are a percentage of the frames each mode decoded, because tiled mode runs many more decoders.",
              "* A laptop's decoder handles a 4K video easily, so a small difference here is expected; the case for tiling is sources the hardware cannot decode whole (the Oculus Go stops at 4096x2048)."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # python3 tools/perf_monitor.py bench.json samples.json [tileset_dir] [source]  -> report on stdout
    bench = json.load(open(sys.argv[1]))
    rows = json.load(open(sys.argv[2]))
    print(build_report(bench, rows, os.cpu_count() or 1, *(sys.argv[3:5])))
