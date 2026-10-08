import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "demo"))
import cachedir  # noqa: E402
import serve  # noqa: E402


class CacheDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = os.path.join(self.tmp.name, "My Trip (360).mp4")
        with open(self.video, "wb") as f:
            f.write(b"x" * 100)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("TILING_CACHE_DIR", None)

    def test_name_is_stable_and_safe_for_the_same_file_and_settings(self):
        a = cachedir.tileset_name(self.video, "hevc", 8, 4, "fast")
        self.assertEqual(a, cachedir.tileset_name(self.video, "hevc", 8, 4, "fast"))
        self.assertRegex(a, r"^My_Trip__360_-hevc-8x4-fast-[0-9a-f]{8}$")

    def test_name_changes_with_settings_and_with_the_file(self):
        base = cachedir.tileset_name(self.video, "hevc", 8, 4, "fast")
        self.assertNotEqual(base, cachedir.tileset_name(self.video, "h264", 8, 4, "fast"))
        self.assertNotEqual(base, cachedir.tileset_name(self.video, "hevc", 6, 3, "fast"))
        time.sleep(0.01)
        with open(self.video, "ab") as f:
            f.write(b"y")                                         # edited video -> new name, stale tiles are not reused
        self.assertNotEqual(base, cachedir.tileset_name(self.video, "hevc", 8, 4, "fast"))

    def test_cache_root_honours_override_and_marks_itself(self):
        os.environ["TILING_CACHE_DIR"] = os.path.join(self.tmp.name, "c")
        root = cachedir.cache_root()
        self.assertTrue(os.path.exists(os.path.join(root, cachedir.MARKER)))

    def test_clear_cache_deletes_contents_but_refuses_foreign_directories(self):
        os.environ["TILING_CACHE_DIR"] = os.path.join(self.tmp.name, "c")
        root = cachedir.cache_root()
        os.makedirs(os.path.join(root, "tilesets", "x"))
        with open(os.path.join(root, "tilesets", "x", "a.bin"), "wb") as f:
            f.write(b"z" * 2048)
        cachedir.clear_cache(root)
        self.assertEqual(os.listdir(root), [cachedir.MARKER])
        precious = os.path.join(self.tmp.name, "documents")
        os.makedirs(precious)
        with self.assertRaises(RuntimeError):
            cachedir.clear_cache(precious)
        self.assertTrue(os.path.isdir(precious))


class MountTest(unittest.TestCase):
    def test_mounted_directory_is_served_with_ranges_and_cannot_be_escaped(self):
        with tempfile.TemporaryDirectory() as web, tempfile.TemporaryDirectory() as cache:
            os.makedirs(os.path.join(cache, "ts"))
            with open(os.path.join(cache, "ts", "v.mp4"), "wb") as f:
                f.write(bytes(range(200)))
            with open(os.path.join(web, "secret.txt"), "w") as f:
                f.write("inside web root")
            srv = serve.make_server(web, 0, mounts={"/tilesets": cache})
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            try:
                self.assertEqual(urllib.request.urlopen(base + "/tilesets/ts/v.mp4").read(), bytes(range(200)))
                req = urllib.request.Request(base + "/tilesets/ts/v.mp4", headers={"Range": "bytes=10-19"})
                r = urllib.request.urlopen(req)
                self.assertEqual((r.status, r.read()), (206, bytes(range(10, 20))))
                # climbing out of the mount must never reach the web root or anything else
                for evil in ("/tilesets/../secret.txt", "/tilesets/%2e%2e/secret.txt", "/tilesets/ts/../../secret.txt"):
                    with self.assertRaises(urllib.error.HTTPError, msg=evil):
                        urllib.request.urlopen(base + evil)
                self.assertEqual(urllib.request.urlopen(base + "/secret.txt").read(), b"inside web root")   # normal root still works
            finally:
                srv.shutdown(); srv.server_close()


class RangeCapTest(unittest.TestCase):
    """A range request must never be answered with the whole rest of a big file: that response stays open as long as
    the <video> exists and, with ~6 connections per server, a dozen tile videos would starve every other request."""

    def serve(self, web, limit):
        self.old, serve.MAX_CHUNK = serve.MAX_CHUNK, limit
        srv = serve.make_server(web, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, "http://127.0.0.1:%d/v.mp4" % srv.server_address[1]

    def test_open_ended_range_is_cut_to_the_chunk_limit_and_reassembles(self):
        with tempfile.TemporaryDirectory() as web:
            data = bytes(i % 251 for i in range(5000))
            with open(os.path.join(web, "v.mp4"), "wb") as f:
                f.write(data)
            srv, url = self.serve(web, 2000)
            try:
                got, pos, requests = b"", 0, 0
                while pos < len(data):
                    r = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=%d-" % pos}))
                    body = r.read()
                    span, total = r.headers["Content-Range"].split(" ")[1].split("/")
                    self.assertEqual((r.status, int(total)), (206, 5000))
                    self.assertEqual(int(span.split("-")[0]), pos)
                    self.assertLessEqual(len(body), 2000)
                    self.assertEqual(int(r.headers["Content-Length"]), len(body))
                    got, pos, requests = got + body, pos + len(body), requests + 1
                self.assertEqual(got, data)
                self.assertEqual(requests, 3)                                  # 2000 + 2000 + 1000
                r = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=10-19"}))
                self.assertEqual(r.read(), data[10:20])                         # small explicit ranges are untouched
                self.assertEqual(urllib.request.urlopen(url).read(), data)      # a plain GET still returns the whole file
            finally:
                serve.MAX_CHUNK = self.old
                srv.shutdown(); srv.server_close()

    def test_limit_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as web:
            with open(os.path.join(web, "v.mp4"), "wb") as f:
                f.write(b"x" * 3000)
            srv, url = self.serve(web, 0)
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-"}))
                self.assertEqual(len(r.read()), 3000)
            finally:
                serve.MAX_CHUNK = self.old
                srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
