import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import tiler  # noqa: E402


def ffmpeg(*a):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)


def keyframe_times(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time,flags", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(round(float(l.split(",")[0]), 3) for l in out if "K" in l.split(",")[1])


class PlanTest(unittest.TestCase):
    def test_rejects_indivisible_and_unaligned(self):
        with self.assertRaises(ValueError):
            tiler.plan(1000, 500, 3, 2, 16)
        with self.assertRaises(ValueError):
            tiler.plan(1280, 640, 8, 4, 4)      # 160+8 not a multiple of 16
        self.assertEqual(tiler.plan(1280, 640, 8, 4, 16), (160, 160, 192, 192))


class TilerTest(unittest.TestCase):
    CODEC = "h264"
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = os.path.join(cls.tmp.name, "src.mp4")
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x640:rate=30:duration=4",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.src)
        cls.out = os.path.join(cls.tmp.name, "ts")
        rc = tiler.main([cls.src, cls.out, "--cols", "4", "--rows", "2", "--pad", "16", "--codec", cls.CODEC, "--preset", "ultrafast",
                         "--base-width", "320", "--bitrate-k", "4000", "--base-bitrate-k", "300"])
        assert rc == 0
        with open(os.path.join(cls.out, "manifest.json")) as f:
            cls.m = json.load(f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_manifest(self):
        g = self.m["grid"]
        self.assertEqual((g["cols"], g["rows"], g["tileWidth"], g["tileHeight"], g["pad"]), (4, 2, 320, 320, 16))
        self.assertEqual(len(self.m["tiles"]), 8)
        self.assertEqual(self.m["gopFrames"], 30)

    def test_tile_size_and_no_audio(self):
        for t in self.m["tiles"]:
            path = os.path.join(self.out, t["file"])
            w, h, _, _ = tiler.probe(path)
            self.assertEqual((w, h), (352, 352))
            self.assertFalse(tiler.has_audio(path))

    def test_base_has_audio(self):
        self.assertTrue(tiler.has_audio(os.path.join(self.out, "base.mp4")))

    def test_keyframes_aligned_across_streams(self):
        expected = [0.0, 1.0, 2.0, 3.0]
        self.assertEqual(keyframe_times(os.path.join(self.out, "base.mp4")), expected)
        for t in self.m["tiles"]:
            self.assertEqual(keyframe_times(os.path.join(self.out, t["file"])), expected, t["file"])

    def test_tile_content_matches_source_with_wrapped_padding(self):
        # Tile (row 0, col 0) with pad 16: left pad must hold the source's rightmost 16 columns.
        raw_t = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.join(self.out, "tiles/t_0_0.mp4"),
                                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                               capture_output=True, check=True).stdout
        raw_s = subprocess.run(["ffmpeg", "-v", "error", "-i", self.src, "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                               capture_output=True, check=True).stdout
        tw = 352
        sw = 1280

        def mad(tile_x0, tile_y0, src_x0, src_y0, w, h):
            tot = 0
            for y in range(h):
                for x in range(w):
                    tot += abs(raw_t[(tile_y0 + y) * tw + tile_x0 + x] - raw_s[(src_y0 + y) * sw + src_x0 + x])
            return tot / (w * h)

        self.assertLess(mad(16, 16, 0, 0, 320, 300), 6)            # interior = source top-left
        self.assertLess(mad(0, 16, sw - 16, 0, 16, 300), 8)        # left pad = wrapped right edge


class Vp9TilerTest(TilerTest):
    """Same checks for the VP9 option used by the browser demo."""
    CODEC = "vp9"


if __name__ == "__main__":
    unittest.main()
