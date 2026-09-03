"""The researcher design endpoints run against a real local HTTP server."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.server import LocalStackHandler


WORKSPACE = Path(__file__).resolve().parents[2]
DESIGN = (Path(__file__).parent / "buyer_seller_design_smoke.yaml").read_text()


def _request(base: str, path: str, body: dict | None = None):
    request = Request(
        base + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_design_create_validate_lock_and_smoke_launch_through_http(tmp_path):
    previous = (
        LocalStackHandler.database,
        LocalStackHandler.workspace,
        LocalStackHandler._control_plane,
    )
    LocalStackHandler.database = tmp_path / "a2a.db"
    LocalStackHandler.workspace = WORKSPACE
    LocalStackHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalStackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, validation = _request(base, "/api/designs/validate", {
            "release_id": "buyer_seller", "design_text": DESIGN,
        })
        assert status == 200
        assert validation["valid"] is True
        assert validation["plan"]["episodes_planned"] == 2

        status, experiment = _request(base, "/api/experiments", {
            "name": "HTTP buyer seller design",
            "release_id": "buyer_seller",
            "design_text": DESIGN,
        })
        assert status == 201

        status, locked = _request(base, f"/api/experiments/{experiment['id']}/lock", {
            "design_sha256": experiment["design_sha256"],
        })
        assert status == 200
        assert locked["locked_at"]

        status, launch = _request(base, "/api/launches", {
            "experiment_id": experiment["id"], "mode": "smoke",
        })
        assert status == 202
        for _ in range(100):
            _, detail = _request(base, f"/api/launches/{launch['id']}")
            if detail["launch"]["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.05)
        assert detail["launch"]["status"] == "COMPLETED"
        assert detail["progress"]["completed"] == 2
        # Reopening a terminal launch reads explicit episode identities and its
        # persisted runner log from SQLite; it does not depend on SSE.
        assert all(attempt["episode_uid"] for attempt in detail["attempts"])
        assert detail["runner_logs"]

        _, experiment_detail = _request(base, f"/api/experiments/{experiment['id']}")
        assert len(experiment_detail["cells"]) == 2
        assert all(cell["episodes_planned"] == 1 for cell in experiment_detail["cells"])
        _, episodes = _request(base, f"/api/episodes?experiment_id={experiment['id']}")
        assert len(episodes["episodes"]) == 2
        for episode in episodes["episodes"]:
            _, detail = _request(base, f"/api/episodes/{episode['episode_uid']}")
            # The record rides under `episode`; the lane and cursor projections
            # ride alongside it, never inside it.
            trace = detail["episode"]
            assert detail["cursor_max"] == len(trace["events"])
            provenance = trace["config"]["provenance"]
            assert provenance["experiment_id"] == experiment["id"]
            assert provenance["cell_id"] == episode["cell_id"]
            assert provenance["design_sha256"] == locked["design_sha256"]
            assert set(provenance["item_attributes"]) == {"seller_cost"}
            assert trace["config"]["discount_factor"] in {0.5, 1.0}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        LocalStackHandler.database, LocalStackHandler.workspace, LocalStackHandler._control_plane = previous


def test_design_save_through_http_persists_valid_text_and_preserves_draft_on_error(tmp_path):
    previous = (
        LocalStackHandler.database,
        LocalStackHandler.workspace,
        LocalStackHandler._control_plane,
    )
    LocalStackHandler.database = tmp_path / "a2a.db"
    LocalStackHandler.workspace = WORKSPACE
    LocalStackHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalStackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        _, experiment = _request(base, "/api/experiments", {
            "name": "HTTP draft save", "release_id": "buyer_seller", "design_text": DESIGN,
        })
        revised = DESIGN.replace("root: 41", "root: 42")
        status, saved = _request(base, f"/api/experiments/{experiment['id']}/design", {
            "design_text": revised,
        })
        assert status == 200
        assert saved["id"] == experiment["id"]
        assert saved["design_text"] == revised

        status, rejected = _request(base, f"/api/experiments/{experiment['id']}/design", {
            "design_text": revised.replace("buyer_value: {pin: 30}", "buyer_value: {pin: 999}"),
        })
        assert status == 400
        assert rejected["errors"]

        _, detail = _request(base, f"/api/experiments/{experiment['id']}")
        assert detail["experiment"]["design_text"] == revised
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        LocalStackHandler.database, LocalStackHandler.workspace, LocalStackHandler._control_plane = previous
