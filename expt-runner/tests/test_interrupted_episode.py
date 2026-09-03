"""A runner killed mid-episode still leaves a readable record.

Before the durable event sink, ``EventLog`` held events in a list and the first
durable write was ``put_episode()`` after the environment returned, so a
SIGKILL lost the whole transcript -- after the tokens were spent -- unless
Redis happened to be configured. This drives the real thing: a real subprocess,
a real signal, and **no Redis**.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from a2a_engine.event_sink import iter_event_sinks, read_event_sink
from a2a_engine.stream_projection import project_events_to_trace

REPO = Path(__file__).resolve().parents[2]

# A environment that announces itself, emits a few events, then blocks forever.
# The parent kills it partway, which is the situation the sink exists for.
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

EXPERIMENT = """
name: interrupted
defaults:
  environment_id: slow
  num_agents: 2
cells:
  - label: b1
    count: 1
    config: {seed: 3}
"""


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_an_interrupted_episode_is_recoverable_from_its_event_log(tmp_path):
    runner = tmp_path / "slow_runner.py"
    runner.write_text(SLOW_GAME)
    experiment = tmp_path / "exp.yaml"
    experiment.write_text(EXPERIMENT)
    results = tmp_path / "results"

    environment = dict(os.environ)
    # The whole point: no operational stream is available, and the record must
    # survive anyway.
    environment.pop("A2A_REDIS_URL", None)
    environment["PYTHONPATH"] = str(REPO)

    process = subprocess.Popen(
        [sys.executable, str(runner), str(experiment),
         "--results-dir", str(results), "--max-parallelism", "1",
         "--storage-path", str(tmp_path / "a2a.db")],
        cwd=REPO, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    try:
        deadline = time.monotonic() + 60
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

    sinks = list(iter_event_sinks(results, "interrupted"))
    assert len(sinks) == 1, "the episode's event log should exist despite the kill"

    entries = read_event_sink(sinks[0])
    assert [entry["event"]["type"] for entry in entries] == [
        "game_start", "message", "message", "message",
    ]
    assert entries[0]["episode_id"] == "interrupted.b1.0"

    trace = project_events_to_trace(sinks[0])
    assert trace.stopped is True
    assert trace.observability["partial"] is True
    assert trace.config.environment_id == "slow"
    # Nothing was persisted to the store: the sink is the only evidence, which
    # is exactly the case that used to lose the transcript entirely.
    assert not list(results.glob("interrupted/*.json"))


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_a_completed_episode_leaves_an_event_log_beside_its_trace(tmp_path):
    """The sink is not a crash-only path; it is written on every run."""
    experiment = tmp_path / "exp.yaml"
    experiment.write_text(EXPERIMENT.replace("environment_id: slow", "environment_id: word_guess")
                          .replace("config: {seed: 3}", "config: {seed: 3, max_turns: 2}"))
    results = tmp_path / "results"

    from expt_runner.run_experiment import main

    assert main([str(experiment), "--results-dir", str(results), "--max-parallelism", "1",
                 "--smoke-test", "--storage-path", str(tmp_path / "a2a.db")]) == 0

    sinks = list(iter_event_sinks(results, "interrupted"))
    assert len(sinks) == 1
    trace = project_events_to_trace(sinks[0])
    assert trace.observability["partial"] is False
    assert json.loads(sinks[0].read_text().splitlines()[0])["episode_id"] == "interrupted.b1.0"
