"""EpisodeManifest — the reproducibility contract for a single environment run.

Previously this was an ad-hoc dict built inline in ``expt_runner.run_experiment``.
Promoting it to a schema makes the rule enforceable:

    Anything that changes model behavior must be in the resolved config, and
    the resolved config is recorded in the trace.

The manifest is what lets a run be re-created later: it pins the code
(``git_hash``, package versions), the inputs (``resolved_config_hash``,
``dataset_sha256``, ``task_id``, ``seed``, ``prompt_variant_id``), and the
agents (models + decoding params).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import socket
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_SCHEMA_VERSION = 3


def config_hash(config: dict[str, Any]) -> str:
    """Stable hash of a resolved run config.

    API keys are excluded: they are credentials, not experimental variables, and
    including them would make the hash differ between collaborators running the
    identical experiment.
    """
    redacted = redact_config(config)
    blob = json.dumps(redacted, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


_SECRET_KEYS = {"api_key", "apikey", "secret", "password", "token"}


def redact_config(config: Any) -> Any:
    """Recursively drop credential-shaped keys from a config tree."""
    if isinstance(config, dict):
        return {
            k: redact_config(v)
            for k, v in config.items()
            if k.lower() not in _SECRET_KEYS
        }
    if isinstance(config, list):
        return [redact_config(v) for v in config]
    return config


def file_sha256(path: str | Path) -> str | None:
    """Content hash of a dataset file. A filename is not a fingerprint."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_version(name: str) -> str | None:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        return None


def git_hash(cwd: str | Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


class ParticipantManifest(BaseModel):
    """Per-agent record of what actually drove the model."""

    model_config = ConfigDict(extra="allow")

    type: str | None = None
    model: str | None = None
    api_format: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class StorageManifest(BaseModel):
    """Where the trace was persisted, and whether that succeeded."""

    model_config = ConfigDict(extra="allow")

    backend: str = "local"
    uri: str | None = None
    status: str = "pending"  # pending | written | failed | not_configured
    error: str | None = None


class EpisodeManifest(BaseModel):
    """Everything needed to identify, locate, and reproduce one run."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = MANIFEST_SCHEMA_VERSION

    # --- identity ---
    experiment_name: str
    episode_id: str
    cell_id: str
    episode_idx: int
    environment_id: str
    episode_uid: str

    # --- provenance ---
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    git_hash: str | None = None
    a2a_engine_version: str | None = None
    game_package_version: str | None = None
    hostname: str | None = None
    local_user: str | None = None

    # --- inputs ---
    resolved_config_hash: str | None = None
    seed: int | None = None
    dataset_path: str | None = None
    dataset_sha256: str | None = None
    task_id: str | None = None
    prompt_variant_id: str | None = None
    release_id: str | None = None
    release_version: str | None = None
    release_content_sha256: str | None = None

    # --- agents ---
    num_agents: int | None = None
    agents: list[ParticipantManifest] = Field(default_factory=list)

    # --- storage ---
    local_trace_path: str | None = None
    trace_size_bytes: int | None = None
    storage: StorageManifest = Field(default_factory=StorageManifest)

    @classmethod
    def from_run(
        cls,
        *,
        config: dict[str, Any],
        experiment_name: str,
        cell_id: str,
        episode_idx: int,
        episode_uid: str,
        game_package: str | None = None,
        repo_root: str | Path | None = None,
    ) -> EpisodeManifest:
        """Build a manifest from a resolved run config.

        ``dataset_path`` is resolved relative to the process cwd, matching how
        games themselves open ``task_path``.
        """
        agents = [
            ParticipantManifest(**_agent_fields(spec)) for spec in config.get("agents", [])
        ]
        dataset_path = config.get("task_path") or config.get("dataset_path")
        release = config.get("release") if isinstance(config.get("release"), dict) else {}
        return cls(
            experiment_name=experiment_name,
            episode_id=str(config.get("episode_id") or ""),
            cell_id=cell_id,
            episode_idx=episode_idx,
            environment_id=str(config.get("environment_id") or ""),
            episode_uid=episode_uid,
            git_hash=config.get("git_hash") or git_hash(repo_root),
            a2a_engine_version=package_version("a2a-engine"),
            game_package_version=package_version(game_package) if game_package else None,
            hostname=socket.gethostname(),
            local_user=_safe_user(),
            resolved_config_hash=config_hash(config),
            seed=config.get("seed"),
            dataset_path=str(dataset_path) if dataset_path else None,
            dataset_sha256=file_sha256(dataset_path) if dataset_path else None,
            task_id=config.get("task_id"),
            prompt_variant_id=config.get("prompt_variant") or config.get("prompt_variant_id"),
            release_id=release.get("id"),
            release_version=release.get("release"),
            release_content_sha256=release.get("content_sha256"),
            num_agents=config.get("num_agents"),
            agents=agents,
        )


def _safe_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:
        return None


def _agent_fields(spec: Any) -> dict[str, Any]:
    """Pull the behavior-relevant fields off an agent spec (dict or model)."""
    if not isinstance(spec, dict):
        spec = getattr(spec, "model_dump", lambda: {})()
    keep = ("type", "model", "api_format", "temperature", "max_tokens")
    return {k: spec.get(k) for k in keep if spec.get(k) is not None}
