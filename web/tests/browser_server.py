"""Serve built Next export with the actual Python route and disposable storage."""

import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_stack.server import LocalStackHandler
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from negotiation_game.game import NegotiationGame

with TemporaryDirectory(prefix="workflow-browser-") as directory:
    LocalStackHandler.database = Path(directory) / "a2a.db"
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
    server = ThreadingHTTPServer(("127.0.0.1", 8765), LocalStackHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
