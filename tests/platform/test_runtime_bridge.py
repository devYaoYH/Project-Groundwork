"""Separate interpreters execute pinned science without its infrastructure code."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cloud_platform.control import PlatformControl
from cloud_platform.runtime_bridge import RuntimeBridge
from cloud_platform.storage import ArtifactStore, Repository
from cloud_platform.worker import Worker
from tests.platform.test_storage_worker import DESIGN, WORKSPACE


def wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(.02)
    raise AssertionError(f"runtime did not produce {path.name}")


def supervisor(bridge, *, loop=False, env=None):
    command = [sys.executable, "-I", str(bridge.root / "runtime_supervisor.py"),
               "--execution-dir", str(bridge.root), "--startup-timeout", "5"]
    if loop:
        command.append("--loop")
    return subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def stop(peer):
    if peer.poll() is None:
        peer.terminate()
        try:
            peer.wait(timeout=15)
        except subprocess.TimeoutExpired:
            peer.kill()
            peer.wait()


def test_separate_legacy_runtime_executes_actual_game_with_current_storage(tmp_path, monkeypatch):
    execution = tmp_path / "execution"
    monkeypatch.setenv("PLATFORM_EXECUTION_DIR", str(execution))
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(WORKSPACE))
    monkeypatch.setenv("OPENAI_API_KEY", "worker-secret-must-never-enter-request")
    monkeypatch.setenv("PLATFORM_PRIVATE_INFRA_SECRET", "must-never-enter-request")
    repository = Repository(backend="local", data_dir=tmp_path / "data")
    artifacts = ArtifactStore(backend="local", data_dir=tmp_path / "data")
    control = PlatformControl(repository, artifacts, workspace=WORKSPACE)
    release = next(row for row in control.releases() if row["environment_id"] == "buyer_seller")
    experiment = control.create_experiment("alice", {"name": "legacy peer", "release_id": release["id"], "design_text": DESIGN})
    launch = control.launch(experiment["id"], "alice", {"mode": "live"})
    worker = Worker(repository, artifacts, workspace=WORKSPACE)
    legacy = tmp_path / "frozen-runtime"
    (legacy / "cloud_platform").mkdir(parents=True)
    (legacy / "cloud_platform" / "__init__.py").write_text("raise ImportError('frozen infrastructure is unavailable')\n")
    runtime_env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(legacy), "RUNTIME_ONLY_PROVIDER_SECRET": "inherited-only"}
    captured = []
    original = worker.bridge.start
    def inspect_request(*args, **kwargs):
        process = original(*args, **kwargs)
        captured.append(json.loads((process.run / "request.json").read_text()))
        return process
    monkeypatch.setattr(worker.bridge, "start", inspect_request)
    peer = supervisor(worker.bridge, env=runtime_env)
    try:
        assert worker.run_once(launch["id"])
        assert peer.wait(timeout=5) == 0
    finally:
        stop(peer)
    assert captured and "OPENAI_API_KEY" not in captured[0]["env"]
    assert "PLATFORM_PRIVATE_INFRA_SECRET" not in captured[0]["env"]
    assert "RUNTIME_ONLY_PROVIDER_SECRET" not in captured[0]["env"]
    assert captured[0]["env"]["A2A_DURABLE_EVENT_SINK"] == "platform_runtime_sink:durable_event_write"
    result = repository.get("launches", launch["id"])
    assert result["status"] == "COMPLETED", artifacts.get(f"launches/{launch['id']}/runner.log").decode()
    assert result["evidence_complete"]
    row = repository.list("episodes", owner_id="alice")[0]
    trace = json.loads(artifacts.get(row["artifact_json_key"]))
    keys = artifacts.keys(f"launches/{launch['id']}/events/{row['episode_uid']}/")
    assert keys and [json.loads(artifacts.get(key))["event"] for key in keys] == trace["events"]
    assert not worker.run_once(launch["id"])


def test_persistent_runtime_rearms_and_inherits_only_its_own_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    peer = supervisor(bridge, loop=True, env={"PATH": os.environ["PATH"], "RUNTIME_PROVIDER_SECRET": "runtime-value"})
    try:
        for index in range(2):
            run = bridge.root / f"run-{index}"
            run.mkdir()
            log = run / "runner.log"
            log.touch()
            code = "import os; from platform_runtime_sink import durable_event_write; e={'episode_uid':'same-uid','episode_id':'episode','event':{'type':'game_start'}}; durable_event_write(e,0); durable_event_write(e,0); print(os.environ['RUNTIME_PROVIDER_SECRET'])"
            process = bridge.start([sys.executable, "-c", code],
                env={"OPENAI_API_KEY": "infra-must-not-forward"}, log_path=log, run_dir=run, launch_id=f"launch-{index}")
            assert process.wait(timeout=5) == 0
            assert log.read_text().strip() == "runtime-value"
            assert not list((run / "event-spool" / "pending").glob("*.json"))
            assert not list((run / "event-spool" / "acks").glob("*.json"))
            assert len(bridge.artifacts.keys(f"launches/launch-{index}/events/")) == 1
            process.close()
        assert peer.poll() is None
    finally:
        stop(peer)


def test_missing_runtime_start_is_bounded_and_cannot_reexecute(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_START_TIMEOUT", ".15")
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    process = bridge.start([sys.executable, "-c", "raise AssertionError('must not execute')"], env={}, log_path=log, run_dir=run, launch_id="missing-peer")
    assert process.wait(timeout=2) == 125
    assert "startup deadline" in process.error
    assert (run / "cancel.json").exists()
    peer = supervisor(bridge)
    try:
        assert peer.wait(timeout=3) == 0
        assert json.loads((run / "result.json").read_text())["exit_code"] == 130
        assert log.read_text() == ""
    finally:
        process.close()
        stop(peer)


def test_peer_cancellation_kills_unresponsive_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    ready = run / "ready"
    code = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(60)"
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="cancelled-peer")
    # Shorten only this request's supervisor grace; production defaults to 10s.
    request = json.loads((run / "request.json").read_text())
    request["cancel_grace"] = .2
    (run / "request.json").write_text(json.dumps(request))
    peer = supervisor(bridge)
    try:
        wait_for(ready)
        process.cancel()
        assert process.wait(timeout=3) == -signal.SIGKILL
        assert peer.wait(timeout=3) == 0
    finally:
        process.close()
        stop(peer)


def test_event_upload_failure_fails_closed_in_legacy_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    artifacts = ArtifactStore(backend="local", data_dir=tmp_path / "data")
    def fail(*_args, **_kwargs):
        raise RuntimeError("cloud unavailable")
    monkeypatch.setattr(artifacts, "put", fail)
    bridge = RuntimeBridge(tmp_path / "execution", artifacts)
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    code = "from platform_runtime_sink import durable_event_write; durable_event_write({'episode_uid':'uid','episode_id':'episode','event':{'type':'game_start'}},0); print('acknowledged')"
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="failed-event-peer")
    peer = supervisor(bridge)
    try:
        assert process.wait(timeout=5) == 1
        assert "not acknowledged by infrastructure" in log.read_text()
        assert "\nacknowledged\n" not in log.read_text()
        assert peer.wait(timeout=3) == 0
    finally:
        process.close()
        stop(peer)


def test_duplicate_delivery_shutdown_exits_job_peer_without_runner(tmp_path):
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    bridge.shutdown()
    peer = supervisor(bridge)
    try:
        # Interpreter startup can be delayed by concurrent image builds; the
        # peer must still exit without accepting any scientific request.
        assert peer.wait(timeout=15) == 0
        assert not (bridge.root / "request.json").exists()
    finally:
        stop(peer)


def test_wrong_pinned_runtime_identity_fails_before_running_science(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    process = bridge.start([sys.executable, "-c", "raise AssertionError('must never run mismatched science')"],
        env={}, log_path=log, run_dir=run, launch_id="wrong-runtime", runtime_identity={"image_digest": "sha256:" + "a" * 64})
    peer = supervisor(bridge, env={"PATH": os.environ["PATH"], "PLATFORM_RUNTIME_DIGEST": "sha256:" + "b" * 64})
    try:
        assert process.wait(timeout=5) == 125
        assert "pinned runtime identity mismatch" in process.error
        assert log.read_text() == ""
        assert peer.wait(timeout=3) == 0
    finally:
        process.close()
        stop(peer)


def test_bridge_gcs_event_upload_has_bounded_no_retry_conflict_verification(monkeypatch):
    from google.api_core.exceptions import PreconditionFailed
    calls = []
    class Blob:
        def upload_from_string(self, data, **kwargs):
            calls.append(("put", kwargs))
            raise PreconditionFailed("already exists")
        def download_as_bytes(self, **kwargs):
            calls.append(("get", kwargs))
            return b"event"
    class Bucket:
        def blob(self, _):
            return Blob()
    store = ArtifactStore.__new__(ArtifactStore)
    store.backend, store.bucket = "gcp", Bucket()
    times = iter([100, 101])
    monkeypatch.setattr("cloud_platform.storage.time", SimpleNamespace(monotonic=lambda: next(times)))
    assert store.put("launches/launch/events/uid/00000000.json", b"event", timeout=15)
    assert calls == [("put", {"if_generation_match": 0, "timeout": 15, "retry": None}),
                     ("get", {"timeout": 14, "retry": None})]


def test_hard_worker_death_cancels_runtime_without_waiting_for_another_event(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    ready = run / "ready"
    code = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(60)"
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="orphaned-runtime")
    request = json.loads((run / "request.json").read_text())
    request.update(cancel_grace=.2, worker_heartbeat_timeout=1.5)
    (run / "request.json").write_text(json.dumps(request))
    peer = supervisor(bridge)
    try:
        wait_for(ready)
        process.finished.set()  # simulate the worker disappearing entirely
        process.pump.join(timeout=1)
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert "infrastructure worker heartbeat expired" in process.error
        assert peer.wait(timeout=3) == 0
    finally:
        process.close()
        stop(peer)
