"""Tests for ``a2a-run --smoke-test``.

The smoke test is what a new contributor runs first, so its own failure modes
matter: it must fail loudly on an unreachable sink, must not require API keys,
and must actually persist rather than reporting success on a no-op.
"""

from __future__ import annotations

import textwrap

import pytest

from a2a_engine import register_environment
from a2a_engine.schemas import EpisodeConfigBase, Event, EpisodeTrace
from a2a_engine.storage.base import StoreCheck, register_store
from expt_runner.run_experiment import main


class TinyGame:
    """Minimal environment: emits one message and finishes."""

    def __init__(self, config: dict, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

    def run(self) -> EpisodeTrace:
        return EpisodeTrace(
            episode_uid="placeholder",
            config=EpisodeConfigBase(**{
                "environment_id": self.config.get("environment_id", "tiny"),
                "num_agents": 2,
                **{k: v for k, v in self.config.items()
                   if k in {"experiment_name", "episode_id", "seed"}},
            }),
            events=[Event(type="message", data={"speaker": "a", "text": "hi"})],
            metrics={"ok": 1},
        )


class TinyGameB(TinyGame):
    pass


@pytest.fixture(autouse=True)
def games():
    register_environment("tiny", TinyGame, package=None, dry_run_checks_keys=False)
    register_environment("tiny_b", TinyGameB, package=None, dry_run_checks_keys=False)


def write_experiment(tmp_path, body: str):
    path = tmp_path / "e.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


ONE_GAME = """
    name: smoke_demo
    storage:
      backend: sqlite
      path: PLACEHOLDER
    defaults:
      environment_id: tiny
      num_agents: 2
    cells:
      - label: only
        count: 3
        config: {seed: 1}
"""


def test_smoke_test_persists_and_reads_back(tmp_path, capsys):
    db = tmp_path / "t.db"
    yaml_path = write_experiment(tmp_path, ONE_GAME.replace("PLACEHOLDER", str(db)))

    rc = main([yaml_path, "--smoke-test", "--results-dir", str(tmp_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "PASS" in out
    assert "write/read/delete round-trip succeeded" in out

    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    store = SQLiteEpisodeStore(path=db, results_dir=tmp_path)
    rows, _ = store.list_episodes(limit=100)
    # One run per cell by default, not the cell's full count of 3.
    assert len(rows) == 1


def test_smoke_episodes_per_cell_is_configurable(tmp_path):
    db = tmp_path / "t.db"
    yaml_path = write_experiment(tmp_path, ONE_GAME.replace("PLACEHOLDER", str(db)))

    assert main([yaml_path, "--smoke-test", "--smoke-episodes-per-cell", "2",
                 "--results-dir", str(tmp_path)]) == 0

    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    rows, _ = SQLiteEpisodeStore(path=db, results_dir=tmp_path).list_episodes(limit=100)
    assert len(rows) == 2


def test_smoke_test_fails_when_the_sink_is_unreachable(tmp_path, capsys):
    class DeadStore:
        name = "dead"

        def __init__(self, **_):
            pass

        def check(self) -> StoreCheck:
            return StoreCheck(backend="dead", ok=False, detail="connection refused")

        def put_episode(self, trace, manifest):  # pragma: no cover - must not run
            raise AssertionError("put_episode called despite a failed sink check")

        def get_episode(self, episode_uid):  # pragma: no cover
            return None

        def list_episodes(self, filters=None, limit=50, cursor=None):  # pragma: no cover
            return [], None

    register_store("dead", DeadStore)
    yaml_path = write_experiment(tmp_path, """
        name: smoke_demo
        storage:
          backend: dead
        defaults:
          environment_id: tiny
          num_agents: 2
        cells:
          - label: only
            config: {seed: 1}
    """)

    assert main([yaml_path, "--smoke-test", "--results-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "connection refused" in out
    assert "Nothing was run" in out


def test_smoke_test_reports_a_cell_naming_an_uninstalled_game(tmp_path, capsys):
    yaml_path = write_experiment(tmp_path, f"""
        name: smoke_demo
        storage:
          backend: sqlite
          path: {tmp_path / 't.db'}
        defaults:
          num_agents: 2
        cells:
          - label: good
            config: {{environment_id: tiny, seed: 1}}
          - label: bad
            config: {{environment_id: not_installed, seed: 2}}
    """)

    assert main([yaml_path, "--smoke-test", "--results-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "not_installed" in out
    assert "not installed" in out


def test_smoke_test_spans_several_games_in_one_experiment(tmp_path, capsys):
    db = tmp_path / "t.db"
    yaml_path = write_experiment(tmp_path, f"""
        name: multi
        storage:
          backend: sqlite
          path: {db}
        defaults:
          num_agents: 2
        cells:
          - label: first
            config: {{environment_id: tiny, seed: 1}}
          - label: second
            config: {{environment_id: tiny_b, seed: 2}}
    """)

    assert main([yaml_path, "--smoke-test", "--results-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "tiny" in out and "tiny_b" in out

    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    store = SQLiteEpisodeStore(path=db, results_dir=tmp_path)
    rows, _ = store.list_episodes(limit=100)
    assert {r["environment_id"] for r in rows} == {"tiny", "tiny_b"}


def test_smoke_test_detects_a_sink_that_silently_drops_writes(tmp_path, capsys):
    """A store that accepts a write but stores nothing must not report PASS."""

    class AmnesiacStore:
        name = "amnesiac"

        def __init__(self, **_):
            pass

        def check(self) -> StoreCheck:
            return StoreCheck(backend="amnesiac", ok=True, detail="reachable")

        def put_episode(self, trace, manifest):
            return "amnesiac://written"

        def get_episode(self, episode_uid):
            return None  # the write never actually landed

        def list_episodes(self, filters=None, limit=50, cursor=None):
            return [], None

    register_store("amnesiac", AmnesiacStore)
    yaml_path = write_experiment(tmp_path, """
        name: smoke_demo
        storage:
          backend: amnesiac
        defaults:
          environment_id: tiny
          num_agents: 2
        cells:
          - label: only
            config: {seed: 1}
    """)

    assert main([yaml_path, "--smoke-test", "--results-dir", str(tmp_path)]) == 1
    assert "did not read back" in capsys.readouterr().out


def test_dry_run_still_persists_nothing(tmp_path):
    db = tmp_path / "t.db"
    yaml_path = write_experiment(tmp_path, ONE_GAME.replace("PLACEHOLDER", str(db)))

    assert main([yaml_path, "--dry-run", "--results-dir", str(tmp_path)]) == 0

    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    rows, _ = SQLiteEpisodeStore(path=db, results_dir=tmp_path).list_episodes(limit=100)
    assert rows == []
