"""The authored, content-addressed experiment design document.

The design is intentionally narrower than an execution configuration.  It
contains only the choices that compile into episode configs: parameter
dispositions, units, roster, and seed policy.  Analysis fields do not belong
here because no runtime consumer exists for them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from .environment import _StrictModel


Scalar = str | float | int | bool


@dataclass(frozen=True)
class ValidationIssue:
    """One user-facing validation error with a stable document path."""

    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class DesignValidationError(ValueError):
    """Validation errors which API callers can render inline."""

    def __init__(self, errors: list[ValidationIssue]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{error.path}: {error.message}" for error in errors))


class Disposition(_StrictModel):
    """What one design does with one declared parameter.

    Exactly one of the three fields is allowed.  ``randomize`` is typed as a
    bool so malformed browser input gets a useful model error; its only valid
    value is ``true`` and source-specific validation happens in the compiler.
    """

    factor: list[Scalar] | None = Field(default=None, min_length=1)
    pin: Scalar | None = None
    randomize: bool | None = None

    @model_validator(mode="after")
    def _one_disposition(self) -> "Disposition":
        configured = sum(value is not None for value in (self.factor, self.pin, self.randomize))
        if configured != 1:
            raise ValueError("set exactly one of factor, pin, or randomize")
        if self.randomize is False:
            raise ValueError("randomize must be true when it is selected")
        return self


class ParticipantConfig(_StrictModel):
    id: str
    role: str | None = None
    kind: Literal["llm", "scripted", "human"] = "llm"
    binding: str | None = None


class UnitsConfig(_StrictModel):
    episodes_per_cell: int = Field(ge=1)


class SeedConfig(_StrictModel):
    mode: Literal["derived", "static"] = "derived"
    root: int


class Design(_StrictModel):
    """The complete authored design, independent of its database identity."""

    schema_version: Literal[1] = 1
    release: str
    parameters: dict[str, Disposition] = Field(default_factory=dict)
    units: UnitsConfig
    roster: list[ParticipantConfig] = Field(default_factory=list)
    seed: SeedConfig

    def content_sha256(self) -> str:
        blob = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_design_text(text: str) -> Design:
    """Parse an authored YAML document into the strict design model."""

    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DesignValidationError([ValidationIssue("$", f"invalid YAML: {exc}")]) from exc
    if not isinstance(payload, dict):
        raise DesignValidationError([ValidationIssue("$", "design must be one YAML mapping")])
    try:
        return Design.model_validate(payload)
    except ValidationError as exc:
        errors = [
            ValidationIssue(_format_path(error["loc"]), str(error["msg"]))
            for error in exc.errors()
        ]
        raise DesignValidationError(errors) from exc


def _format_path(location: tuple[Any, ...]) -> str:
    if not location:
        return "$"
    parts: list[str] = []
    for part in location:
        if isinstance(part, int):
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return ".".join(parts)
