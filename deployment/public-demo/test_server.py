"""Run with python -m unittest discover -s deployment/public-demo -p 'test_*.py'."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location("showcase", Path(__file__).with_name("server.py"))
showcase = importlib.util.module_from_spec(spec)
spec.loader.exec_module(showcase)


class ReadOnlyServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        session = {"session_id": "approved", "artifacts": {"script_generation": {"title": "雨夜归途"}, "post_production": {"final_video": "/media/video/approved/final.mp4"}}, "artifact_versions": {}}
        (self.root / "sessions.json").write_text(json.dumps([session]))
        (self.root / "media.json").write_text(json.dumps(["video/approved/final.mp4"]))
        (self.root / "media/video/approved").mkdir(parents=True)
        (self.root / "media/video/approved/final.mp4").write_bytes(b"0123456789")
        (self.root / "private.txt").write_text("must not be public")
        (self.root / "media/video/approved/unlisted.mp4").write_bytes(b"private")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), showcase.handler_for(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        try:
            with urlopen(Request(self.base + path, method=method, headers=headers or {}), timeout=3) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_anonymous_reads_and_no_accounts(self):
        self.assertEqual(self.request("/api/sessions")[0], 200)
        self.assertEqual(json.loads(self.request("/api/sessions/approved/artifact/script_generation")[1])["title"], "雨夜归途")
        for path in ("/api/auth/me", "/api/settings", "/api/sessions/private", "/api/sessions/approved/stream", "/api/models"):
            with self.subTest(path=path): self.assertEqual(self.request(path)[0], 404)

    def test_every_write_rejected_and_snapshot_unchanged(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            for path in ("/api/sessions", "/api/sessions/approved", "/api/sessions/approved/execute/script_generation", "/api/sessions/approved/resume", "/api/sessions/approved/intervene", "/api/sessions/approved/checks/script_generation", "/api/auth/login"):
                with self.subTest(method=method, path=path):
                    status, body, _ = self.request(path, method)
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(body)["error"]["message"], showcase.MESSAGE)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_media_allowlist_traversal_and_ranges(self):
        status, data, headers = self.request("/api/sessions/approved/media/video/final.mp4", headers={"Range": "bytes=2-5"})
        self.assertEqual((status, data), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.request("/api/sessions/approved/export")[1], b"0123456789")
        for path in ("/api/sessions/approved/media/video/unlisted.mp4", "/api/sessions/approved/media/video/%2e%2e%2fprivate.txt", "/api/sessions/approved/media/../../private.txt", "/api/sessions/approved/export?version_id=private"):
            with self.subTest(path=path): self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/sessions/approved/media/video/final.mp4", "HEAD")[1], b"")

    def test_reload_from_disk_preserves_snapshot(self):
        first = self.request("/api/sessions")[1]
        second = ThreadingHTTPServer(("127.0.0.1", 0), showcase.handler_for(self.root))
        worker = threading.Thread(target=second.serve_forever, daemon=True); worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{second.server_port}/api/sessions") as response: self.assertEqual(first, response.read())
        finally:
            second.shutdown(); second.server_close(); worker.join()

    def test_public_media_routes_ranges_and_export_match(self):
        public = "/media/video/approved/final.mp4"
        self.assertEqual(self.request(public)[1], self.request("/api/sessions/approved/export")[1])
        for value, expected, bounds in (("bytes=0-", b"0123456789", "0-9"), ("bytes=7-99", b"789", "7-9"), ("bytes=-3", b"789", "7-9"), ("bytes=2-5", b"2345", "2-5")):
            with self.subTest(value=value):
                status, data, headers = self.request(public, headers={"Range": value})
                self.assertEqual((status, data), (206, expected))
                self.assertEqual(headers["Content-Range"], f"bytes {bounds}/10")
                self.assertEqual(int(headers["Content-Length"]), len(expected))
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(headers["Content-Type"], "video/mp4")
        for value in ("bytes=10-", "bytes=6-2", "bytes=-0", "bytes=-", "bytes=0-1,3-4", "invalid"):
            with self.subTest(value=value):
                status, _, headers = self.request(public, headers={"Range": value})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], "bytes */10")
        status, data, headers = self.request(public, "HEAD", {"Range": "bytes=2-5"})
        self.assertEqual((status, data, headers["Content-Length"]), (206, b"", "4"))
        for path in ("/media/video/approved/unlisted.mp4", "/media/video/approved/%2e%2e%2fprivate.txt", "/media/../private.txt", "/media/video/private/final.mp4"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request(public, "POST")[0], 403)


if __name__ == "__main__": unittest.main()
