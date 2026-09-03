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


class ParameterConfig(_StrictModel):
    """One environment setting the design layer may describe.

    ``source`` states the oracle boundary, not just where a config value is
    read. An ``item`` parameter is one whose change changes that item's pinned
    oracle ground truth; it is frozen in the item bank beside that result. A
    ``design`` parameter cannot affect the pinned oracle result. A design sets
    a design parameter in the episode config, while a design *selects* on an
    item parameter — factoring on it restricts the bank to matching rows rather
    than overwriting a config key the loaded item would ignore.

    Item-bank oracle fields must therefore be ground truth determined by frozen
    item attributes alone, never by a design-sourced setting. Runtime metrics
    may combine the selected item with design settings after the episode starts,
    but those derived values do not belong in the pinned bank oracle result.

    An item parameter therefore must not hand-write a ``domain``: its levels
    are whatever the pinned bank actually contains, projected at read time by
    :func:`a2a_engine.items.derive_item_domain`.  Hand-writing them is how the
    declaration and the bank drift apart.

    The declaration does not say whether a parameter *ought* to be an
    experimental axis.  That is a judgement about a particular comparison and
    belongs to whoever writes the design, which states a disposition —
    factor, pin, or randomize — per parameter.  ``fixed`` is the one
    release-side veto and it is absolute: a design that sets a fixed parameter
    is rejected.
    """

    name: str
    type: Literal["continuous", "integer", "categorical", "boolean"]
    domain: list[Any] | tuple[float, float] | None = None
    fixed: bool = False
    source: Literal["design", "item"] = "design"
    item_key: str | None = None          # bank column, when it is not ``name``

    @model_validator(mode="after")
    def _domain_matches_type(self) -> "ParameterConfig":
        if not self.name:
            raise ValueError("parameter name must not be empty")
        if self.type in {"continuous", "integer"} and self.domain is not None:
            if not isinstance(self.domain, tuple) or len(self.domain) != 2:
                raise ValueError("numeric parameter domains must be a (minimum, maximum) pair")
            if self.domain[0] > self.domain[1]:
                raise ValueError("numeric parameter domain minimum must not exceed maximum")
        if self.type == "categorical" and self.domain is not None:
            if not isinstance(self.domain, list) or not self.domain:
                raise ValueError("categorical parameter domains must be a non-empty list")
        return self

    @model_validator(mode="after")
    def _item_parameters_defer_to_the_bank(self) -> "ParameterConfig":
        if self.source == "item":
            if self.domain is not None:
                raise ValueError(
                    f"parameter {self.name!r} is item-sourced, so its levels are derived from the "
                    "item bank; remove the hand-written domain rather than restating the bank"
                )
            if self.fixed:
                raise ValueError(
                    f"parameter {self.name!r} is item-sourced, so no design sets it and "
                    "release-fixed does not apply"
                )
        elif self.item_key is not None:
            raise ValueError(
                f"parameter {self.name!r} names an item_key but is design-sourced; "
                "item_key only applies when source='item'"
            )
        return self

    @property
    def bank_key(self) -> str:
        """The item-bank column this parameter reads, for item-sourced parameters."""

        return self.item_key or self.name


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
    accepts: list[Literal["llm", "scripted", "human"]] = Field(
        default_factory=lambda: ["llm"]
    )
    description: str = ""

    @field_validator("accepts")
    @classmethod
    def _accepts_at_least_one_kind(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("a role must accept at least one participant kind")
        if len(value) != len(set(value)):
            raise ValueError("accepted participant kinds must be unique")
        return value


class ResourceConfig(_StrictModel):
    id: str
    kind: str
    description: str = ""


class MeasureConfig(_StrictModel):
    """A declared metric; computation remains environment/extractor-owned."""

    name: str
    producer: Literal["environment", "derived"]
    grain: Literal["episode", "sequence"] = "episode"
    index_label: str | None = None
    scope: Literal["episode", "participant"] = "episode"
    unit: Literal["joint", "participant"] = "joint"
    direction: Literal["maximize", "minimize", "neutral"] = "neutral"
    extractor: str | None = None

    @model_validator(mode="after")
    def _derived_metrics_name_an_extractor(self) -> "MeasureConfig":
        if self.producer == "derived" and not self.extractor:
            raise ValueError("a derived metric requires an extractor identifier")
        if self.producer == "environment" and self.extractor is not None:
            raise ValueError("a environment-produced metric must not name an extractor")
        if self.grain == "sequence" and not self.index_label:
            raise ValueError("a sequence measure requires an index_label")
        if self.grain == "episode" and self.index_label is not None:
            raise ValueError("an episode measure must not name an index_label")
        return self


class AdapterBindingsConfig(_StrictModel):
    """Named adapters pinned for comparison; games adopt them component by component."""

    model: str = "engine.llm"
    communication: str = "local.in_process"
    resources: dict[str, str] = Field(default_factory=dict)


class ItemPolicy(_StrictModel):
    """A frozen item bank and the release-owned policy for selecting from it."""

    mode: Literal["enumerate", "sample"]
    bank_path: str
    item_bank_sha256: str

    @field_validator("bank_path")
    @classmethod
    def _local_relative_bank_path(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or "://" in value:
            raise ValueError("item banks must use a local relative path, not an absolute path or URI")
        return value

    @field_validator("item_bank_sha256")
    @classmethod
    def _item_bank_sha256(cls, value: str) -> str:
        value = value.lower()
        if not _SHA256.fullmatch(value):
            raise ValueError("item_bank_sha256 must be a 64-character hexadecimal digest")
        return value


class ReleaseDeclaration(_StrictModel):
    """Versioned, declarative environment release.

    ``id``/``release``/``engine`` remain while the typed YAML format is in use.
    The Phase 2 fields make the release inspectable by the researcher-facing
    control plane without making a second declaration format.
    """

    schema_version: Literal[1] = 1
    id: str | None = None
    release: str | None = None
    description: str = ""
    engine: EngineConfig | None = None
    inputs: list[InputConfig] = Field(default_factory=list)
    roles: list[RoleConfig] = Field(default_factory=list)
    resources: list[ResourceConfig] = Field(default_factory=list)
    metrics: list[MeasureConfig] = Field(default_factory=list)
    measures: list[MeasureConfig] = Field(default_factory=list)
    adapter_bindings: AdapterBindingsConfig = Field(default_factory=AdapterBindingsConfig)
    environment_id: str | None = None
    version: str | None = None
    blurb: str = ""
    source_url: str = ""
    parameters: list[ParameterConfig] = Field(default_factory=list)
    item_policy: ItemPolicy | None = None
    oracle_version: str | None = None

    @field_validator("id")
    @classmethod
    def _release_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _ID.fullmatch(value):
            raise ValueError("release id must start with a lowercase letter and contain only [a-z0-9_.-]")
        return value

    @model_validator(mode="after")
    def _unique_declarations(self) -> "ReleaseDeclaration":
        inferred_environment_id = self.environment_id or (
            self.engine.environment_id if self.engine is not None else None
        )
        if inferred_environment_id is None:
            raise ValueError("a release declaration must name an environment_id")
        if not _ID.fullmatch(inferred_environment_id):
            raise ValueError("environment_id must start with a lowercase letter and contain only [a-z0-9_.-]")
        self.environment_id = inferred_environment_id
        self.id = self.id or inferred_environment_id
        self.version = self.version or self.release or "v1"
        self.release = self.release or self.version
        self.blurb = self.blurb or self.description
        self.description = self.description or self.blurb
        if self.metrics and self.measures and self.metrics != self.measures:
            raise ValueError("metrics and measures must agree when both are declared")
        self.measures = self.measures or list(self.metrics)
        self.metrics = self.metrics or list(self.measures)
        for name, values in (("input", self.inputs), ("role", self.roles),
                             ("resource", self.resources), ("metric", self.metrics),
                             ("parameter", self.parameters)):
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
    if config.item_policy is not None:
        bank_path = (path.parent / config.item_policy.bank_path).resolve()
        if not bank_path.is_file():
            raise FileNotFoundError(f"release item bank does not exist: {bank_path}")
        actual = _sha256_file(bank_path)
        if actual != config.item_policy.item_bank_sha256:
            raise ValueError(
                "release item bank digest mismatch: "
                f"expected {config.item_policy.item_bank_sha256}, got {actual}"
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
