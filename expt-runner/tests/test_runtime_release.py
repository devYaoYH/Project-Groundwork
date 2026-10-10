import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def release_runner(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/run_game_runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"environment_id": "calendar", "experiment": str(tmp_path / "experiment.yaml")}))
    monkeypatch.setattr(module, "load_experiment", lambda path: SimpleNamespace(environment_ids=lambda: ["calendar"]))
    measure = SimpleNamespace(name="efficiency", producer="environment")
    monkeypatch.setattr(module, "get_environment_spec", lambda game: SimpleNamespace(declaration=SimpleNamespace(measures=[measure])))
    traces = []
    monkeypatch.setattr(module, "SQLiteEpisodeStore", lambda **kwargs: SimpleNamespace(iter_episodes=lambda: iter(traces)))
    return module, ["--release", str(release), "--storage-path", str(tmp_path / "a2a.db")], traces


def trace(uid, *, stopped=False, metrics=None):
    return SimpleNamespace(episode_uid=uid, config=SimpleNamespace(environment_id="calendar"),
                           stopped=stopped, metrics=metrics if metrics is not None else {"efficiency": 1.0})


def test_release_checks_only_this_invocations_new_traces(release_runner, monkeypatch):
    module, args, traces = release_runner
    traces.append(trace("historical", stopped=True, metrics={}))
    monkeypatch.setattr(module, "run_experiment", lambda args: traces.append(trace("new")) or 0)
    assert module.main(args) == 0


@pytest.mark.parametrize("result, message", [(trace("new", stopped=True), "stopped episode"),
                                           (trace("new", metrics={}), "absent from emitted metrics"),
                                           (None, "emitted no episodes")])
def test_release_rejects_current_stopped_missing_or_absent_results(release_runner, monkeypatch, result, message):
    module, args, traces = release_runner
    monkeypatch.setattr(module, "run_experiment", lambda args: (traces.append(result) if result else None) or 0)
    with pytest.raises(AssertionError, match=message):
        module.main(args)
