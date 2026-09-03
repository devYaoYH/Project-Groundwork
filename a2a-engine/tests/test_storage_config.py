"""Tests for the ``storage:`` config layer: named sinks, inheritance, env vars.

This is config-as-code, so the failure modes that matter are silent ones — a
typo'd sink name resolving to the default, or an unset credential becoming the
literal string "${VAR}" and being handed to a cloud SDK.
"""

from __future__ import annotations

import textwrap

import pytest

from a2a_engine.experiment import (
    ExecutionPlan,
    expand_env,
    load_experiment,
    resolve_storage,
)


def write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


# Pre-dedented: several tests concatenate fragments onto this, and dedent() on a
# mix of indented and column-0 text would find no common prefix and no-op.
BASE = textwrap.dedent("""
    name: demo
    defaults:
      environment_id: word_guess
      num_agents: 2
    cells:
      - label: a
        config: {seed: 1}
""")


# --- env interpolation ------------------------------------------------------


def test_expands_set_variables(monkeypatch):
    monkeypatch.setenv("MY_BUCKET", "lab-episodes")
    assert expand_env({"bucket": "${MY_BUCKET}"}) == {"bucket": "lab-episodes"}


def test_uses_fallback_when_unset(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert expand_env("${NOPE:-default-value}") == "default-value"


def test_empty_fallback_is_allowed(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert expand_env("${NOPE:-}") == ""


def test_unset_variable_without_fallback_is_an_error(monkeypatch):
    """Better a loud failure than handing '${VAR}' to boto3 as a bucket name."""
    monkeypatch.delenv("MISSING_THING", raising=False)
    with pytest.raises(ValueError, match="MISSING_THING"):
        expand_env({"bucket": "${MISSING_THING}"})


def test_expansion_recurses_into_nested_structures(monkeypatch):
    monkeypatch.setenv("P", "proj")
    out = expand_env({"a": ["${P}", {"b": "${P}"}]})
    assert out == {"a": ["proj", {"b": "proj"}]}


# --- named sinks ------------------------------------------------------------


def test_sink_is_loaded_from_sibling_storage_yaml(tmp_path):
    write(tmp_path, "storage.yaml", """
        sinks:
          local_db:
            backend: sqlite
            path: ./results/x.db
    """)
    path = write(tmp_path, "e.yaml", BASE + "storage:\n  extends: local_db\n")
    spec = load_experiment(path)
    assert resolve_storage(spec) == {
        "backend": "sqlite", "path": "./results/x.db", "sink": "local_db",
    }


def test_inline_storage_keys_override_the_sink(tmp_path):
    write(tmp_path, "storage.yaml", """
        sinks:
          local_db: {backend: sqlite, path: ./results/x.db}
    """)
    path = write(
        tmp_path, "e.yaml",
        BASE + "storage:\n  extends: local_db\n  path: ./results/override.db\n",
    )
    assert resolve_storage(load_experiment(path))["path"] == "./results/override.db"


def test_sinks_support_single_inheritance(tmp_path):
    write(tmp_path, "storage.yaml", """
        sinks:
          base:  {backend: sqlite, path: ./a.db}
          child: {extends: base, mirror_json: true}
    """)
    path = write(tmp_path, "e.yaml", BASE + "\nstorage:\n  extends: child\n")
    resolved = resolve_storage(load_experiment(path))
    assert resolved["backend"] == "sqlite"
    assert resolved["path"] == "./a.db"
    assert resolved["mirror_json"] is True


def test_sink_extending_an_undefined_parent_is_an_error(tmp_path):
    write(tmp_path, "storage.yaml", """
        sinks:
          child: {extends: nonexistent, backend: sqlite}
    """)
    with pytest.raises(ValueError, match="unknown sink"):
        load_experiment(write(tmp_path, "e.yaml", BASE))


def test_unknown_sink_name_is_an_error_not_a_silent_default(tmp_path):
    path = write(tmp_path, "e.yaml", BASE + "\nstorage:\n  extends: typo_db\n")
    with pytest.raises(ValueError, match="unknown sink 'typo_db'"):
        resolve_storage(load_experiment(path))


def test_bare_string_storage_is_shorthand_for_extends(tmp_path):
    write(tmp_path, "storage.yaml", """
        sinks:
          local_db: {backend: sqlite, path: ./x.db}
    """)
    path = write(tmp_path, "e.yaml", BASE + "\nstorage: local_db\n")
    assert resolve_storage(load_experiment(path))["backend"] == "sqlite"


# --- precedence -------------------------------------------------------------


def test_game_default_applies_when_experiment_is_silent():
    spec = ExecutionPlan(name="d", defaults={"environment_id": "calendar"})
    resolved = resolve_storage(spec, game_default={"backend": "s3", "prefix": "cal"})
    assert resolved == {"backend": "s3", "prefix": "cal"}


def test_switching_backend_drops_the_game_default_entirely():
    """A sqlite sink must not inherit calendar's S3 prefix."""
    spec = ExecutionPlan(name="d", storage={"backend": "sqlite", "path": "./x.db"})
    resolved = resolve_storage(spec, game_default={"backend": "s3", "prefix": "cal"})
    assert resolved == {"backend": "sqlite", "path": "./x.db"}
    assert "prefix" not in resolved


def test_same_backend_merges_with_the_game_default():
    spec = ExecutionPlan(name="d", storage={"bucket": "mine"})
    resolved = resolve_storage(spec, game_default={"backend": "s3", "prefix": "cal"})
    assert resolved == {"backend": "s3", "prefix": "cal", "bucket": "mine"}


def test_caller_overrides_beat_everything():
    spec = ExecutionPlan(name="d", storage={"backend": "sqlite", "path": "./a.db"})
    resolved = resolve_storage(spec, overrides={"path": "./cli.db"})
    assert resolved["path"] == "./cli.db"


def test_env_expansion_runs_after_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DB", "/tmp/from-env.db")
    write(tmp_path, "storage.yaml", """
        sinks:
          local_db: {backend: sqlite, path: "${DB:-./default.db}"}
    """)
    path = write(tmp_path, "e.yaml", BASE + "\nstorage:\n  extends: local_db\n")
    assert resolve_storage(load_experiment(path))["path"] == "/tmp/from-env.db"


# --- multi-environment experiments -------------------------------------------------


def test_environment_ids_collects_per_cell_overrides():
    spec = ExecutionPlan(
        name="d",
        defaults={"environment_id": "word_guess"},
        cells=[
            {"label": "a", "config": {}},
            {"label": "b", "config": {"environment_id": "buyer_seller"}},
            {"label": "c", "config": {"environment_id": "buyer_seller"}},
        ],
    )
    assert spec.environment_ids() == ["word_guess", "buyer_seller"]


def test_environment_ids_falls_back_to_defaults_with_no_cells():
    spec = ExecutionPlan(name="d", defaults={"environment_id": "calendar"})
    assert spec.environment_ids() == ["calendar"]
