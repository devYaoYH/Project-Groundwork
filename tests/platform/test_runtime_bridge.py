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
from cloud_platform.runtime_bridge import LocalProcess, RuntimeBridge
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


@pytest.mark.parametrize("worker_stops", [False, True])
def test_job_peer_waits_for_final_publication_before_exiting(tmp_path, monkeypatch, worker_stops):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    artifacts = ArtifactStore(backend="local", data_dir=tmp_path / "data")
    bridge = RuntimeBridge(tmp_path / "execution", artifacts)
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    process = bridge.start([sys.executable, "-c", "import time; time.sleep(.3)"], env={}, log_path=log,
                           run_dir=run, launch_id="publication-barrier")
    peer = subprocess.Popen([sys.executable, "-I", str(bridge.root / "runtime_supervisor.py"),
                             "--execution-dir", str(bridge.root), "--wait-for-shutdown",
                             "--startup-timeout", ".2"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert process.wait(timeout=5) == 0
        # Finalization can exceed the startup deadline. Runner completion must
        # not end the hosted container while final artifacts are still missing.
        with pytest.raises(subprocess.TimeoutExpired):
            peer.wait(timeout=.3)
        assert not artifacts.keys("launches/publication-barrier/")
        process.close()
        if worker_stops:
            peer.terminate()
            assert peer.wait(timeout=5) == 130
        else:
            artifacts.put("launches/publication-barrier/detail.json", b'{"evidence_complete":true}')
            bridge.shutdown()
            assert peer.wait(timeout=5) == 0
            assert artifacts.get("launches/publication-barrier/detail.json") == b'{"evidence_complete":true}'
    finally:
        process.close()
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


def orphan_code(run, *, leader_success=False):
    ready, calls = run / "descendant-ready", run / "model-calls"
    child = "import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); p=pathlib.Path(" + repr(str(calls)) + "); p.write_text('call\\n'); pathlib.Path(" + repr(str(ready)) + ").write_text(str(os.getpid()))\nwhile True:\n with p.open('a') as out: out.write('call\\n')\n time.sleep(.02)\n"
    leader = "import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); p=pathlib.Path(" + repr(str(ready)) + ")\nwhile not p.exists(): time.sleep(.02)\n"
    if not leader_success:
        leader += "time.sleep(60)\n"
    return leader, ready, calls


def assert_descendant_stopped(pid, calls):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        state = Path(f"/proc/{pid}/stat")
        if state.exists() and state.read_text().split(")", 1)[1].split()[0] == "Z":
            break  # A container PID1 may defer reaping; zombies cannot execute.
        time.sleep(.02)
    else:
        raise AssertionError("descendant still runs after its leader exited")
    before = calls.read_bytes()
    time.sleep(.15)
    assert calls.read_bytes() == before, "orphan performed another model call"


@pytest.mark.parametrize("mode", ["cancel", "heartbeat", "success"])
def test_persistent_supervisor_removes_descendants_after_leader_exits(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    code, ready, calls = orphan_code(run, leader_success=mode == "success")
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="descendant-runtime")
    request = json.loads((run / "request.json").read_text())
    request["worker_heartbeat_timeout"] = 1.5
    (run / "request.json").write_text(json.dumps(request))
    peer = supervisor(bridge, loop=True)
    pid = None
    descendant_cleaned = False
    try:
        wait_for(ready)
        pid = int(ready.read_text())
        if mode == "cancel":
            process.cancel(grace=1)
            assert process.wait(timeout=5) == -signal.SIGTERM
        elif mode == "heartbeat":
            process.finished.set()
            process.pump.join(timeout=1)
            assert process.wait(timeout=5) == -signal.SIGTERM
            assert "infrastructure worker heartbeat expired" in process.error
        else:
            assert process.wait(timeout=5) == 0
        assert_descendant_stopped(pid, calls)
        descendant_cleaned = True
        process.close()
        following = bridge.root / "following"
        following.mkdir()
        next_log = following / "runner.log"
        next_log.touch()
        next_process = bridge.start([sys.executable, "-c", "pass"], env={}, log_path=next_log, run_dir=following, launch_id="following-runtime")
        assert next_process.wait(timeout=5) == 0
        next_process.close()
        assert_descendant_stopped(pid, calls)
    finally:
        process.close()
        stop(peer)
        if pid is not None and not descendant_cleaned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("mode", ["cancel", "success"])
def test_local_executor_cleans_orphans_and_never_rekills_after_close(tmp_path, mode):
    code, ready, calls = orphan_code(tmp_path, leader_success=mode == "success")
    process = LocalProcess([sys.executable, "-c", code], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    descendant_cleaned = False
    try:
        wait_for(ready)
        pid = int(ready.read_text())
        if mode == "cancel":
            process.cancel(grace=1)
            assert process.wait(timeout=5) == -signal.SIGTERM
        else:
            assert process.wait(timeout=5) == 0
        process.close()
        assert process.group_cleaned
        assert_descendant_stopped(pid, calls)
        descendant_cleaned = True
        process.close()
        process.cancel()  # cleanup is idempotent; never reuse a stale PGID
        assert_descendant_stopped(pid, calls)
    finally:
        process.close()
        if pid is not None and not descendant_cleaned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_restarted_peer_never_reexecutes_an_ambiguous_started_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    counter = run / "executions"
    code = "import pathlib,time; p=pathlib.Path(" + repr(str(counter)) + "); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); time.sleep(60)"
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="ambiguous-runtime")
    first_peer = supervisor(bridge, loop=True)
    restarted = None
    runner_pid = None
    try:
        wait_for(counter)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runner_pid = json.loads((run / "started.json").read_text())["pid"]
            if runner_pid is not None:
                break
            time.sleep(.02)
        assert runner_pid is not None
        first_peer.kill()
        first_peer.wait(timeout=5)
        # A container restart kills the old PID namespace. Simulate that here
        # without using a persisted PID to kill anything during recovery.
        os.killpg(runner_pid, signal.SIGKILL)
        runner_pid = None
        restarted = supervisor(bridge, loop=True)
        assert process.wait(timeout=5) == 125
        assert "already started" in process.error
        assert counter.read_text() == "1"
        process.close()
        assert restarted.poll() is None
        assert not (bridge.root / "request.json").exists()
    finally:
        process.close()
        stop(first_peer)
        if restarted:
            stop(restarted)
        if runner_pid is not None:
            try:
                os.killpg(runner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stale_worker_heartbeat_rejected_before_any_child_start(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_RUNTIME_WORKSPACE", str(tmp_path))
    bridge = RuntimeBridge(tmp_path / "execution", ArtifactStore(backend="local", data_dir=tmp_path / "data"))
    run = bridge.root / "run"
    run.mkdir()
    log = run / "runner.log"
    log.touch()
    marker = run / "must-not-exist"
    code = "import pathlib; pathlib.Path(" + repr(str(marker)) + ").touch()"
    process = bridge.start([sys.executable, "-c", code], env={}, log_path=log, run_dir=run, launch_id="stale-worker")
    process.finished.set()
    process.pump.join(timeout=1)
    (run / "worker-heartbeat.json").write_text(json.dumps({"id": "stale-worker", "heartbeat": 0}))
    peer = supervisor(bridge)
    try:
        assert process.wait(timeout=5) == 125
        assert "expired before runtime startup" in process.error
        assert not marker.exists() and not (run / "started.json").exists()
        assert peer.wait(timeout=5) == 0
    finally:
        process.close()
        stop(peer)
