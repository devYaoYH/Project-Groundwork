"""Build-time design projection consumed without importing a game package."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .environment import EngineConfig, InputConfig, ItemPolicy, MeasureConfig, ParameterConfig, ReleaseDeclaration, RoleConfig


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SurfaceRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    count: int
    accepts: list[str]
    bindings: list[str]
    description: str = ""


class ReleaseSurface(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[SurfaceRole]
    parameters: list[ParameterConfig]
    item_policy: ItemPolicy | None
    engine_defaults: dict[str, Any]
    inputs: list[InputConfig] = Field(default_factory=list)
    measures: list[MeasureConfig]
    oracle_version: str | None
    blurb: str = ""
    source_url: str = ""

    @field_validator("parameters", mode="before")
    @classmethod
    def restore_numeric_ranges(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        return [
            {**value, "domain": tuple(value["domain"])}
            if isinstance(value, dict) and value.get("type") in {"integer", "continuous"}
            and isinstance(value.get("domain"), list) else value
            for value in values
        ]


class PublishedRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    environment_id: str
    release: str
    release_id: str
    package: str | None = None
    surface: ReleaseSurface
    surface_sha256: str
    declaration_sha256: str
    image_digest: str | None = None

    @model_validator(mode="after")
    def verify(self) -> "PublishedRelease":
        if self.schema_version != 1 or self.surface_sha256 != surface_digest(self.surface):
            raise ValueError("release surface digest mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.declaration_sha256):
            raise ValueError("invalid declaration digest")
        if self.image_digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("image digest must be a pinned sha256")
        return self

    def design_declaration(self) -> "DesignDeclaration":
        return DesignDeclaration(self)


class DesignDeclaration:
    """The compiler's declaration shape, without runtime implementation mappings."""

    def __init__(self, manifest: PublishedRelease):
        self._declaration_digest = manifest.declaration_sha256
        self.id = manifest.release_id
        self.environment_id = manifest.environment_id
        self.version = manifest.release
        self.schema_version = manifest.schema_version
        surface = manifest.surface
        self.roles = [RoleConfig(id=role.id, count=role.count, accepts=role.accepts,
                                 scripted_bindings={name: "scripted" for name in role.bindings},
                                 description=role.description) for role in surface.roles]
        self.parameters = surface.parameters
        self.item_policy = surface.item_policy
        self.engine = EngineConfig(environment_id=manifest.environment_id, defaults=surface.engine_defaults)
        self.inputs = surface.inputs
        self.measures = surface.measures
        self.oracle_version = surface.oracle_version
        self.blurb = surface.blurb
        self.source_url = surface.source_url

    def content_sha256(self) -> str:
        return self._declaration_digest

    def model_dump(self, *, mode: str = "json") -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.id,
            "release": self.version, "environment_id": self.environment_id,
            "roles": [role.model_dump(mode=mode) for role in self.roles],
            "parameters": [parameter.model_dump(mode=mode) for parameter in self.parameters],
            "item_policy": self.item_policy.model_dump(mode=mode) if self.item_policy else None,
            "engine": self.engine.model_dump(mode=mode),
            "measures": [measure.model_dump(mode=mode) for measure in self.measures],
            "oracle_version": self.oracle_version, "blurb": self.blurb,
            "source_url": self.source_url,
        }


def project_surface(declaration: ReleaseDeclaration) -> ReleaseSurface:
    return ReleaseSurface(
        roles=[SurfaceRole(id=role.id, count=role.count, accepts=list(role.accepts),
                           bindings=sorted(role.scripted_bindings), description=role.description)
               for role in declaration.roles],
        parameters=list(declaration.parameters), item_policy=declaration.item_policy,
        engine_defaults=dict(declaration.engine.defaults) if declaration.engine else {},
        inputs=list(declaration.inputs), measures=list(declaration.measures),
        oracle_version=declaration.oracle_version, blurb=declaration.blurb,
        source_url=declaration.source_url,
    )


def surface_digest(surface: ReleaseSurface) -> str:
    return _digest(surface.model_dump(mode="json"))


def publish_release(declaration: ReleaseDeclaration, *, package: str | None = None,
                    image_digest: str | None = None) -> PublishedRelease:
    surface = project_surface(declaration)
    return PublishedRelease(environment_id=str(declaration.environment_id),
                            release=str(declaration.version), release_id=str(declaration.id),
                            package=package, surface=surface, surface_sha256=surface_digest(surface),
                            declaration_sha256=declaration.content_sha256(), image_digest=image_digest)
