import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import tiler  # noqa: E402

ARGS = ["--cols", "4", "--rows", "2", "--pad", "16", "--base-width", "320", "--bitrate-k", "3000",
        "--base-bitrate-k", "300", "--preset", "fast", "--quiet"]


def make_video(path, seconds=2, freq=440):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x640:rate=30:duration=%d" % seconds,
                    "-f", "lavfi", "-i", "sine=frequency=%d:duration=%d" % (freq, seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", path], check=True)


def run(src, out, *extra):
    t = time.time()
    assert tiler.main([src, out] + ARGS + list(extra)) == 0
    return time.time() - t


def mtimes(out):
    return {f: os.stat(os.path.join(out, f)).st_mtime_ns for f in ["base.mp4", "manifest.json"] +
            ["tiles/" + n for n in sorted(os.listdir(os.path.join(out, "tiles")))]}


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "v.mp4")
        make_video(self.src)
        self.out = os.path.join(self.tmp.name, "ts")

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_reuses_everything(self):
        run(self.src, self.out)
        before = mtimes(self.out)
        again = run(self.src, self.out)
        self.assertEqual(mtimes(self.out), before)               # not one file was rewritten
        self.assertLess(again, 3)                                # and it returned immediately

    def test_interrupted_build_resumes_without_redoing_finished_rows(self):
        run(self.src, self.out)
        # Simulate a crash after row 0 and the base layer: row 1 missing, state says incomplete, a stray .part left.
        for c in range(4):
            os.remove(os.path.join(self.out, "tiles/t_1_%d.mp4" % c))
        os.remove(os.path.join(self.out, "manifest.json"))
        st = os.path.join(self.out, tiler.STATE_FILE)
        state = json.load(open(st)); state["complete"] = False; json.dump(state, open(st, "w"))
        open(os.path.join(self.out, "tiles/t_1_0.mp4.part"), "wb").write(b"junk")
        row0 = {f: os.stat(os.path.join(self.out, f)).st_mtime_ns for f in ["base.mp4", "tiles/t_0_0.mp4", "tiles/t_0_3.mp4"]}
        run(self.src, self.out)
        self.assertEqual({f: os.stat(os.path.join(self.out, f)).st_mtime_ns for f in row0}, row0)   # finished work untouched
        self.assertTrue(all(os.path.getsize(os.path.join(self.out, "tiles/t_1_%d.mp4" % c)) > 1000 for c in range(4)))
        self.assertFalse([f for f in os.listdir(os.path.join(self.out, "tiles")) if f.endswith(".part")])
        self.assertTrue(json.load(open(st))["complete"])

    def test_changed_settings_or_changed_video_rebuild(self):
        run(self.src, self.out)
        before = mtimes(self.out)
        run(self.src, self.out, "--gop-seconds", "0.5")                    # different settings -> new tiles
        self.assertNotEqual(mtimes(self.out)["tiles/t_0_0.mp4"], before["tiles/t_0_0.mp4"])
        self.assertEqual(json.load(open(os.path.join(self.out, "manifest.json")))["gopFrames"], 15)
        mid = mtimes(self.out)
        time.sleep(0.01)
        make_video(self.src, seconds=2, freq=880)                          # same path, new contents
        run(self.src, self.out, "--gop-seconds", "0.5")
        self.assertNotEqual(mtimes(self.out)["tiles/t_0_0.mp4"], mid["tiles/t_0_0.mp4"])

    def test_rebuild_flag_forces_a_fresh_build(self):
        run(self.src, self.out)
        before = mtimes(self.out)
        run(self.src, self.out, "--rebuild")
        self.assertNotEqual(mtimes(self.out)["tiles/t_0_0.mp4"], before["tiles/t_0_0.mp4"])

    def test_foreign_files_in_the_output_directory_are_never_deleted(self):
        os.makedirs(self.out)
        keep = os.path.join(self.out, "notes.txt"); open(keep, "w").write("mine")
        run(self.src, self.out)
        run(self.src, self.out, "--gop-seconds", "0.5")                    # triggers the clean-up path
        self.assertEqual(open(keep).read(), "mine")

    def test_unreadable_input_fails_cleanly_and_leaves_nothing_behind(self):
        bad = os.path.join(self.tmp.name, "bad.mp4")
        with open(bad, "wb") as f:
            f.write(b"not a video")
        out = os.path.join(self.tmp.name, "bad_out")
        self.assertEqual(tiler.main([bad, out] + ARGS), 1)
        self.assertFalse(os.path.exists(os.path.join(out, "manifest.json")))


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "v.mp4")
        make_video(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_output_folder_names_with_glob_characters_are_cleaned_correctly(self):
        out = os.path.join(self.tmp.name, "my video [360] *")
        run(self.src, out)
        before = mtimes(out)["tiles/t_0_0.mp4"]
        run(self.src, out, "--gop-seconds", "0.5")                         # new settings: old tiles must really be replaced
        self.assertNotEqual(mtimes(out)["tiles/t_0_0.mp4"], before)
        with open(os.path.join(out, "manifest.json")) as f:
            self.assertEqual(json.load(f)["gopFrames"], 15)
        self.assertFalse([f for f in os.listdir(os.path.join(out, "tiles")) if f.endswith(".part")])

    def test_state_and_manifest_are_written_atomically(self):
        target = os.path.join(self.tmp.name, "s.json")
        tiler._write_json_atomic(target, {"a": 1})
        tiler._write_json_atomic(target, {"a": 2}, indent=2)
        with open(target) as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["s.json", "v.mp4"])      # no .part left over

    def test_frame_rate_falls_back_when_r_frame_rate_is_zero(self):
        real = tiler.run
        try:
            def fake(cmd, payload):
                tiler.run = lambda c: json.dumps({"streams": [payload], "format": {"duration": "4.0"}})
                return tiler.probe("x.mp4")
            ok = fake(None, {"width": 640, "height": 320, "r_frame_rate": "0/0", "avg_frame_rate": "30000/1001"})
            self.assertAlmostEqual(ok[2], 29.97, places=2)
            with self.assertRaises(ValueError):
                fake(None, {"width": 640, "height": 320, "r_frame_rate": "0/0", "avg_frame_rate": "0/0"})
        finally:
            tiler.run = real

    def test_missing_ffmpeg_is_a_clear_error_not_a_traceback(self):
        import io
        from contextlib import redirect_stderr
        old = os.environ["PATH"]
        os.environ["PATH"] = self.tmp.name                                 # a folder with no programs in it
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = tiler.main([self.src, os.path.join(self.tmp.name, "o")] + ARGS)
        finally:
            os.environ["PATH"] = old
        self.assertEqual(rc, 1)
        self.assertIn("not found", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
