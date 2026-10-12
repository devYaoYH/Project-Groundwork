import json

from a2a_engine.remote.lobby import SeatsUnavailable
from expt_runner.run_experiment import _make_run_context, _run_contexts


def test_one_public_opened_handle_per_attempt(tmp_path, capsys):
    contexts = [_make_run_context("lobby", "cell", {"environment_id": "word_guess", "secret_word": "dog"}, i,
                                  dry_run=True, persist=False) for i in range(2)]
    results, errors = _run_contexts(contexts, None, tmp_path, max_workers=1, on_result=None, on_error=None)
    assert len(results) == 2 and not errors
    lines = [line for line in capsys.readouterr().out.splitlines() if "INFO expt_runner: opened " in line]
    assert len(lines) == 2
    handles = [json.loads(line.split(" ", 4)[4]) for line in lines]
    assert len({h["episode_id"] for h in handles}) == len({h["attempt_id"] for h in handles}) == 2
    assert all("ticket" not in line and "callback" not in line for line in lines)


def test_seat_unavailable_uses_existing_failure_callback(tmp_path, monkeypatch):
    from word_guess.game import WordGuessGame
    def fail(self):
        raise SeatsUnavailable([0])
    monkeypatch.setattr(WordGuessGame, "run", fail)
    context = _make_run_context("lobby", "cell", {"environment_id": "word_guess"}, 0, dry_run=True, persist=False)
    failures = []
    results, errors = _run_contexts([context], None, tmp_path, max_workers=1, on_result=None,
                                    on_error=lambda ctx, error: failures.append(f"fail {ctx['config']['episode_id']}: {error}"))
    assert not results and len(errors) == 1
    assert failures == ["fail lobby.cell.0: seat_unavailable: (0,)"]


def test_calendar_no_show_fails_without_storing_episode(tmp_path, capsys):
    from a2a_engine.storage import make_store
    config = {"environment_id": "calendar", "seed": 42, "num_agents": 2,
              "num_slots": 3, "density": 0, "num_meetings": 1, "join_timeout_s": 0.05,
              "agents": [{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]}
    context = _make_run_context("lobby", "calendar", config, 0, dry_run=True, persist=True)
    failures = []
    store = make_store({"backend": "sqlite", "path": str(tmp_path / "episodes.db")}, results_dir=tmp_path)
    results, errors = _run_contexts([context], store, tmp_path, max_workers=1, on_result=None,
        on_error=lambda ctx, error: failures.append(f"fail {ctx['config']['episode_id']}: {error}"))
    assert not results and len(errors) == 1
    assert failures == ["fail lobby.calendar.0: seat_unavailable: (0,)"]
    assert store.list_episodes()[0] == []
    output = capsys.readouterr().out
    assert output.count("INFO expt_runner: opened ") == 1
    assert "ticket" not in output
