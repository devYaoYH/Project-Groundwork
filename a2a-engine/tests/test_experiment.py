"""Seam tests: experiment expansion.

Two mechanics merged here and both change what config a environment actually receives:

- **preset inheritance** came from a2a-negotiation, whose precedence is
  preset -> defaults -> cell. Getting this order wrong silently changes every
  negotiation experiment.
- **resolve_config** is new. It runs once per cell, before fan-out, and its
  output must reach the trace — that is the whole reproducibility argument for
  stochastically generated scenarios.
"""

import textwrap

import pytest

from a2a_engine.experiment import (
    ExecutionPlan,
    expand_cells,
    load_experiment,
    resolve_presets,
)


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


# --- preset inheritance ------------------------------------------------------


def test_presets_resolve_extends_chain():
    resolved = resolve_presets({
        "base": {"num_rounds": 5, "talk": True},
        "long": {"extends": "base", "num_rounds": 10},
        "long_quiet": {"extends": "long", "talk": False},
    })
    assert resolved["long"] == {"num_rounds": 10, "talk": True}
    assert resolved["long_quiet"] == {"num_rounds": 10, "talk": False}
    assert resolved["base"] == {"num_rounds": 5, "talk": True}, "parent not mutated"


def test_preset_extending_a_later_preset_is_rejected():
    """Forward-only inheritance is what makes cycles unrepresentable."""
    with pytest.raises(ValueError, match="extends unknown preset"):
        resolve_presets({"a": {"extends": "b"}, "b": {}})


def test_preset_defaults_cell_precedence():
    """preset < defaults < cell. This ordering is inherited from negotiation."""
    spec = ExecutionPlan(
        name="e",
        presets={"p": {"a": "preset", "b": "preset", "c": "preset"}},
        defaults={"preset": "p", "b": "defaults", "c": "defaults"},
        cells=[{"label": "x", "count": 1, "config": {"c": "cell"}}],
    )
    _, resolved = expand_cells(spec)[0]
    assert resolved["a"] == "preset"
    assert resolved["b"] == "defaults"
    assert resolved["c"] == "cell"


def test_cell_can_override_the_default_preset():
    spec = ExecutionPlan(
        name="e",
        presets={"p1": {"v": 1}, "p2": {"v": 2}},
        defaults={"preset": "p1"},
        cells=[
            {"label": "keep", "count": 1},
            {"label": "switch", "count": 1, "config": {"preset": "p2"}},
        ],
    )
    results = {b.label: cfg for b, cfg in expand_cells(spec)}
    assert results["keep"]["v"] == 1
    assert results["switch"]["v"] == 2
    # The name is retained so the trace records which preset was used.
    assert results["switch"]["preset"] == "p2"


def test_unknown_preset_fails_loudly():
    spec = ExecutionPlan(
        name="e", cells=[{"label": "x", "count": 1, "config": {"preset": "ghost"}}]
    )
    with pytest.raises(ValueError, match="Unknown preset 'ghost'"):
        expand_cells(spec)


def test_merge_is_deep_not_shallow():
    """Agent-level fields must not be clobbered wholesale by a partial override."""
    spec = ExecutionPlan(
        name="e",
        defaults={"opts": {"temperature": 0.0, "max_tokens": 1024}},
        cells=[{"label": "x", "count": 1, "config": {"opts": {"temperature": 1.0}}}],
    )
    _, resolved = expand_cells(spec)[0]
    assert resolved["opts"] == {"temperature": 1.0, "max_tokens": 1024}


# --- presets file discovery --------------------------------------------------


def test_sibling_presets_file_is_discovered(tmp_path):
    """a2a-negotiation's YAMLs reference presets without naming the file."""
    write(tmp_path, "presets.yaml", """
        presets:
          shared: {num_rounds: 9}
    """)
    path = write(tmp_path, "exp.yaml", """
        name: e
        defaults: {preset: shared}
        cells:
          - {label: x, count: 1}
    """)
    spec = load_experiment(path)
    _, resolved = expand_cells(spec)[0]
    assert resolved["num_rounds"] == 9


def test_inline_presets_override_the_shared_file(tmp_path):
    write(tmp_path, "presets.yaml", """
        presets:
          shared: {num_rounds: 9}
    """)
    path = write(tmp_path, "exp.yaml", """
        name: e
        presets:
          shared: {num_rounds: 99}
        defaults: {preset: shared}
        cells:
          - {label: x, count: 1}
    """)
    _, resolved = expand_cells(load_experiment(path))[0]
    assert resolved["num_rounds"] == 99


def test_experiment_without_presets_still_loads(tmp_path):
    """The pre-merge framework YAML shape must keep working untouched."""
    path = write(tmp_path, "exp.yaml", """
        name: calendar_example
        defaults: {environment_id: calendar, num_slots: 16}
        cells:
          - {label: low, count: 5, config: {seed: 1, density: 0.3}}
    """)
    spec = load_experiment(path)
    cell, resolved = expand_cells(spec)[0]
    assert cell.count == 5
    assert resolved == {"environment_id": "calendar", "num_slots": 16, "seed": 1, "density": 0.3}


def test_storage_block_is_parsed(tmp_path):
    path = write(tmp_path, "exp.yaml", """
        name: e
        storage: {backend: firestore, collection: games}
        defaults: {environment_id: g}
        cells:
          - {label: x, count: 1}
    """)
    assert load_experiment(path).storage == {"backend": "firestore", "collection": "games"}


# --- resolve_config hook -----------------------------------------------------


def test_resolve_config_output_reaches_the_run_config():
    """Derived scenario data must land in the config, hence in the trace."""

    def resolver(cfg):
        out = dict(cfg)
        out["agent_projects"] = [["generated"]]
        out.pop("mc_ratio", None)
        return out

    spec = ExecutionPlan(
        name="e", cells=[{"label": "x", "count": 3, "config": {"mc_ratio": 0.8}}]
    )
    _, resolved = expand_cells(spec, resolve_config=resolver)[0]
    assert resolved["agent_projects"] == [["generated"]]
    assert "mc_ratio" not in resolved


def test_resolve_config_runs_once_per_cell_not_once_per_run():
    """All runs in a cell must share one scenario, or the cell isn't a cell."""
    calls = []

    def resolver(cfg):
        calls.append(cfg)
        return cfg

    spec = ExecutionPlan(
        name="e",
        cells=[
            {"label": "a", "count": 10},
            {"label": "b", "count": 5},
        ],
    )
    expand_cells(spec, resolve_config=resolver)
    assert len(calls) == 2


def test_resolve_config_sees_the_fully_merged_config():
    """The hook must run after preset/defaults/cell merging, not before."""
    seen = {}

    def resolver(cfg):
        seen.update(cfg)
        return cfg

    spec = ExecutionPlan(
        name="e",
        presets={"p": {"resource_types": ["wood"]}},
        defaults={"preset": "p", "agent_budget": 10},
        cells=[{"label": "x", "count": 1, "config": {"mc_ratio": 0.5}}],
    )
    expand_cells(spec, resolve_config=resolver)
    assert seen["resource_types"] == ["wood"]
    assert seen["agent_budget"] == 10
    assert seen["mc_ratio"] == 0.5


def test_no_hook_leaves_config_untouched():
    spec = ExecutionPlan(name="e", cells=[{"label": "x", "count": 1, "config": {"k": 1}}])
    _, resolved = expand_cells(spec, resolve_config=None)[0]
    assert resolved == {"k": 1}


def test_participant_count_must_match_the_environment_s_declared_slots(tmp_path):
    """Silently truncating or padding a line-up would change what ran without
    saying so."""
    from pathlib import Path

    import pytest

    from a2a_engine.experiment import load_experiment

    workspace = Path(__file__).resolve().parents[2]
    probe = workspace / "games/calendar/experiments/_count_probe.yaml"
    probe.write_text(
        "schema_version: 1\nname: probe\n"
        "release: ../environments/calendar_tiny_v1.yaml\n"
        "participants: [gpt-mini, haiku]\n"
        "episodes: [{label: x, count: 1, seeds: [1]}]\n"
    )
    try:
        with pytest.raises(ValueError, match="5 agent slots"):
            load_experiment(probe)
    finally:
        probe.unlink()


def test_a_pool_backed_experiment_hydrates_to_concrete_models():
    """What the runner and the trace see must be fully resolved, so a config
    naming participants is indistinguishable downstream from one naming models."""
    from pathlib import Path

    from a2a_engine.experiment import load_experiment

    workspace = Path(__file__).resolve().parents[2]
    spec = load_experiment(workspace / "games/word-guess/experiments/pool_example.yaml")

    models = [agent["model"] for agent in spec.defaults["agents"]]
    assert models == ["gpt-5-mini", "claude-haiku-4-5-20251001"]
    assert "participants" not in spec.defaults
