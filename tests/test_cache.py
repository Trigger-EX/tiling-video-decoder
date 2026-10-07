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


if __name__ == "__main__":
    unittest.main()
