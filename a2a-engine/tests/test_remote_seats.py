import copy
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from a2a_engine.remote.seats import LocalProcessLauncher, validate_runtime, validate_runtimes
from a2a_engine.remote.server import RuntimeManager
from calendar_game.game import CalendarGame
from calendar_game.remote import CALENDAR_TOOLS


def config(**updates):
    return {"environment_id": "calendar", "num_agents": 2, "num_slots": 3,
            "num_meetings": 1, "density": 0, "seed": 1, "enable_reflection": False,
            "max_turns_per_round": 1, "decision_retries": 0,
            "agents": [{"type": "scripted", "runtime": "local_process"}, {"type": "scripted"}], **updates}


@pytest.mark.parametrize("runtime", ["human", "unknown", 42, None, [], {}])
def test_reject_unknown_runtime(runtime):
    with pytest.raises(ValueError):
        validate_runtime({"runtime": runtime})


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "2"])
def test_join_deadline_validation(timeout):
    with pytest.raises(ValueError, match="join_timeout_s"):
        validate_runtimes(config(join_timeout_s=timeout))


def test_model_harness_requires_a_model_but_smoke_keeps_remote():
    cfg = config(agents=[{"type": "llm", "runtime": "local_process", "harness": "structured_output"}])
    with pytest.raises(ValueError, match="requires type: llm and a model"):
        validate_runtimes(cfg)
    assert validate_runtimes(cfg, scripted=True)[0]["runtime"] == "local_process"
    cfg["agents"][0]["model"] = "mock/model"
    assert validate_runtimes(cfg)[0]["harness"] == "structured_output"
    with pytest.raises(ValueError, match="only by calendar"):
        validate_runtimes({**cfg, "environment_id": "word_guess"}, scripted=True)


def test_real_local_child_direct_game_and_cleanup():
    cfg = config()
    before = copy.deepcopy(cfg)
    game = CalendarGame(cfg)
    trace = game.run()
    assert not trace.stopped
    assert trace.metrics["meetings_scheduled"] == 1
    assert cfg == before
    assert game.runtime_context.closed
    assert all(child.process.poll() is not None for child in game.runtime_context.children)
    assert not game.runtime_manager.io.thread.is_alive()
    assert trace.release.adapter_bindings == {"communication": "mcp.http", "models": {"0": "remote", "1": "engine.llm"}, "resources": {}}
    facts = [event.data for event in trace.events if event.type == "agent_registered"]
    assert facts[0]["runtime"] == "local_process"
    assert facts[0]["protocol_version"] == "a2a-turns/1"
    assert facts[0]["agent_info"]["source"] == "self_reported"
    assert facts[1]["protocol_version"] is None


def test_private_descriptor_and_stale_attempt_isolation(tmp_path):
    cfg = config(agents=[{"runtime": "external", "type": "llm"}], join_timeout_s=1)
    with RuntimeManager(CALENDAR_TOOLS, provisioning_dir=tmp_path) as manager:
        first = manager.provision(cfg)
        descriptor = first.descriptors[0]
        payload = json.loads(descriptor.read_text())
        assert descriptor.stat().st_mode & 0o777 == 0o600
        assert payload["seat"] == 0 and payload["expires_at"] > time.time()
        assert payload["join_url"].startswith(manager.io.base_url)
        old = first.episode.seats[0].ticket
        first.close()
        assert not descriptor.exists() and first.episode.seats[0].ticket == ""
        second = manager.provision(cfg)
        assert second.episode.episode_id != first.episode.episode_id
        assert second.episode.attempt_id != first.episode.attempt_id
        assert second.episode.seats[0].ticket != old
        admission = {"join_ticket": old, "callback_url": "http://127.0.0.1:1", "protocol_versions": ["a2a-turns/1"]}
        with httpx.Client(trust_env=False) as client:
            assert client.post(payload["join_url"], json=admission).status_code == 400
            current_url = json.loads(second.descriptors[0].read_text())["join_url"]
            assert client.post(current_url, json=admission).status_code == 400
        assert not second.episode.seats[0].consumed and second.episode.seats[0].secret is None
        second.close()
        port = manager.io.socket.getsockname()[1]
    assert not manager.io.thread.is_alive()
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_descriptor_refuses_public_directory_and_symlink(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    cfg = config(agents=[{"type": "llm", "runtime": "external"}])
    with RuntimeManager(CALENDAR_TOOLS, provisioning_dir=public) as manager:
        with pytest.raises(ValueError, match="owner-only"):
            manager.provision(cfg)
        assert not manager.episodes and not manager.environment.registry._episodes
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with RuntimeManager(CALENDAR_TOOLS, provisioning_dir=link) as manager:
        with pytest.raises(ValueError, match="symlink"):
            manager.provision(cfg)


def test_child_environment_never_inherits_sibling_secrets(monkeypatch):
    captured = {}

    def popen(command, **kwargs):
        captured.update(command=command, **kwargs)
        return object()

    monkeypatch.setenv("A2A_SIGNING_KEY", "runner-key")
    monkeypatch.setenv("A2A_JOIN_TICKET", "sibling-ticket")
    monkeypatch.setenv("OPENAI_API_KEY", "unneeded-provider-key")
    monkeypatch.setattr("a2a_engine.remote.seats.subprocess.Popen", popen)
    LocalProcessLauncher("http://127.0.0.1:1/join", "own-ticket")
    env = captured["env"]
    assert env["A2A_JOIN_TICKET"] == "own-ticket"
    assert "A2A_SIGNING_KEY" not in env and "OPENAI_API_KEY" not in env
    assert "own-ticket" not in str(captured["command"])


def test_provision_launch_failure_cleans_earlier_child(monkeypatch):
    cfg = config(agents=[{"type": "scripted", "runtime": "local_process"}] * 2)
    real = LocalProcessLauncher
    children = []

    def fail_second(*args):
        if children:
            raise OSError("launch failed")
        child = real(*args)
        children.append(child)
        return child

    monkeypatch.setattr("a2a_engine.remote.seats.LocalProcessLauncher", fail_second)
    with RuntimeManager(CALENDAR_TOOLS) as manager:
        with pytest.raises(OSError, match="launch failed"):
            manager.provision(cfg)
        assert not manager.episodes and not manager.environment.registry._episodes
        assert children[0].process.poll() is not None


def test_crashed_real_child_stops_episode_and_is_reaped(monkeypatch):
    real = LocalProcessLauncher

    def crash(*args):
        child = real(*args)
        child.process.kill()
        child.process.wait(timeout=5)
        return child

    monkeypatch.setattr("a2a_engine.remote.seats.LocalProcessLauncher", crash)
    game = CalendarGame(config(join_timeout_s=0.2))
    trace = game.run()
    assert trace.stopped and trace.events[-1].data["reason"] == "seat_unavailable"
    assert game.runtime_context.closed and not game.runtime_manager.io.thread.is_alive()
    assert all(child.process.poll() is not None for child in game.runtime_context.children)


def test_cleanup_error_does_not_skip_other_children_or_app(tmp_path):
    cfg = config(agents=[{"type": "llm", "runtime": "external"}])
    closed = []

    class Child:
        def __init__(self, fail=False):
            self.fail = fail

        def close(self):
            closed.append(self.fail)
            if self.fail:
                raise RuntimeError("cleanup failed")

    manager = RuntimeManager(CALENDAR_TOOLS, provisioning_dir=tmp_path)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with manager:
            first, second = manager.provision(cfg), manager.provision(cfg)
            first.children.extend([Child(), Child(fail=True)])
            second.children.append(Child())
    assert len(closed) == 3
    assert not manager.episodes and not manager.environment.registry._episodes
    assert not manager.io.thread.is_alive() and not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("kind", ["public", "symlink", "directory", "invalid", "fifo"])
def test_descriptor_cli_rejects_unsafe_or_invalid_files_without_logging_contents(tmp_path, kind):
    secret = "must-not-be-logged"
    path = tmp_path / "descriptor"
    if kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        path.write_text(secret)
        path.chmod(0o644 if kind == "public" else 0o600)
        if kind == "symlink":
            link = tmp_path / "link"
            link.symlink_to(path)
            path = link
    result = subprocess.run([sys.executable, "-m", "a2a_agent.server", "--join-descriptor", str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "owner-only provisioning" in result.stderr
    assert secret not in result.stderr + result.stdout
