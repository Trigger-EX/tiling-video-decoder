"""Tiny static file server with HTTP Range support (browsers need it to seek in <video>)."""
import http.server
import json
import os
import re
import sys
import threading
import time


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_POST(self):
        """Receives benchmark results from the viewer (POST /__perf) and stores them under perf/."""
        if self.path != "/__perf":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 2_000_000:
            return self.send_error(413)
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            return self.send_error(400)
        out_dir = os.path.join(self.directory, "perf")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "bench-%d.json" % int(time.time()))
        with open(path, "w") as f:
            json.dump(data, f, indent=1)
        self.server.perf_results.append((path, data))
        self.server.perf_event.set()
        self.send_response(204)
        self.end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)$", rng)
        size = os.path.getsize(path)
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()
        if m.group(1) == "":
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        if start >= size or start > end:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self._remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(self._remaining))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        self._remaining = None
        try:
            if remaining is None:
                return super().copyfile(source, outputfile)
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser cancelled the request (it does this when seeking)


def make_server(directory, port):
    handler = lambda *a, **k: RangeHandler(*a, directory=directory, **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.perf_results = []              # (path, parsed json) of every benchmark the viewer posted
    server.perf_event = threading.Event()
    return server


if __name__ == "__main__":
    make_server(sys.argv[1], int(sys.argv[2])).serve_forever()
