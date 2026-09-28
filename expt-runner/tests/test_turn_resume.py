"""A killed worker resumes from published evidence, not its private files."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from a2a_engine.artifacts import make_artifact_store, sha256_bytes
from a2a_engine.event_sink import read_event_sink
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from a2a_engine.stream_projection import project_events_to_trace


REPO = Path(__file__).resolve().parents[2]
WORKER = '''
import os, sys, time
from a2a_engine import EpisodeConfigBase, EpisodeTrace, EventLog, register_environment
from a2a_engine.llm import api
from a2a_engine.turns import turn, finish_turn, request

def provider(base, key, model, messages, max_tokens, temperature, timeout, cfg):
    request(model, "openai", {"model": model, "messages": messages})
    if os.environ.get("RESUMED") and messages[0]["content"] == "first":
        raise AssertionError("replayed turn called the provider")
    return "sample-one" if messages[0]["content"] == "first" else "sample-two"
api._oneshot_openai = provider

class Game:
    def __init__(self, config, dry_run=False):
        self.config = EpisodeConfigBase(**config)
        self.events = EventLog.from_config(self.config)
    def run(self):
        self.events.append("game_start", {"config": self.config.model_dump(mode="json")})
        client = api.LLMClient(model="gpt-4o-mini", api_key="dummy")
        with turn("a", "a", {"step": 0}):
            first = client.oneshot([{"role": "user", "content": "first"}])
            finish_turn(first)
        self.events.append("message", {"speaker": "a", "text": first})
        with turn("a", "a", {"step": 1}):
            if not os.environ.get("RESUMED"):
                print("INTERRUPT", flush=True)
                time.sleep(600)
            second = client.oneshot([{"role": "user", "content": "second"}])
            finish_turn(second)
        self.events.append("game_end", {"first": first, "second": second})
        return EpisodeTrace(episode_uid="", config=self.config, events=self.events.all(),
                            final_state={"first": first, "second": second})

register_environment("resume_test", Game)
from expt_runner.run_experiment import main
sys.exit(main(sys.argv[1:]))
'''


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_sigkilled_episode_replays_prefix_and_completes_without_recalling_provider(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    experiment = tmp_path / "exp.yaml"
    experiment.write_text('''name: interrupted
defaults:
  environment_id: resume_test
  num_agents: 1
  agents: [{id: a, type: llm, model: gpt-4o-mini}]
cells:
  - label: b1
    count: 1
    config: {seed: 3}
''')
    artifacts = tmp_path / "artifacts"
    results = tmp_path / "worker-results"
    db = tmp_path / "episodes.db"
    env = {**os.environ, "OPENAI_API_KEY": "dummy", "PYTHONPATH": str(REPO)}
    env.pop("A2A_REDIS_URL", None)
    command = [sys.executable, str(worker), str(experiment), "--artifact-root", str(artifacts),
               "--launch-id", "original", "--results-dir", str(results),
               "--storage-path", str(db), "--max-parallelism", "1"]
    process = subprocess.Popen(command, cwd=REPO, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if "INTERRUPT" in line:
                break
            if not line:
                pytest.fail(f"worker exited early: {process.returncode}")
        else:
            pytest.fail("worker did not reach the in-flight turn")
        process.send_signal(signal.SIGKILL)
    finally:
        process.wait(timeout=10)
        process.stdout.close()
    shutil.rmtree(results)
    assert not results.exists()
    store = make_artifact_store({"backend": "local"}, root=artifacts)
    ref = next(iter(store.iter_event_sinks("original")))
    source = read_event_sink(ref)
    partial = project_events_to_trace(ref, environment_id="resume_test")
    assert partial.observability["partial"] is True
    assert partial.observability["event_count"] == ref.observed_through() == len(source)
    assert source[-1]["event"]["type"] == "turn.started"
    assert ref.watermark is None
    raw = ref.read_bytes()
    resumed = subprocess.run(command[:6] + ["resumed"] + command[7:] +
                             ["--resume-episode", ref.episode_id, "--resume-from", ref.uri,
                              "--resume-from-sha256", sha256_bytes(raw),
                              "--resume-durable-through", str(len(source))],
                             cwd=REPO, env={**env, "RESUMED": "1"}, capture_output=True, text=True,
                             timeout=40)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    records = list(SQLiteEpisodeStore(path=db).iter_episodes())
    assert len(records) == 1
    trace = records[0]
    assert trace.stopped is False
    assert trace.final_state == {"first": "sample-one", "second": "sample-two"}
    assert [e.model_dump(mode="json") for e in trace.events[:len(source) - 1]] == [
        e.model_dump(mode="json") for e in partial.events[:-1]
    ]
