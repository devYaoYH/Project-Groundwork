"""The operator checker consumes real HTTP chunks and never starts a launch."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from deploy.verify_live_logs import cookie_session, frames, verify_launch


def test_sse_parser_retains_comments_and_multiline_json():
    values = list(frames([b": connected", b"retry: 2000", b"",
                          b"id: 4", b"event: runner.log", b'data: {"id":', b"data: 4}", b""]))
    assert values[0]["comments"] == ["connected"] and values[0]["retry"] == "2000"
    assert values[1]["id"] == "4" and json.loads(values[1]["data"]) == {"id": 4}


def test_cookie_file_requires_private_permissions(tmp_path):
    path = tmp_path / "operator.json"
    path.write_text('{"platform_session":"explicit-operator-value"}')
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="private"):
        cookie_session(path)
    path.chmod(0o600)
    with cookie_session(path) as session:
        assert session.cookies.get("platform_session") == "explicit-operator-value"


def test_wire_checker_reads_early_frame_and_both_resume_contracts():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            # A mutation in this checker would never be accepted by this server.
            assert urlsplit(self.path).path == "/api/launches/existing/events"
            cookie = self.headers.get("Cookie", "")
            status = 404 if "Bob" in cookie else (200 if "Alice" in cookie else 401)
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.end_headers()
            if status != 200:
                return
            self.wfile.write(b": connected\nretry: 2000\n\n")
            self.wfile.flush()
            # A response.text/content or default large client chunk would defer
            # the initial frame until this pause and the final event finish.
            time.sleep(0.6)
            cursor = int(self.headers.get("Last-Event-ID") or
                         parse_qs(urlsplit(self.path).query).get("after_id", ["0"])[0])
            for identifier in (1, 2, 3):
                if identifier > cursor:
                    self.wfile.write(f'id: {identifier}\nevent: runner.log\ndata: {{"id":{identifier}}}\n\n'.encode())
            progress = {"progress": {}, "attempts": [], "provisional": False,
                        "live_updated_at": None, "log_start_id": 1}
            self.wfile.write(f"event: launch.progress\ndata: {json.dumps(progress)}\n\n".encode())
            self.wfile.write(b'event: launch.status\ndata: {"status":"COMPLETED"}\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as owner, requests.Session() as other:
            owner.cookies.set("operator", "Alice")
            other.cookies.set("operator", "Bob")
            result = verify_launch(f"http://127.0.0.1:{server.server_port}", "existing", owner,
                                   other_owner=other, first_frame_timeout=2)
        assert result["initial_log_count"] == 3
        assert result["header_resume_log_count"] == result["query_resume_log_count"] == 1
        assert result["first_frame_after_headers_seconds"] < 0.5
        assert result["terminal_closed"] and result["cross_owner_denial"] == "passed"
        assert result["new_experiments_or_model_calls"] == 0
        assert len(paths) == 5 and paths[-1].endswith("?after_id=2")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
