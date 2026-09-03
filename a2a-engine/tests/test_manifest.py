"""Seam tests: the EpisodeManifest reproducibility contract.

The manifest replaces an ad-hoc metadata dict that used to be assembled inline in
expt-runner. These tests pin the properties the contract is *for*: the same
experiment hashes the same everywhere, credentials never leak into the hash or
the file, and a dataset is identified by content rather than filename.
"""

import json

from a2a_engine.manifest import (
    MANIFEST_SCHEMA_VERSION,
    EpisodeManifest,
    config_hash,
    file_sha256,
    redact_config,
)


BASE_CONFIG = {
    "environment_id": "calendar",
    "episode_id": "exp.cell.0",
    "num_agents": 2,
    "seed": 42,
    "agents": [
        {"type": "llm", "model": "gpt-4o-mini", "temperature": 0.0, "api_key": "sk-secret"},
        {"type": "llm", "model": "claude-sonnet-4-6", "api_format": "vertexai_anthropic"},
    ],
}


def build(config=None, **kw):
    kw.setdefault("experiment_name", "exp")
    kw.setdefault("cell_id", "cell")
    kw.setdefault("episode_idx", 0)
    kw.setdefault("episode_uid", "gid")
    return EpisodeManifest.from_run(config=config or BASE_CONFIG, **kw)


# --- config hashing ----------------------------------------------------------


def test_config_hash_is_order_independent():
    """Two collaborators writing the same YAML must get the same hash."""
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_config_hash_ignores_api_keys():
    """Keys are credentials, not experimental variables."""
    with_key = {"model": "gpt-4o-mini", "api_key": "sk-alice"}
    other_key = {"model": "gpt-4o-mini", "api_key": "sk-bob"}
    assert config_hash(with_key) == config_hash(other_key)


def test_config_hash_tracks_behavioral_changes():
    assert config_hash({"temperature": 0.0}) != config_hash({"temperature": 1.0})


def test_redaction_is_recursive():
    cleaned = redact_config({"agents": [{"model": "m", "api_key": "sk-x", "token": "t"}]})
    assert cleaned == {"agents": [{"model": "m"}]}


def test_manifest_never_serializes_a_key():
    """A manifest is uploaded to shared storage; it must be safe to share."""
    blob = build().model_dump_json()
    assert "sk-secret" not in blob


# --- dataset identity --------------------------------------------------------


def test_dataset_is_hashed_by_content(tmp_path):
    """A filename is not a fingerprint — task files get edited in place."""
    task_file = tmp_path / "tasks.jsonl"
    task_file.write_text('{"id": 1}\n')
    first = build({**BASE_CONFIG, "task_path": str(task_file)}).dataset_sha256

    task_file.write_text('{"id": 2}\n')
    second = build({**BASE_CONFIG, "task_path": str(task_file)}).dataset_sha256

    assert first and second and first != second


def test_missing_dataset_hashes_to_none_without_raising():
    m = build({**BASE_CONFIG, "task_path": "/nonexistent/tasks.jsonl"})
    assert m.dataset_path == "/nonexistent/tasks.jsonl"
    assert m.dataset_sha256 is None


def test_file_sha256_on_a_directory_is_none(tmp_path):
    assert file_sha256(tmp_path) is None


# --- agent capture -----------------------------------------------------------


def test_agents_capture_behavioral_params_only():
    m = build()
    assert [a.model for a in m.agents] == ["gpt-4o-mini", "claude-sonnet-4-6"]
    assert m.agents[0].temperature == 0.0
    assert m.agents[1].api_format == "vertexai_anthropic"
    assert not hasattr(m.agents[0], "api_key") or m.agents[0].model_dump().get("api_key") is None


def test_manifest_survives_a_config_with_no_agents():
    m = build({"environment_id": "g", "episode_id": "e.b.0"})
    assert m.agents == []
    assert m.resolved_config_hash


# --- identity and provenance -------------------------------------------------


def test_manifest_carries_identity_and_versions():
    m = build()
    assert m.schema_version == MANIFEST_SCHEMA_VERSION
    assert (m.experiment_name, m.cell_id, m.episode_idx) == ("exp", "cell", 0)
    assert m.episode_id == "exp.cell.0"
    assert m.environment_id == "calendar"
    assert m.seed == 42
    assert m.a2a_engine_version, "engine version must be recorded"


def test_explicit_git_hash_in_config_wins():
    """The runner stamps the hash once so all shards agree, even off-repo."""
    m = build({**BASE_CONFIG, "git_hash": "deadbeef"})
    assert m.git_hash == "deadbeef"


def test_manifest_round_trips_through_json():
    m = build()
    restored = EpisodeManifest.model_validate(json.loads(m.model_dump_json()))
    assert restored.resolved_config_hash == m.resolved_config_hash
    assert [a.model for a in restored.agents] == [a.model for a in m.agents]
