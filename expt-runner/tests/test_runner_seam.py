"""Seam tests: the runner wiring.

The runner is where all the consolidated pieces meet — registry declarations,
storage selection, the resolve hook, manifest emission, resume. It used to do
S3 uploads inline; now it delegates. These tests drive ``main()`` end to end with
a fake environment so they need no API keys, no network, and no cloud credentials.
"""

import json
import os
from pathlib import Path

import pytest

from a2a_engine import EpisodeConfigBase, Event, EpisodeTrace
from a2a_engine.registry import _REGISTRY, register_environment
from expt_runner.run_experiment import _configure_observability, main


class FakeGame:
    """Records the config it was constructed with, so tests can inspect it."""

    seen_configs: list[dict] = []

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        FakeGame.seen_configs.append(dict(config))

    def run(self) -> EpisodeTrace:
        return EpisodeTrace(
            episode_uid="",
            # EpisodeConfigBase allows extras, so a real environment's config subclass
            # carries resolved/derived fields straight into the trace.
            config=EpisodeConfigBase(**self.config),
            events=[Event(type="message", data={"speaker": "a", "text": "hi"})],
            final_state={"ok": True},
            metrics={"score": 1.0},
        )


@pytest.fixture(autouse=True)
def isolated_registry():
    # ``main()`` discovers installed games.  Do not clear that durable process
    # registry on teardown: discovery modules are already imported, so a later
    # viewer test cannot re-trigger their registration just by importing them
    # again.  This fixture only owns the fake environment it introduces.
    saved_fake = _REGISTRY.get("fake")
    FakeGame.seen_configs = []
    yield
    if saved_fake is None:
        _REGISTRY.pop("fake", None)
    else:
        _REGISTRY["fake"] = saved_fake


def write_yaml(tmp_path, body: str) -> Path:
    path = tmp_path / "exp.yaml"
    path.write_text(body)
    return path


BASIC = """
name: test_exp
defaults:
  environment_id: fake
  num_agents: 2
cells:
  - label: b1
    count: 2
    config: {seed: 1}
"""


def test_end_to_end_run_writes_traces_and_manifests(tmp_path):
    register_environment("fake", FakeGame)
    results = tmp_path / "results"

    rc = main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
               "--max-parallelism", "1"])

    assert rc == 0
    episodes = list((results / "test_exp").glob("*.json"))
    manifests = [p for p in episodes if p.name.endswith(".manifest.json")]
    plain = [p for p in episodes if not p.name.endswith((".manifest.json", ".metadata.json"))]
    assert len(plain) == 2
    assert len(manifests) == 2
    trace = json.loads(plain[0].read_text())
    assert len(trace["observability"]["otel_trace_id"]) == 32
    assert len(trace["observability"]["otel_root_span_id"]) == 16


def test_manifest_records_identity_and_config_hash(tmp_path):
    register_environment("fake", FakeGame)
    results = tmp_path / "results"
    main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
          "--max-parallelism", "1"])

    manifest = json.loads(
        next((results / "test_exp").glob("*.manifest.json")).read_text()
    )
    assert manifest["experiment_name"] == "test_exp"
    assert manifest["cell_id"] == "b1"
    assert manifest["environment_id"] == "fake"
    assert manifest["seed"] == 1
    assert manifest["resolved_config_hash"]
    assert manifest["storage"]["status"] == "written"


def test_typed_experiment_attaches_environment_and_episode_to_trace(tmp_path):
    register_environment("fake", FakeGame)
    (tmp_path / "task.json").write_text("fixture")
    import hashlib

    digest = hashlib.sha256(b"fixture").hexdigest()
    (tmp_path / "release.yaml").write_text(
        "schema_version: 1\nid: fake.demo\nrelease: v1\nengine:\n"
        "  environment_id: fake\n  defaults: {num_agents: 2}\ninputs:\n"
        f"  - id: task\n    path: task.json\n    sha256: {digest}\n"
    )
    path = write_yaml(
        tmp_path,
        "schema_version: 1\nname: typed_test\nrelease: release.yaml\n"
        "episodes:\n  - label: sample\n    count: 1\n    seeds: [11]\n"
        "storage: {backend: local}\n",
    )
    results = tmp_path / "results"

    assert main([str(path), "--results-dir", str(results), "--max-parallelism", "1"]) == 0
    trace_path = next(
        p for p in (results / "typed_test").glob("*.json")
        if not p.name.endswith((".manifest.json", ".metadata.json"))
    )
    trace = json.loads(trace_path.read_text())
    manifest = json.loads(next((results / "typed_test").glob("*.manifest.json")).read_text())
    assert trace["release"]["id"] == "fake.demo"
    assert trace["episode"]["id"] == "typed_test.sample.0"
    assert manifest["environment_id"] == "fake"
    assert manifest["release_id"] == "fake.demo"


def test_typed_experiment_defaults_otel_jsonl_under_results_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("A2A_OTEL_TRACES_FILE", raising=False)
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    _configure_observability({"capture_content": True}, results_dir=tmp_path / "results")
    assert os.environ["A2A_OTEL_TRACES_FILE"] == str(tmp_path / "results" / "otel-spans.jsonl")
    assert os.environ["OTEL_TRACES_EXPORTER"] == "file"


def test_runner_uses_the_games_declared_storage_backend(tmp_path):
    """A environment shouldn't need CLI flags to land in its own store."""
    register_environment("fake", FakeGame, storage={"backend": "s3", "prefix": "p"})
    results = tmp_path / "results"

    main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
          "--max-parallelism", "1"])

    manifest = json.loads(
        next((results / "test_exp").glob("*.manifest.json")).read_text()
    )
    # No bucket configured in this release, so S3 degrades to local-only —
    # but the backend selection itself must be visible in the manifest.
    assert manifest["storage"]["backend"] == "s3"


def test_experiment_yaml_storage_overrides_the_game_default(tmp_path):
    register_environment("fake", FakeGame, storage={"backend": "s3"})
    results = tmp_path / "results"
    yaml_body = BASIC.replace("defaults:", "storage:\n  backend: local\ndefaults:")

    main([str(write_yaml(tmp_path, yaml_body)), "--results-dir", str(results),
          "--max-parallelism", "1"])

    manifest = json.loads(
        next((results / "test_exp").glob("*.manifest.json")).read_text()
    )
    assert manifest["storage"]["backend"] == "local"


def test_cli_flag_overrides_everything(tmp_path):
    register_environment("fake", FakeGame, storage={"backend": "s3"})
    results = tmp_path / "results"

    main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
          "--max-parallelism", "1", "--storage-backend", "local"])

    manifest = json.loads(
        next((results / "test_exp").glob("*.manifest.json")).read_text()
    )
    assert manifest["storage"]["backend"] == "local"


def test_resolve_hook_output_reaches_the_game_and_the_trace(tmp_path):
    """The reproducibility path: derived config must survive into the trace."""

    def resolver(cfg):
        return {**cfg, "generated_scenario": [[1, 2], [3, 4]]}

    register_environment("fake", FakeGame, resolve_config=resolver)
    results = tmp_path / "results"
    main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
          "--max-parallelism", "1"])

    assert FakeGame.seen_configs[0]["generated_scenario"] == [[1, 2], [3, 4]]

    trace_path = next(
        p for p in (results / "test_exp").glob("*.json")
        if not p.name.endswith((".manifest.json", ".metadata.json"))
    )
    trace = json.loads(trace_path.read_text())
    assert trace["config"]["generated_scenario"] == [[1, 2], [3, 4]]


def test_dry_run_key_check_is_skipped_when_the_game_opts_out(tmp_path):
    """Negotiation's dry run uses heuristic agents; demanding keys would fail it."""
    body = BASIC.replace(
        "  num_agents: 2",
        "  num_agents: 2\n  agents:\n    - {type: llm, model: gpt-4o-mini}",
    )
    register_environment("fake", FakeGame, dry_run_checks_keys=False)
    rc = main([str(write_yaml(tmp_path, body)), "--results-dir", str(tmp_path / "r"),
               "--dry-run", "--max-parallelism", "1"])
    assert rc == 0


def test_dry_run_key_check_still_fires_for_games_that_want_it(tmp_path, monkeypatch):
    """Calendar's behavior must be preserved exactly."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = BASIC.replace(
        "  num_agents: 2",
        "  num_agents: 2\n  agents:\n    - {type: llm, model: gpt-4o-mini}",
    )
    register_environment("fake", FakeGame)  # default: checks keys
    rc = main([str(write_yaml(tmp_path, body)), "--results-dir", str(tmp_path / "r"),
               "--dry-run", "--max-parallelism", "1"])
    assert rc == 1, "missing API key must fail the dry run"


def test_dry_run_writes_nothing(tmp_path):
    register_environment("fake", FakeGame)
    results = tmp_path / "results"
    main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
          "--dry-run", "--max-parallelism", "1"])
    assert not results.exists() or not list(results.rglob("*.json"))


def test_resume_skips_completed_runs(tmp_path):
    register_environment("fake", FakeGame)
    results = tmp_path / "results"
    args = [str(write_yaml(tmp_path, BASIC)), "--results-dir", str(results),
            "--max-parallelism", "1"]

    main(args)
    assert len(FakeGame.seen_configs) == 2

    FakeGame.seen_configs = []
    main(args + ["--resume"])
    assert FakeGame.seen_configs == [], "completed runs must not re-execute"


def test_sharding_partitions_runs_without_overlap(tmp_path):
    register_environment("fake", FakeGame)
    body = BASIC.replace("count: 2", "count: 4")
    path = write_yaml(tmp_path, body)

    seen = []
    for shard in range(2):
        FakeGame.seen_configs = []
        main([str(path), "--results-dir", str(tmp_path / f"r{shard}"),
              "--max-parallelism", "1", "--shard-count", "2", "--shard-index", str(shard)])
        seen.extend(c["episode_id"] for c in FakeGame.seen_configs)

    assert len(seen) == 4
    assert len(set(seen)) == 4, "shards must not duplicate runs"


def test_a_game_returning_the_wrong_type_is_rejected(tmp_path):
    class BadGame:
        def __init__(self, config, dry_run=False):
            pass

        def run(self):
            return {"not": "a trace"}

    register_environment("fake", BadGame)
    rc = main([str(write_yaml(tmp_path, BASIC)), "--results-dir", str(tmp_path / "r"),
               "--max-parallelism", "1"])
    assert rc == 1


def test_live_run_preflights_credentials_before_executing_anything(tmp_path, monkeypatch, caplog):
    """Without this a missing key surfaces as a per-episode 'HTTP Error 401'
    from deep inside the client, after the run has already started."""
    import logging
    from expt_runner.run_experiment import _preflight_credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    contexts = [{"config": {"environment_id": "fake", "agents": [{"type": "llm", "model": "gpt-4o-mini"}]}}]

    with pytest.raises(EnvironmentError) as excinfo:
        _preflight_credentials(contexts)

    message = str(excinfo.value)
    assert "openai" in message and "gpt-4o-mini" in message and "agent 0" in message

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with caplog.at_level(logging.INFO):
        _preflight_credentials(contexts)


def test_preflight_ignores_agents_that_call_no_provider(monkeypatch):
    from expt_runner.run_experiment import _preflight_credentials

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _preflight_credentials([{"config": {"agents": [{"type": "heuristic"}, {"type": "scripted"}]}}])


def test_a_pool_name_in_model_is_refused_with_the_fix(monkeypatch, tmp_path):
    """`model:` and `participants:` are different namespaces. Left alone a pool
    name resolves to no provider and fails much later with an empty base URL."""
    from expt_runner.run_experiment import _preflight_credentials

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    contexts = [{"config": {"agents": [{"type": "llm", "model": "ds-flash"}]}}]

    with pytest.raises(EnvironmentError) as excinfo:
        _preflight_credentials(contexts)

    message = str(excinfo.value)
    assert "agent-pool name" in message
    assert "participants: [ds-flash" in message


def test_an_unrecognised_model_string_is_still_only_a_warning(monkeypatch, caplog):
    """A genuinely unknown model may be a custom endpoint; only a name the pool
    actually defines is the mistake worth stopping for."""
    import logging

    from expt_runner.run_experiment import _preflight_credentials

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    with caplog.at_level(logging.WARNING):
        _preflight_credentials([{"config": {"agents": [{"type": "llm", "model": "some-local-thing"}]}}])
    assert "unknown provider" in caplog.text
