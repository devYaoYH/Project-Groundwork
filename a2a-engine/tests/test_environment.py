"""Contract tests for the additive typed release/experiment YAML format."""

from __future__ import annotations

import hashlib
import textwrap

import pytest

from a2a_engine.environment import load_release_declaration, load_experiment_config
from a2a_engine.experiment import expand_cells, load_experiment


def _write(path, body: str):
    path.write_text(textwrap.dedent(body))
    return path


def _environment(tmp_path, digest: str):
    return _write(tmp_path / "release.yaml", f"""
        schema_version: 1
        id: demo.calendar
        release: v1
        engine:
          environment_id: fake
          defaults: {{num_agents: 2, task_path: task.json}}
        inputs:
          - id: task
            path: task.json
            sha256: "{digest}"
        roles:
          - id: participant
            count: 2
        resources:
          - id: calendar
            kind: schedule
        metrics:
          - name: utility
            producer: environment
            direction: maximize
        adapter_bindings:
          model: engine.llm
          communication: local.in_process
          resources: {{calendar: fixture.schedule}}
    """)


def test_environment_validates_pinned_local_input_and_has_stable_identity(tmp_path):
    task = _write(tmp_path / "task.json", '{"scenario": "one"}')
    config = load_release_declaration(_environment(tmp_path, hashlib.sha256(task.read_bytes()).hexdigest()))

    assert config.id == "demo.calendar"
    assert config.content_sha256() == config.content_sha256()
    assert config.inputs[0].sha256 == hashlib.sha256(task.read_bytes()).hexdigest()


def test_environment_rejects_mismatched_or_nonlocal_inputs(tmp_path):
    _write(tmp_path / "task.json", "actual")
    path = _environment(tmp_path, "0" * 64)
    with pytest.raises(ValueError, match="digest mismatch"):
        load_release_declaration(path)

    path.write_text(path.read_text().replace("path: task.json", "path: /outside.json"))
    with pytest.raises(ValueError, match="local relative path"):
        load_release_declaration(path)


def test_typed_experiment_normalizes_to_existing_execution_cells(tmp_path):
    task = _write(tmp_path / "task.json", "task")
    _environment(tmp_path, hashlib.sha256(task.read_bytes()).hexdigest())
    experiment_path = _write(tmp_path / "experiment.yaml", """
        schema_version: 1
        name: typed_demo
        release: release.yaml
        agents:
          - role: participant
            model: test-model
            temperature: 0.2
          - role: participant
            model: test-model
            temperature: 0.2
        episodes:
          - label: baseline
            count: 2
            seeds: [7, 9]
        storage: {backend: sqlite, path: episodes.db}
        observability: {capture_content: true, local_spans_path: spans.jsonl}
    """)

    typed, release = load_experiment_config(experiment_path)
    assert typed.name == "typed_demo"
    assert release.release == "v1"

    spec = load_experiment(experiment_path)
    expanded = expand_cells(spec)
    assert [cell.label for cell, _ in expanded] == ["baseline-0", "baseline-1"]
    assert [config["seed"] for _, config in expanded] == [7, 9]
    assert expanded[0][1]["release"]["id"] == "demo.calendar"
    assert expanded[0][1]["release"]["content_sha256"] == release.content_sha256()
    assert spec.storage == {"backend": "sqlite", "path": "episodes.db"}
    assert spec.observability and spec.observability["capture_content"] is True


def test_typed_experiment_rejects_episode_seed_length_mismatch(tmp_path):
    _write(tmp_path / "task.json", "task")
    _environment(tmp_path, hashlib.sha256(b"task").hexdigest())
    path = _write(tmp_path / "experiment.yaml", """
        name: invalid
        release: release.yaml
        episodes: [{label: baseline, count: 2, seeds: [1]}]
    """)
    with pytest.raises(ValueError, match="exactly count"):
        load_experiment_config(path)


def test_typed_experiment_validates_explicit_agent_roles(tmp_path):
    task = _write(tmp_path / "task.json", "task")
    _environment(tmp_path, hashlib.sha256(task.read_bytes()).hexdigest())
    path = _write(tmp_path / "experiment.yaml", """
        name: invalid_roles
        release: release.yaml
        agents: [{role: participant, model: test-model}]
        episodes: [{label: baseline}]
    """)
    with pytest.raises(ValueError, match="role counts"):
        load_experiment_config(path)

    path.write_text(path.read_text().replace("role: participant", "role: missing"))
    with pytest.raises(ValueError, match="undeclared roles"):
        load_experiment_config(path)


def test_participants_and_agents_cannot_both_be_declared():
    """Two sources for one line-up is a silent divergence waiting to happen."""
    import pytest
    from pydantic import ValidationError

    from a2a_engine.environment import ExperimentConfig

    with pytest.raises(ValidationError, match="not both"):
        ExperimentConfig(
            name="x", release="../environments/e.yaml",
            agents=[{"type": "llm", "model": "gpt-5-mini"}],
            participants=["gpt-mini"],
            episodes=[{"label": "a", "count": 1, "seeds": [1]}],
        )
