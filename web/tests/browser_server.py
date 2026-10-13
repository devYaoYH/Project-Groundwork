"""Serve built Next export with the actual Python route and disposable storage."""

import os
import shutil
import sqlite3
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_stack.server import LocalStackHandler  # noqa: E402
from scripts.workspace import seed_browser_fixtures  # noqa: E402


class BrowserStackHandler(LocalStackHandler):
    def do_GET(self):
        # Browser downloads use a separate request that page.route cannot mock.
        if self.path == "/api/exports/browser-test-export/download":
            empty_zip = bytes.fromhex("504b0506") + bytes(18)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition", 'attachment; filename="experiment-export-test.zip"'
            )
            self.send_header("Content-Length", str(len(empty_zip)))
            self.end_headers()
            self.wfile.write(empty_zip)
            return
        super().do_GET()


with TemporaryDirectory(prefix="workflow-browser-") as directory:
    LocalStackHandler.database = Path(directory) / "a2a.db"
    # Optional local evidence preview: preserve archived scientific payloads.
    evidence = os.environ.get("GROUNDWORK_EVIDENCE_DB")
    if evidence:
        source = Path(evidence).resolve()
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True) as existing,
            sqlite3.connect(LocalStackHandler.database) as target,
        ):
            existing.backup(target)
        if (source.parent / "assets").is_dir():
            shutil.copytree(source.parent / "assets", Path(directory) / "assets")
    LocalStackHandler.workspace = ROOT
    LocalStackHandler.static_dir = ROOT / "web/out"
    assert (LocalStackHandler.static_dir / "index.html").is_file(), (
        "Build Next export before test:browser"
    )
    seed_browser_fixtures(Path(directory))
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ.get("GROUNDWORK_BROWSER_PORT", "8765"))), BrowserStackHandler
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
