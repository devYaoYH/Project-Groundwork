"""Typed, local-first release and episode configuration.

``ReleaseDeclaration`` is the stable description of a world; ``ExperimentConfig``
selects agents and expands it into concrete episodes.  Both are deliberately
declarative in v0.  They record adapter intent but do not yet enforce topology
or transport policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputConfig(_StrictModel):
    """A portable, local input reference pinned by content digest."""

    id: str
    path: str
    sha256: str

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("id must start with a lowercase letter and contain only [a-z0-9_.-]")
        return value

    @field_validator("path")
    @classmethod
    def _local_relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or "://" in value:
            raise ValueError("v1 inputs must use a local relative path, not an absolute path or URI")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        value = value.lower()
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return value


class EngineConfig(_StrictModel):
    """The installed environment implementation and its release-owned defaults."""

    environment_id: str
    defaults: dict[str, Any] = Field(default_factory=dict)


class RoleConfig(_StrictModel):
    id: str
    count: int = Field(default=1, ge=1)
    description: str = ""


class ResourceConfig(_StrictModel):
    id: str
    kind: str
    description: str = ""


class MeasureConfig(_StrictModel):
    """A declared metric; computation remains environment/extractor-owned."""

    name: str
    producer: Literal["environment", "derived"]
    scope: Literal["episode", "participant"] = "episode"
    unit: str = ""
    direction: Literal["maximize", "minimize", "neutral"] = "neutral"
    extractor: str | None = None

    @model_validator(mode="after")
    def _derived_metrics_name_an_extractor(self) -> "MeasureConfig":
        if self.producer == "derived" and not self.extractor:
            raise ValueError("a derived metric requires an extractor identifier")
        if self.producer == "environment" and self.extractor is not None:
            raise ValueError("a environment-produced metric must not name an extractor")
        return self


class AdapterBindingsConfig(_StrictModel):
    """Named adapters pinned for comparison; games adopt them component by component."""

    model: str = "engine.llm"
    communication: str = "local.in_process"
    resources: dict[str, str] = Field(default_factory=dict)


class ReleaseDeclaration(_StrictModel):
    """Versioned, declarative coordination release."""

    schema_version: Literal[1] = 1
    id: str
    release: str
    description: str = ""
    engine: EngineConfig
    inputs: list[InputConfig] = Field(default_factory=list)
    roles: list[RoleConfig] = Field(default_factory=list)
    resources: list[ResourceConfig] = Field(default_factory=list)
    metrics: list[MeasureConfig] = Field(default_factory=list)
    adapter_bindings: AdapterBindingsConfig = Field(default_factory=AdapterBindingsConfig)

    @field_validator("id")
    @classmethod
    def _environment_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("release id must start with a lowercase letter and contain only [a-z0-9_.-]")
        return value

    @model_validator(mode="after")
    def _unique_declarations(self) -> "ReleaseDeclaration":
        for name, values in (("input", self.inputs), ("role", self.roles),
                             ("resource", self.resources), ("metric", self.metrics)):
            ids = [item.id if hasattr(item, "id") else item.name for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} identifiers must be unique")
        resource_ids = {resource.id for resource in self.resources}
        unknown = set(self.adapter_bindings.resources) - resource_ids
        if unknown:
            raise ValueError(f"adapter bindings reference undeclared resources: {sorted(unknown)}")
        return self

    def content_sha256(self) -> str:
        """Digest the declarative release, including declared input hashes."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AgentConfig(_StrictModel):
    role: str | None = None
    type: str = "llm"
    model: str | None = None
    api_format: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    def as_game_config(self) -> dict[str, Any]:
        base = self.model_dump(exclude={"role", "config"}, exclude_none=True)
        return {**base, **self.config}


class EpisodePlanConfig(_StrictModel):
    label: str
    count: int = Field(default=1, ge=1)
    seeds: list[int] | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _seeds_match_count(self) -> "EpisodePlanConfig":
        if self.seeds is not None and len(self.seeds) != self.count:
            raise ValueError("seeds must contain exactly count values")
        return self

    def expanded_seeds(self) -> list[int]:
        return list(self.seeds) if self.seeds is not None else list(range(self.count))


class StorageConfig(BaseModel):
    """Typed common storage surface; backend-specific keys remain extensible."""

    model_config = ConfigDict(extra="allow")
    backend: Literal["sqlite", "local", "s3", "firestore"] = "sqlite"
    path: str | None = None


class ObservabilityConfig(_StrictModel):
    capture_content: bool = True
    local_spans_path: str | None = None


class ExperimentConfig(_StrictModel):
    """Configures concrete episodes of one ReleaseDeclaration."""

    schema_version: Literal[1] = 1
    name: str
    release: str
    description: str = ""
    agents: list[AgentConfig] = Field(default_factory=list)
    # Positional logical names resolved against the agent pool. An alternative
    # to `agents`, never a supplement: two sources for one line-up is a silent
    # divergence waiting to happen.
    participants: list[str] = Field(default_factory=list)
    episodes: list[EpisodePlanConfig] = Field(min_length=1)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def _one_source_of_agents(self) -> "ExperimentConfig":
        if self.agents and self.participants:
            raise ValueError(
                "declare either 'agents' (already hydrated) or 'participants' "
                "(resolved from the agent pool), not both"
            )
        return self

    @field_validator("release")
    @classmethod
    def _environment_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or "://" in value:
            raise ValueError("v1 release must be a local relative path")
        return value


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one YAML mapping")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_release_declaration(path: str | Path) -> ReleaseDeclaration:
    """Validate an release file and verify every declared local input."""
    path = Path(path).resolve()
    config = ReleaseDeclaration.model_validate(_read_yaml(path))
    for input_config in config.inputs:
        input_path = (path.parent / input_config.path).resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"release input {input_config.id!r} does not exist: {input_path}")
        actual = _sha256_file(input_path)
        if actual != input_config.sha256:
            raise ValueError(
                f"release input {input_config.id!r} digest mismatch: "
                f"expected {input_config.sha256}, got {actual}"
            )
    return config


def load_experiment_config(path: str | Path) -> tuple[ExperimentConfig, ReleaseDeclaration]:
    """Validate an experiment and its referenced local release."""
    path = Path(path).resolve()
    experiment = ExperimentConfig.model_validate(_read_yaml(path))
    release = load_release_declaration(path.parent / experiment.release)
    _validate_agent_roles(experiment, release)
    return experiment, release


def _validate_agent_roles(experiment: ExperimentConfig, release: ReleaseDeclaration) -> None:
    """Keep the typed agent list consistent with the release's roles.

    Legacy experiment YAML can retain its historical loose agent list.  In the
    v1 pair, roles are part of the portable release contract: an explicit
    agent list must name every role instance it configures.  When a environment builds
    its own agents (the empty-list case), its ``num_agents`` default must still
    agree with the declared role cardinality.
    """
    if not release.roles:
        return
    expected = {role.id: role.count for role in release.roles}
    if not experiment.agents:
        configured_count = release.engine.defaults.get("num_agents")
        if configured_count is not None and configured_count != sum(expected.values()):
            raise ValueError(
                "release engine.defaults.num_agents must equal the declared role count "
                "when ExperimentConfig.agents is empty"
            )
        return
    missing = [index for index, agent in enumerate(experiment.agents) if not agent.role]
    if missing:
        raise ValueError(
            "each explicit experiment agent must name an release role; "
            f"missing role at indexes {missing}"
        )
    actual: dict[str, int] = {}
    for agent in experiment.agents:
        assert agent.role is not None
        actual[agent.role] = actual.get(agent.role, 0) + 1
    unknown = sorted(set(actual) - set(expected))
    if unknown:
        raise ValueError(f"experiment agents reference undeclared roles: {unknown}")
    if actual != expected:
        raise ValueError(
            "explicit experiment-agent role counts must equal release role counts: "
            f"expected {expected}, got {actual}"
        )
