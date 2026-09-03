"""Per-researcher bindings from a logical agent name to a concrete endpoint.

Two people can run the same study with the same agent line-up and still reach
different providers: Claude through the Anthropic API, through OpenRouter, or
through Vertex under ADC. That difference belongs to the person running the
experiment, not to the experiment, so it lives here rather than in a config
that gets committed and shared.

The pool is an input to resolution, never part of the record. What a run
persists is the hydrated config — concrete model strings, formats and
endpoints — exactly as a config that named its agents inline would.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

SHARED_POOL_RELATIVE = Path("experiments/agents.yaml")
LOCAL_POOL_NAME = "agents.local.yaml"
POOL_ENV_VAR = "A2A_AGENT_POOL"


class AgentPoolEntry(BaseModel):
    """One named, runnable agent binding."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    type: str = "llm"
    model: str | None = None
    api_format: str | None = None
    api_base: str | None = None
    # Named rather than inferred from the model string: inference is what makes
    # a missing key surface as an opaque 401 instead of a named variable.
    # ``None`` means the binding needs no release key (ADC, or a scripted
    # agent that calls nothing).
    credential: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    def as_agent_config(self) -> dict[str, Any]:
        """The agent dict a environment receives, with pool-only fields dropped."""
        fields = self.model_dump(
            exclude={"description", "credential", "config"}, exclude_none=True,
        )
        return {**fields, **self.config}


class AgentPool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentPoolEntry] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)

    def entry(self, name: str) -> AgentPoolEntry:
        try:
            return self.agents[name]
        except KeyError:
            raise KeyError(
                f"unknown agent {name!r}; the pool defines {sorted(self.agents)}. "
                f"Add it to {LOCAL_POOL_NAME} or {SHARED_POOL_RELATIVE}."
            ) from None

    def missing_credentials(self, names: list[str]) -> list[str]:
        """Release variables these bindings need that are not set."""
        missing = {
            entry.credential
            for entry in (self.entry(name) for name in names)
            if entry.credential and not os.environ.get(entry.credential)
        }
        return sorted(missing)


def _read_pool_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one YAML mapping")
    agents = payload.get("agents") or {}
    if not isinstance(agents, dict):
        raise ValueError(f"{path}: 'agents' must be a mapping of name -> binding")
    return agents


def find_pool_files(start: Path) -> list[Path]:
    """Locate the shared pool and any local override, nearest repository first.

    Walks up from an experiment file looking for ``experiments/agents.yaml``,
    so a environment's configs resolve against the workspace that contains them.
    """
    override = os.environ.get(POOL_ENV_VAR)
    if override:
        return [Path(override)] if Path(override).is_file() else []

    start = start.resolve()
    for parent in [start, *start.parents]:
        shared = parent / SHARED_POOL_RELATIVE
        if shared.is_file():
            local = parent / LOCAL_POOL_NAME
            # Local last: a researcher's binding shadows the shared default.
            return [shared, local] if local.is_file() else [shared]
    return []


def load_agent_pool(start: Path | str) -> AgentPool:
    """Build the effective pool for an experiment file's workspace."""
    merged: dict[str, AgentPoolEntry] = {}
    sources: list[str] = []
    for path in find_pool_files(Path(start)):
        for name, binding in _read_pool_file(path).items():
            merged[name] = AgentPoolEntry.model_validate(binding or {})
        sources.append(str(path))
    return AgentPool(agents=merged, sources=sources)


def hydrate_participants(participants: list[str], pool: AgentPool) -> list[dict[str, Any]]:
    """Resolve positional participant names into concrete agent configs."""
    return [pool.entry(name).as_agent_config() for name in participants]
