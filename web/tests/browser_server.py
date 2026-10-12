"""Serve built Next export with the actual Python route and disposable storage."""

import sys
import os
import shutil
import sqlite3
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_stack.server import LocalStackHandler
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from negotiation_game.game import NegotiationGame


class BrowserStackHandler(LocalStackHandler):
    def do_GET(self):
        # Browser downloads use a separate request that page.route cannot mock.
        if self.path == "/api/exports/browser-test-export/download":
            empty_zip = bytes.fromhex("504b0506") + bytes(18)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="experiment-export-test.zip"')
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
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as existing, sqlite3.connect(LocalStackHandler.database) as target:
            existing.backup(target)
        if (source.parent / "assets").is_dir():
            shutil.copytree(source.parent / "assets", Path(directory) / "assets")
    LocalStackHandler.workspace = ROOT
    LocalStackHandler.static_dir = ROOT / "web/out"
    assert (LocalStackHandler.static_dir / "index.html").is_file(), "Build Next export before test:browser"
    config = {'environment_id': 'negotiation', 'num_rounds': 2, 'cheap_talk_turns': 1,
              'agents': [{'type': 'heuristic'}] * 2}
    trace = NegotiationGame(config, dry_run=True).run()
    trace.episode_uid = 'workflow-browser-negotiation'
    manifest = EpisodeManifest.from_run(config=config, experiment_name='workflow-browser', cell_id='cell',
        episode_idx=0, episode_uid=trace.episode_uid)
    SQLiteEpisodeStore(path=LocalStackHandler.database).put_episode(trace, manifest)
    from tangram_reference.environment import TangramReferenceEnvironment
    visual_caps = {"input": ["text", "image"], "evidence": "groundwork:scripted-visual-v1"}
    config = {"environment_id": "tangram_reference", "num_agents": 2, "num_rounds": 2, "seat_roles": ['director', 'matcher'],
              "agents": [{"type": "scripted", "role": role, "capabilities": visual_caps} for role in ['director', 'matcher']],
              "asset_root": str(Path(directory) / "tangram_reference" / "generated")}
    trace = TangramReferenceEnvironment(config, dry_run=True).run()
    trace.episode_uid = "workflow-browser-tangram_reference"
    manifest = EpisodeManifest.from_run(config=trace.config.model_dump(), experiment_name="workflow-browser", cell_id="tangram_reference",
        episode_idx=0, episode_uid=trace.episode_uid)
    SQLiteEpisodeStore(path=LocalStackHandler.database).put_episode(trace, manifest)
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("GROUNDWORK_BROWSER_PORT", "8765"))), BrowserStackHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
