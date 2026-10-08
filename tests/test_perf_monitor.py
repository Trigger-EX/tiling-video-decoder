import json
import os
import sys
import threading
import unittest
import urllib.request
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "demo"))
import perf_monitor as pm  # noqa: E402
import serve  # noqa: E402


def window(mode, rnd, start, secs=10, cpu_cores=None, **kw):
    w = dict(mode=mode, round=rnd, startEpochMs=start * 1000, endEpochMs=(start + secs) * 1000, seconds=secs, frames=600, fps=60,
             frameMs=dict(p50=16.6, p95=20, p99=25, max=40), hitches=3, videoFramesDropped=0, videoFramesTotal=300,
             meanSlots=0 if mode == "full" else 11, decodedMpxPerS=221 if mode == "full" else 119, sharpCoverage=1.0 if mode == "full" else 0.7,
             sharpArea=1.0 if mode == "full" else 0.95, tileStarts=0 if mode == "full" else 20, joinMsAvg=None if mode == "full" else 120, maxSyncMs=0 if mode == "full" else 15)
    w.update(kw)
    return w


class ReportTest(unittest.TestCase):
    def setUp(self):
        # monitor rows: browser uses 2.0 cores in the first (normal) window, 1.0 in the second (tiled)
        self.rows = [{"t": t, "sys_cpu": 0.5 if t < 110 else 0.3, "browser_cores": 2.0 if t < 110 else 1.0,
                      "browser_mb": 900.0, "watts": None, "gpu": None, "gpu_dec": None} for t in range(100, 120)]
        self.bench = {"tileset": dict(codec="hevc", width=3840, height=1920, cols=8, rows=4, fps=30), "display": dict(width=1280, height=720, dpr=1),
                      "settings": dict(durationS=10, budget=12, marginDeg=10, fovDeg=90, tourSpeed=0.5), "userAgent": "test",
                      "windows": [window("full", 1, 100), window("tiled", 1, 110)]}

    def test_window_stats_use_only_samples_inside_the_window(self):
        a = pm.window_stats(self.rows, 100_000, 110_000)
        b = pm.window_stats(self.rows, 110_000, 120_000)
        self.assertEqual((a["browser_cores"], b["browser_cores"]), (2.0, 1.0))
        self.assertAlmostEqual(a["sys_cpu"], 0.5); self.assertAlmostEqual(b["sys_cpu"], 0.3)
        self.assertIsNone(a["watts"])

    def test_report_compares_modes_and_marks_unavailable_metrics(self):
        r = pm.build_report(self.bench, self.rows, 4)
        self.assertIn("| Browser CPU (cores busy) | 2.00 | 1.00 | -50% |", r)
        self.assertIn("| System CPU | 50% | 30% | -40% |", r)
        self.assertIn("View drawn at full resolution (average) | 100.0% | 95.0% |", r)
        self.assertNotIn("Battery power", r)                    # nothing sampled -> row omitted, not faked
        self.assertIn("avg tile join time 120 ms", r)

    def test_report_says_so_when_the_viewer_had_to_back_off(self):
        self.assertNotIn("had to back off", pm.build_report(self.bench, self.rows, 4))
        self.bench["windows"][1].update(tileStalls=3, effectiveBudgetMin=7)
        r = pm.build_report(self.bench, self.rows, 4)
        self.assertIn("The viewer had to back off", r)
        self.assertIn("3 tile(s) failed to join in time", r)
        self.assertIn("dropped from 12 to 7", r)

    def test_report_includes_storage_when_given_paths(self):
        with tempfile.TemporaryDirectory() as d:
            ts = os.path.join(d, "ts"); os.makedirs(ts)
            open(os.path.join(ts, "a.bin"), "wb").write(b"x" * 3000)
            src = os.path.join(d, "src.mp4"); open(src, "wb").write(b"y" * 1000)
            self.assertIn("(3.00x)", pm.build_report(self.bench, self.rows, 4, ts, src))


class ServerTest(unittest.TestCase):
    def test_post_perf_stores_results_and_signals(self):
        with tempfile.TemporaryDirectory() as d:
            srv = serve.make_server(d, 0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                url = "http://127.0.0.1:%d/__perf" % srv.server_address[1]
                req = urllib.request.Request(url, data=json.dumps({"windows": []}).encode(), method="POST")
                self.assertEqual(urllib.request.urlopen(req).status, 204)
                self.assertTrue(srv.perf_event.wait(2))
                path, data = srv.perf_results[-1]
                self.assertEqual(data, {"windows": []}); self.assertTrue(os.path.exists(path))
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(urllib.request.Request(url, data=b"not json", method="POST"))
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(urllib.request.Request(url.replace("__perf", "other"), data=b"{}", method="POST"))
            finally:
                srv.shutdown(); srv.server_close()


class ReportRobustnessTest(ReportTest):
    def test_report_survives_null_numbers_from_a_hidden_tab(self):
        # A background tab stops requestAnimationFrame: the page then sends nulls for everything it could not measure.
        self.bench["windows"][1].update(sharpCoverage=None, sharpArea=None, frameMs=dict(p50=None, p95=None, p99=None, max=None), joinMsAvg=None)
        r = pm.build_report(self.bench, self.rows, 4)
        self.assertIn("| Browser CPU (cores busy) | 2.00 | 1.00 | -50% |", r)    # the OS-side numbers are still reported
        self.assertIn("n/a", r)

    def test_report_file_names_come_from_the_file_name_not_the_folder(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "demo"))
        import run_demo
        self.assertEqual(run_demo.report_paths("/home/me/bench-runs/perf/bench-1700000000.json"),
                         ("/home/me/bench-runs/perf/report-1700000000.md", "/home/me/bench-runs/perf/samples-1700000000.json"))


class BrowserProcessMatchTest(unittest.TestCase):
    def test_firefox_helpers_with_renamed_process_names_are_counted_under_psutil(self):
        import types
        procs = [
            {"pid": 1, "name": "firefox", "exe": "/usr/lib/firefox/firefox", "cmdline": ["/usr/lib/firefox/firefox"]},
            {"pid": 2, "name": "Isolated Web Co", "exe": "/usr/lib/firefox/firefox", "cmdline": ["/usr/lib/firefox/firefox", "-contentproc"]},
            {"pid": 3, "name": "RDD Process", "exe": None, "cmdline": ["/usr/lib/firefox/firefox", "-contentproc"]},
            {"pid": 4, "name": "python3", "exe": "/usr/bin/python3", "cmdline": ["python3", "x.py"]},
        ]
        fake = types.ModuleType("psutil")
        fake.process_iter = lambda attrs: [types.SimpleNamespace(info=p) for p in procs]
        old = sys.modules.get("psutil")
        sys.modules["psutil"] = fake
        try:
            self.assertEqual(sorted(pm.browser_pids()), [1, 2, 3])
        finally:
            if old is None:
                del sys.modules["psutil"]
            else:
                sys.modules["psutil"] = old


if __name__ == "__main__":
    unittest.main()
