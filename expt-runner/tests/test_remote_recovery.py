"""A killed worker leaves evidence a reader with no access to its disk can find.

``test_interrupted_episode.py`` already proves the transcript survives a
SIGKILL -- but it proves it by reading the worker's own ``--results-dir``, which
is a guarantee that evaporates the moment the worker is on another machine.
These tests take that away: the worker's results directory is **deleted** before
anything is recovered, so the only remaining copy is the one the worker
published to the shared artifact store as it played.

The second test runs the real negotiation environment, which until now kept its
events in a list and published only to Redis. A negotiation episode killed
mid-run recovered as nothing at all.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from a2a_engine.artifacts import make_artifact_store
from a2a_engine.event_sink import configure_event_artifacts, read_event_sink
from a2a_engine.provenance import episode_status
from a2a_engine.stream_projection import project_events_to_trace

REPO = Path(__file__).resolve().parents[2]
LAUNCH_ID = "launch-remote-recovery"

# An environment that announces itself, emits a few events, then blocks forever
# -- the same instrument ``test_interrupted_episode.py`` uses, because "killable
# at a known point" is a property of the test, not of the recording path.
SLOW_GAME = '''
import sys, time
from a2a_engine import EpisodeConfigBase, EpisodeTrace, EventLog, register_environment


class SlowGame:
    def __init__(self, config, dry_run=False):
        self.config = EpisodeConfigBase(**config)
        self.events = EventLog.from_config(self.config)

    def run(self):
        self.events.append("game_start", {"num_agents": 2})
        for turn in range(3):
            self.events.append("message", {"speaker": "a", "text": f"turn {turn}"})
        print("EVENTS_WRITTEN", flush=True)
        time.sleep(600)  # never reaches game_end
        return EpisodeTrace(episode_uid="", config=self.config)


register_environment("slow", SlowGame)

from expt_runner.run_experiment import main
sys.exit(main(sys.argv[1:]))
'''

# The real negotiation environment, paused after a handful of events. Nothing
# about how those events are recorded is altered: they travel the same
# ``_record`` -> ``EventLog`` -> sink -> artifact path they always do. The
# pause only makes the process killable at a point the test can name.
SLOW_NEGOTIATION = '''
import sys, time
import negotiation_game.game as negotiation

_record = negotiation.NegotiationGame._record


def paused_record(self, event_type, data):
    _record(self, event_type, data)
    if len(self.events) >= 6:
        print("EVENTS_WRITTEN", flush=True)
        time.sleep(600)  # never reaches game_complete


negotiation.NegotiationGame._record = paused_record

from expt_runner.run_experiment import main
sys.exit(main(sys.argv[1:]))
'''

SLOW_EXPERIMENT = """
name: interrupted
defaults:
  environment_id: slow
  num_agents: 2
cells:
  - label: b1
    count: 1
    config: {seed: 3}
"""

NEGOTIATION_EXPERIMENT = """
name: interrupted
defaults:
  environment_id: negotiation
  num_agents: 2
  num_rounds: 4
  cheap_talk_turns: 2
  enable_cheap_talk: true
  agents:
    - {type: heuristic}
    - {type: heuristic}
cells:
  - label: b1
    count: 1
    config: {seed: 7}
"""


def _kill_a_worker_mid_episode(tmp_path: Path, runner_source: str, experiment_text: str) -> Path:
    """Run a worker until it says it has written events, then SIGKILL it.

    Returns the artifact root -- deliberately the *only* thing handed back. The
    worker's results directory is removed before this returns, so a caller
    cannot accidentally read it and call that recovery.
    """
    runner = tmp_path / "worker.py"
    runner.write_text(runner_source)
    experiment = tmp_path / "exp.yaml"
    experiment.write_text(experiment_text)
    worker_results = tmp_path / "worker-results"
    artifact_root = tmp_path / "artifacts"

    environment = dict(os.environ)
    # No operational stream: the artifact store is the only remote copy, which
    # is the claim under test.
    environment.pop("A2A_REDIS_URL", None)
    environment["PYTHONPATH"] = str(REPO)

    process = subprocess.Popen(
        [sys.executable, str(runner), str(experiment),
         "--results-dir", str(worker_results), "--max-parallelism", "1",
         "--artifact-root", str(artifact_root), "--launch-id", LAUNCH_ID,
         "--storage-path", str(tmp_path / "a2a.db")],
        cwd=REPO, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    try:
        deadline = time.monotonic() + 120
        assert process.stdout is not None
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            if "EVENTS_WRITTEN" in line:
                break
        else:  # pragma: no cover - only on a very slow machine
            pytest.fail("the environment never reached its events")
        process.send_signal(signal.SIGKILL)
    finally:
        process.wait(timeout=30)

    assert process.returncode != 0, "the process must have died rather than finished"

    # The whole point. Whatever the worker wrote for itself is gone; only what
    # it published survives.
    shutil.rmtree(worker_results, ignore_errors=True)
    assert not worker_results.exists()
    return artifact_root


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_a_killed_worker_is_recoverable_from_the_artifact_store_alone(tmp_path):
    artifact_root = _kill_a_worker_mid_episode(tmp_path, SLOW_GAME, SLOW_EXPERIMENT)

    store = make_artifact_store({"backend": "local"}, root=artifact_root)
    refs = list(store.iter_event_sinks(LAUNCH_ID))
    assert len(refs) == 1, "the episode's event stream should have been published"
    ref = refs[0]
    assert ref.episode_id == "interrupted.b1.0"
    # The launch is addressed by the key, so a reader finds this episode without
    # being told anything the worker knew.
    assert list(store.iter_event_sinks(LAUNCH_ID, episode_id="interrupted.b1.0")) == refs
    assert not list(store.iter_event_sinks(LAUNCH_ID, episode_id="interrupted.b1.9"))

    entries = read_event_sink(ref)
    assert [entry["event"]["type"] for entry in entries] == [
        "game_start", "message", "message", "message",
    ]

    trace = project_events_to_trace(ref)
    assert trace.stopped is True
    assert trace.observability["partial"] is True
    assert trace.config.environment_id == "slow"
    # Killed between two events, so the sink never wrote a watermark. How far
    # durability reached is therefore what the artifact holds -- which is the
    # honest answer, not a silently truncated list.
    assert ref.watermark is None and ref.closed is False
    durable_through = ref.watermark if ref.watermark is not None else len(entries)
    assert durable_through == len(entries) == 4


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_a_killed_negotiation_episode_recovers_as_partial(tmp_path):
    """It recovered as nothing before this: negotiation bypassed the sink."""
    artifact_root = _kill_a_worker_mid_episode(
        tmp_path, SLOW_NEGOTIATION, NEGOTIATION_EXPERIMENT
    )

    store = make_artifact_store({"backend": "local"}, root=artifact_root)
    refs = list(store.iter_event_sinks(LAUNCH_ID, episode_id="interrupted.b1.0"))
    assert len(refs) == 1

    entries = read_event_sink(refs[0])
    assert len(entries) >= 6
    types = [entry["event"]["type"] for entry in entries]
    assert types[0] == "game_start"
    assert "game_complete" not in types, "the episode must not have finished"

    trace = project_events_to_trace(refs[0])
    assert trace.config.environment_id == "negotiation"
    assert trace.observability["partial"] is True
    # The same verdict the fact table's status column is filled from: evidence
    # for inspection and retry, never a successful experimental result.
    assert episode_status(trace) == "PARTIAL"


def test_a_worker_with_no_launch_identity_publishes_nothing(tmp_path):
    """Publishing is what a *dispatched* worker does.

    ``a2a-run`` from a shell has no control plane that will come looking, so it
    keeps exactly the local-file behaviour it had and writes no artifacts.
    """
    configure_event_artifacts(None, launch_id=None)
    experiment = tmp_path / "exp.yaml"
    experiment.write_text(
        NEGOTIATION_EXPERIMENT.replace("num_rounds: 4", "num_rounds: 1")
    )
    results = tmp_path / "results"
    artifact_root = tmp_path / "artifacts"

    from expt_runner.run_experiment import main

    assert main([str(experiment), "--results-dir", str(results),
                 "--max-parallelism", "1", "--smoke-test",
                 "--storage-path", str(tmp_path / "a2a.db")]) == 0

    assert not artifact_root.exists()
    # ...and the local sink is still written, unchanged.
    assert list(results.glob("interrupted/*.events.jsonl"))
