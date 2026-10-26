"""Immutable public showcase: standard-library-only, no database or model code."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MESSAGE = "演示版只开放示例作品"


def handler_for(root):
    root = Path(root).resolve()
    sessions = json.loads((root / "sessions.json").read_text())
    by_id = {s["session_id"]: s for s in sessions}
    allowed_media = set(json.loads((root / "media.json").read_text()))

    class Handler(BaseHTTPRequestHandler):
        server_version = "Showcase"

        def log_message(self, *_): pass

        def json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD": self.wfile.write(data)

        def blocked(self):
            self.close_connection = True
            self.json({"error": {"code": "DEMO_READ_ONLY", "message": MESSAGE}}, 403)

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = blocked

        def do_HEAD(self): self.do_GET()

        def send_media(self, relative):
            if relative not in allowed_media: return self.missing()
            path = (root / "media" / relative).resolve()
            if not path.is_relative_to(root / "media") or not path.is_file(): return self.missing()
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            if requested:
                import re
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                if not match: return self.json({"message": "暂不支持此播放范围"}, 416)
                start = int(match[1]); end = min(int(match[2]) if match[2] else end, end)
                if start > end: return self.json({"message": "暂不支持此播放范围"}, 416)
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD": return
            with path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)

        def missing(self): self.json({"error": {"code": "NOT_FOUND", "message": "该示例暂未开放"}}, 404)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = unquote(parsed.path).rstrip("/")
            if path == "/api/health": return self.json({"status": "ok", "read_only": True})
            if path == "/api/sessions": return self.json(sessions)
            if path == "/api/tasks": return self.json([])
            parts = path.strip("/").split("/")
            if len(parts) < 3 or parts[:2] != ["api", "sessions"] or parts[2] not in by_id: return self.missing()
            sid = parts[2]; session = by_id[sid]
            if len(parts) == 3: return self.json(session)
            if len(parts) == 5 and parts[3] == "artifact" and parts[4] in session["artifacts"]: return self.json(session["artifacts"][parts[4]])
            if len(parts) == 6 and parts[3] == "media": return self.send_media(f"{parts[4]}/{sid}/{parts[5]}")
            if len(parts) == 4 and parts[3] == "export":
                artifact = session["artifacts"].get("post_production", {})
                version = parse_qs(parsed.query).get("version_id", [None])[0]
                if version:
                    artifact = next((v["artifact"] for v in session["artifact_versions"].get("post_production", []) if v["version_id"] == version), {})
                media = artifact.get("final_video", "")
                if media.startswith("/media/"): return self.send_media(media.removeprefix("/media/"))
            return self.missing()

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--port", type=int, default=8130)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.root)).serve_forever()
