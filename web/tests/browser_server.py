"""Serve built Next export with the actual Python route and disposable storage."""

import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_stack.server import LocalStackHandler

with TemporaryDirectory(prefix="workflow-browser-") as directory:
    LocalStackHandler.database = Path(directory) / "a2a.db"
    LocalStackHandler.workspace = ROOT
    LocalStackHandler.static_dir = ROOT / "web/out"
    assert (LocalStackHandler.static_dir / "index.html").is_file(), "Build Next export before test:browser"
    server = ThreadingHTTPServer(("127.0.0.1", 8765), LocalStackHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
