import copy
from contextlib import nullcontext
import threading
from types import SimpleNamespace

import pytest

from a2a_engine.remote.compose import compose_enabled, runtime_config


def test_compose_is_opt_in_and_rosters_are_not_mutated(monkeypatch):
    config = {"agents": [{"type": "scripted", "id": "agent", "runtime": "in_process"}]}
    original = copy.deepcopy(config)
    monkeypatch.delenv("A2A_RUNTIME_MODE", raising=False)
    assert not compose_enabled()
    assert runtime_config(config) is config
    monkeypatch.setenv("A2A_RUNTIME_MODE", "compose")
    assert runtime_config(config)["agents"][0]["runtime"] == "external"
    assert config == original


@pytest.mark.parametrize("config", [{}, {"agents": [{"type": "human"}]},
                                    {"agents": [{"type": "llm", "api_key": "secret"}]}])
def test_compose_refuses_unscoped_or_secret_rosters(config, monkeypatch):
    monkeypatch.setenv("A2A_RUNTIME_MODE", "compose")
    with pytest.raises(ValueError):
        runtime_config(config)


def test_unknown_mode_does_not_silently_use_loopback(monkeypatch):
    monkeypatch.setenv("A2A_RUNTIME_MODE", "other")
    with pytest.raises(ValueError):
        compose_enabled()


def test_cancellation_during_descriptor_publication_removes_every_descriptor(tmp_path, monkeypatch):
    from a2a_engine.remote import compose
    from a2a_engine.remote.server import EpisodeRegistry

    first_written, cancellation_progress = threading.Event(), threading.Event()

    class ObservedLock:
        """Resume the publisher only after cancellation blocks or completes."""
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "cancel-descriptors":
                if not self.lock.acquire(blocking=False):
                    cancellation_progress.set()
                    self.lock.acquire()
            else:
                self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    manager = compose.ComposeRuntimeManager([])
    manager.lock = ObservedLock()
    manager.environment = SimpleNamespace(registry=EpisodeRegistry())
    manager.io = SimpleNamespace(base_url="http://viewer:8100")
    manager.provisioning_dir = tmp_path
    monkeypatch.setattr(compose, "_managers", {manager})
    monkeypatch.setattr(compose, "validate_runtimes", lambda config, **kwargs: config["agents"])
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"healthy": True})
    client = SimpleNamespace(get=lambda url: response)
    monkeypatch.setattr(compose.httpx, "Client", lambda **kwargs: nullcontext(client))
    original_write = compose.write_descriptor

    def pause_first_write(directory, episode, seat, join_url, **kwargs):
        path = original_write(directory, episode, seat, join_url, **kwargs)
        if seat.seat == 0:
            first_written.set()
            assert cancellation_progress.wait(5), "cancellation did not reach the publication boundary"
        return path

    monkeypatch.setattr(compose, "write_descriptor", pause_first_write)
    failures, runtimes = [], []

    def provision():
        try:
            runtimes.append(manager.provision({"environment_id": "calendar", "agents": [
                {"type": "scripted", "runtime": "external"},
                {"type": "scripted", "runtime": "external"}]}, scripted=True))
        except BaseException as error:
            failures.append(error)

    def cancel():
        try:
            compose.cancel_active_episodes()
        except BaseException as error:
            failures.append(error)
        finally:
            cancellation_progress.set()

    publisher = threading.Thread(target=provision, daemon=True)
    canceller = threading.Thread(target=cancel, name="cancel-descriptors", daemon=True)
    publisher.start()
    assert first_written.wait(5)
    canceller.start()
    publisher.join(5)
    canceller.join(5)
    assert not publisher.is_alive() and not canceller.is_alive()
    assert not list(tmp_path.glob("seat-*/*.json")), "descriptor publication raced past episode cleanup"
    assert not failures
    assert manager.cancelled and not manager.episodes
    assert len(runtimes) == 1 and runtimes[0].closed
