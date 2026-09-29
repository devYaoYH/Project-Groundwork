"""A resumed attempt is a new launch of exactly one frozen episode."""

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import yaml
import pytest

from a2a_engine.event_sink import configure_event_artifacts, open_event_sink
from a2a_engine.tracing import EventLog
from a2a_engine.turns import current_log, finish_turn, llm_call, request, response, turn
from local_stack.control_plane import ControlPlane
from local_stack.launchers import ExecutionStatus
from local_stack.server import LocalStackHandler
from local_stack.tests.fakes import FakeLauncher

WORKSPACE = Path(__file__).resolve().parents[2]
DESIGN = (Path(__file__).parent / "buyer_seller_design_smoke.yaml").read_text()


def test_resume_endpoint_publishes_a_single_digest_bound_new_attempt(tmp_path):
    plane = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)
    experiment = plane.create_experiment(name="Resume", release_id="buyer_seller", design_text=DESIGN)
    plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    fake = FakeLauncher()
    plane.launcher = fake
    source = plane.launch_experiment(experiment.id)
    episode_id = plane.planned_episode_ids(source.id)[0]
    config = next(cfg for cfg in plane._design_episode_configs(experiment, mode="live")
                  if cfg["episode_id"] == episode_id)
    configure_event_artifacts(plane.artifacts, launch_id=source.id)
    try:
        sink = open_event_sink(tmp_path / "worker-only", experiment_name=experiment.name,
                               episode_uid="killed-resume", episode_id=episode_id,
                               environment_id="buyer_seller")
        log = EventLog(sink=sink)
        token = current_log.set(log)
        try:
            log.append("game_start", {"config": config})
            with turn("seller", "seller", {"round": 1}):
                with llm_call():
                    request("gpt-4o-mini", "openai", {"messages": [{"role": "user", "content": "offer"}]})
                    response("offer", model="gpt-4o-mini")
                finish_turn("offer")
            with turn("buyer", "buyer", {"round": 1}):
                pass
        finally:
            current_log.reset(token)
            sink.close()
    finally:
        configure_event_artifacts(None, launch_id=None)
    fake.finish(source.id, ExecutionStatus.FAILED)
    detail = plane.launch_detail(source.id)
    partial = next(row for row in detail["attempts"] if row["episode_id"] == episode_id)
    assert partial["status"] == "FAILED" and partial["episode_uid"] == "killed-resume"

    previous = (LocalStackHandler.database, LocalStackHandler.workspace,
                LocalStackHandler._control_plane)
    LocalStackHandler.database = plane.path
    LocalStackHandler.workspace = WORKSPACE
    LocalStackHandler._control_plane = plane
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalStackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"episode_id": episode_id}).encode()
        with urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/launches/{source.id}/resume",
                             data=body, headers={"Content-Type": "application/json"}), timeout=10) as reply:
            assert reply.status == 202
            resumed = json.loads(reply.read())
        assert resumed["id"] != source.id
        assert len(plane.planned_episode_ids(resumed["id"])) == 1
        assert len(fake.submitted) == 2
        plan = yaml.safe_load(Path(resumed["execution_path"]).read_text())
        planned = plan["cells"][0]["config"]
        assert planned["seed"] == config["seed"]
        assert planned["provenance"]["attempt"] == 2
        assert planned["_resume_from"]["episode_id"] == episode_id
        assert planned["_resume_from"]["durable_through"] > 0
        assert "_resume_from" not in planned["provenance"]
        fake.name = "local_container"
        with pytest.raises(ValueError, match="different execution image"):
            plane.resume_attempt(source.id, episode_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        LocalStackHandler.database, LocalStackHandler.workspace, LocalStackHandler._control_plane = previous
