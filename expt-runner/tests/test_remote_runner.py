import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import yaml

from a2a_engine.remote.server import RuntimeManager
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from expt_runner.run_experiment import _check_api_keys, _make_run_context, _run_contexts, _run_one, main
import calendar_game.game


def config(**updates):
    return {"environment_id": "calendar", "num_agents": 3, "num_slots": 3,
            "num_meetings": 1, "density": 0, "seed": 42, "enable_reflection": False,
            "enable_fallback": False, "max_turns_per_round": 2, "decision_retries": 0,
            "join_timeout_s": 15,
            "agents": [{"type": "scripted"}, {"type": "scripted", "runtime": "local_process"},
                       {"type": "llm", "runtime": "external", "model": "openai/not-verified"}], **updates}


@pytest.fixture
def external_from_descriptors(tmp_path):
    """A separately started process joins using only the owner-only descriptor."""
    directory = tmp_path / "private-joins"
    children = []
    seen = set()
    tickets = []
    failures = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            for path in directory.glob("*.json"):
                if path in seen:
                    continue
                try:
                    payload = json.loads(path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    continue
                seen.add(path)
                tickets.append(payload["join_ticket"])
                env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
                children.append(subprocess.Popen([sys.executable, "-m", "a2a_agent.server", "--join-descriptor", str(path)],
                                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            stop.wait(0.01)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    yield directory, children, tickets
    stop.set()
    thread.join(5)
    for child in children:
        try:
            stdout, stderr = child.communicate(timeout=10)
            if child.returncode != 0:
                failures.append(stderr)
            assert all(ticket not in stdout + stderr for ticket in tickets)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            failures.append("external process was not notified of episode_end")
    assert not failures


def test_real_children_parallel_mixed_storage_and_no_secret_leak(tmp_path, external_from_descriptors, monkeypatch, caplog):
    directory, external, tickets = external_from_descriptors
    monkeypatch.setenv("A2A_PROVISIONING_DIR", str(directory))
    instances = []
    contexts = []
    credentials = []
    original = RuntimeManager.provision

    def track(self, cfg, **kwargs):
        if self not in instances:
            instances.append(self)
        runtime = original(self, cfg, **kwargs)
        contexts.append(runtime)
        credentials.extend(seat.ticket for seat in runtime.episode.seats.values())
        return runtime

    monkeypatch.setattr(RuntimeManager, "provision", track)
    authored = config()
    before = copy.deepcopy(authored)
    items = [_make_run_context("mixed", "same-seats", authored, index, False) for index in range(2)]
    for item in items:
        item["verify_readback"] = True
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db", results_dir=tmp_path, mirror_json=True)
    results, errors = _run_contexts(items, store, tmp_path, max_workers=2, on_result=None, on_error=None)
    assert not errors and len(results) == 2
    assert authored == before
    assert len(instances) == 1
    manager = instances[0]
    assert not manager.io.thread.is_alive() and not manager.episodes
    assert len({context.episode.episode_id for context in contexts}) == 2
    assert all(context.closed and not context.episode.active for context in contexts)
    assert all(seat.secret is None and seat.ticket == "" for context in contexts for seat in context.episode.seats.values())
    assert all(child.process.poll() is not None for context in contexts for child in context.children)
    assert len(external) == 2
    assert not list(directory.glob("*.json"))
    manifests, _ = store.list_episodes()
    assert len(manifests) == 2
    for manifest in manifests:
        trace = store.get_episode(manifest["episode_uid"])
        assert trace and not trace.stopped and trace.metrics["meetings_scheduled"] == 1
        assert trace.release.adapter_bindings["models"] == {"0": "engine.llm", "1": "remote", "2": "remote"}
        assert manifest["adapter_bindings"] == trace.release.adapter_bindings
        assert [seat["runtime"] for seat in manifest["agents"]] == ["in_process", "local_process", "external"]
        assert [seat["protocol_version"] for seat in manifest["agents"]] == [None, "a2a-turns/1", "a2a-turns/1"]
        assert manifest["agents"][2]["model"] is None
        assert manifest["agents"][2]["agent_info"]["source"] == "self_reported"
        persisted = trace.model_dump_json() + json.dumps(manifest) + caplog.text
        for path in tmp_path.rglob("*.jsonl"):
            persisted += path.read_text()
        for path in tmp_path.rglob("*.json"):
            persisted += path.read_text()
        assert all(credential not in persisted for credential in credentials + tickets)
        assert "seat_secret" not in persisted and "join_ticket" not in persisted and '"capability"' not in persisted


def test_external_no_show_persists_stopped_and_reaps_local_child(tmp_path, monkeypatch):
    directory = tmp_path / "private-joins"
    monkeypatch.setenv("A2A_PROVISIONING_DIR", str(directory))
    cfg = config(join_timeout_s=1)
    store = SQLiteEpisodeStore(path=tmp_path / "stopped.db")
    ctx = _make_run_context("no-show", "cell", cfg, 0, False)
    result, errors = _run_contexts([ctx], store, tmp_path, max_workers=1, on_result=None, on_error=None)
    assert len(result) == 1 and not errors
    manifest = store.list_episodes()[0][0]
    trace = store.get_episode(manifest["episode_uid"])
    assert trace.stopped
    assert trace.events[0].type == "game_start"
    assert trace.events[-1].type == "game_stopped"
    assert trace.events[-1].data["reason"] == "seat_unavailable"
    assert not list(directory.glob("*.json"))
    assert manifest["agents"][2]["protocol_version"] is None


def test_external_credentials_are_not_environment_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _check_api_keys(config())
    with pytest.raises(EnvironmentError, match="missing API key"):
        _check_api_keys({"agents": [{"type": "llm", "runtime": "local_process", "model": "openai/gpt-5-mini"}]})


@pytest.mark.parametrize("mode", ["--smoke-test", "--dry-run"])
def test_cli_modes_keep_real_remote_transport(tmp_path, mode, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dry-run-reachability-test")
    agents = [{"type": "llm", "runtime": "local_process", "harness": "structured_output", "model": "mock/model"},
              {"type": "scripted"}]
    cfg = config(num_agents=2, agents=agents)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump({"name": "modes", "defaults": cfg,
                                    "storage": {"backend": "sqlite", "path": str(tmp_path / "modes.db")},
                                    "cells": [{"label": "remote", "count": 2}]}))
    assert main([str(path), mode, "--results-dir", str(tmp_path / "results")]) == 0
    if mode == "--smoke-test":
        store = SQLiteEpisodeStore(path=tmp_path / "modes.db")
        manifests, _ = store.list_episodes()
        assert len(manifests) == 1
        assert manifests[0]["agents"][0]["runtime"] == "local_process"
        assert manifests[0]["agents"][0]["protocol_version"] == "a2a-turns/1"
        assert manifests[0]["adapter_bindings"]["communication"] == "mcp.http"
    else:
        assert not (tmp_path / "modes.db").exists()
        assert not list((tmp_path / "results").rglob("*.jsonl"))


def test_runner_error_after_provision_reaps_and_unregisters(tmp_path, monkeypatch):
    cfg = config(num_agents=2, agents=[{"type": "scripted", "runtime": "local_process"}] * 2)
    recorded = []
    original = RuntimeManager.provision

    def track(self, cfg, **kwargs):
        runtime = original(self, cfg, **kwargs)
        recorded.append(runtime)
        return runtime

    monkeypatch.setattr(RuntimeManager, "provision", track)
    monkeypatch.setattr(calendar_game.game.CalendarGame, "_run_async", lambda *args: (_ for _ in ()).throw(RuntimeError("worker error")))
    store = SQLiteEpisodeStore(path=tmp_path / "errors.db")
    results, errors = _run_contexts([_make_run_context("error", "cell", cfg, 0, False)], store, tmp_path,
                                   max_workers=1, on_result=None, on_error=None)
    assert not results and len(errors) == 1
    assert recorded[0].closed and not recorded[0].manager.io.thread.is_alive()
    assert all(child.process.poll() is not None for child in recorded[0].children)


def test_preflight_rejects_all_unsupported_configs_before_any_children(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(RuntimeManager, "__enter__", lambda self: calls.append(self))
    good = config(num_agents=1, agents=[{"type": "scripted", "runtime": "local_process"}])
    bad = {**good, "environment_id": "buyer_seller"}
    with pytest.raises(ValueError, match="only by calendar"):
        _run_contexts([_make_run_context("preflight", "cell", cfg, i, False) for i, cfg in enumerate([good, bad])],
                      object(), tmp_path, max_workers=2, on_result=None, on_error=None)
    assert not calls
