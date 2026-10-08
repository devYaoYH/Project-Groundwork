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


def test_calendar_mixed_runtime_validate_lock_and_plan_preserve_options(tmp_path):
    import sqlite3
    import yaml

    design = {
        "schema_version": 1, "release": "calendar@v1",
        "parameters": {"density": {"randomize": True}, "num_slots": {"randomize": True},
                       "num_meetings": {"randomize": True}, "communication_topology": {"factor": ["ring", "phase_shift"]}},
        "units": {"episodes_per_cell": 1}, "seed": {"root": 42},
        "roster": [
            {"id": "local", "role": "calendar-agent", "kind": "scripted", "binding": "dsm"},
            {"id": "child", "role": "calendar-agent", "kind": "scripted", "binding": "baseline", "runtime": "local_process", "harness": "scripted"},
            {"id": "external", "role": "calendar-agent", "kind": "llm", "runtime": "external"},
            {"id": "other-1", "role": "calendar-agent", "kind": "scripted", "binding": "baseline"},
            {"id": "other-2", "role": "calendar-agent", "kind": "scripted", "binding": "baseline"},
        ],
    }
    previous = LocalStackHandler.database, LocalStackHandler.workspace, LocalStackHandler._control_plane
    LocalStackHandler.database = tmp_path / "mixed.db"
    LocalStackHandler.workspace = WORKSPACE
    LocalStackHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalStackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        text = yaml.safe_dump(design)
        status, validation = _request(base, "/api/designs/validate", {"release_id": "calendar", "design_text": text})
        assert status == 200 and validation["valid"]
        preview = validation["plan"]["preview_episode_config"]
        assert preview["agents"][1]["runtime"] == "local_process" and preview["agents"][1]["harness"] == "scripted"
        assert preview["agents"][2] == {"id": "external", "type": "llm", "runtime": "external"}
        assert "communication" in preview and "communication_topology" not in preview
        status, experiment = _request(base, "/api/experiments", {"name": "mixed runtime design", "release_id": "calendar", "design_text": text})
        assert status == 201
        status, locked = _request(base, f"/api/experiments/{experiment['id']}/lock", {"design_sha256": experiment["design_sha256"]})
        assert status == 200 and locked["locked_at"]
        with sqlite3.connect(LocalStackHandler.database) as connection:
            rows = connection.execute("SELECT episode_configs FROM cells WHERE experiment_id = ?", (experiment["id"],)).fetchall()
        assert len(rows) == 2
        for row in rows:
            cfg = json.loads(row[0])[0]
            assert cfg["agents"][1]["runtime"] == "local_process"
            assert cfg["agents"][2]["runtime"] == "external"
            assert cfg["communication"]["topology"]["default"]["graph"] == "ring"
        invalid = {**design, "roster": [{**participant, "runtime": "human"} for participant in design["roster"]]}
        status, rejected = _request(base, "/api/designs/validate", {"release_id": "calendar", "design_text": yaml.safe_dump(invalid)})
        assert not rejected.get("valid", False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        LocalStackHandler.database, LocalStackHandler.workspace, LocalStackHandler._control_plane = previous


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

        status, rejected = _request(base, f"/api/experiments/{experiment['id']}/design", {
            "design_text": DESIGN.replace("root: 41", "root: 42"),
        })
        assert status == 400
        assert "fork it before editing" in rejected["error"]

        status, fork = _request(base, f"/api/experiments/{experiment['id']}/fork", {})
        assert status == 201
        assert fork["forked_from"] == experiment["id"]
        assert fork["locked_at"] is None

        status, fork_locked = _request(base, f"/api/experiments/{fork['id']}/lock", {
            "design_sha256": fork["design_sha256"],
        })
        assert status == 200
        assert fork_locked["locked_at"]

        _, parent_detail = _request(base, f"/api/experiments/{experiment['id']}")
        _, fork_detail = _request(base, f"/api/experiments/{fork['id']}")
        assert {
            cell["cell_id"] for cell in parent_detail["cells"]
        }.isdisjoint(cell["cell_id"] for cell in fork_detail["cells"])

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
