"""Named adapter seams for swappable model, communication, and resource components.

The environment engine remains responsible for its current concrete implementations.
This module deliberately starts with registration and validation rather than a
premature universal RPC abstraction: an release can name the components it
requires, episodes preserve those names, and future implementations can be
introduced without changing the release schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


AdapterKind = Literal["model", "communication", "resource"]


class AdapterDescriptor(BaseModel):
    """Stable name and capability declaration for a pluggable component."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: AdapterKind
    version: str = "v1"
    capabilities: set[str] = Field(default_factory=set)


@runtime_checkable
class ModelAdapter(Protocol):
    """Creates a model client from an agent's resolved configuration."""

    descriptor: AdapterDescriptor

    def create(self, config: dict[str, Any]) -> Any: ...


@runtime_checkable
class CommunicationAdapter(Protocol):
    """Provides a environment-visible communication channel or topology."""

    descriptor: AdapterDescriptor

    def create(self, config: dict[str, Any]) -> Any: ...


@runtime_checkable
class ResourceAdapter(Protocol):
    """Provides a named release resource from resolved configuration."""

    descriptor: AdapterDescriptor

    def create(self, config: dict[str, Any]) -> Any: ...


AdapterFactory = Callable[[dict[str, Any]], Any]


class AdapterRegistry:
    """Small in-process registry used by games while adapters migrate in."""

    def __init__(self) -> None:
        self._entries: dict[tuple[AdapterKind, str], tuple[AdapterDescriptor, AdapterFactory]] = {}

    def register(self, descriptor: AdapterDescriptor, factory: AdapterFactory) -> None:
        self._entries[(descriptor.kind, descriptor.name)] = (descriptor, factory)

    def descriptor(self, kind: AdapterKind, name: str) -> AdapterDescriptor:
        try:
            return self._entries[(kind, name)][0]
        except KeyError as exc:
            raise KeyError(f"No {kind} adapter named {name!r}") from exc

    def create(self, kind: AdapterKind, name: str, config: dict[str, Any]) -> Any:
        try:
            factory = self._entries[(kind, name)][1]
        except KeyError as exc:
            raise KeyError(f"No {kind} adapter named {name!r}") from exc
        return factory(config)

    def list(self, kind: AdapterKind | None = None) -> list[AdapterDescriptor]:
        return sorted(
            (descriptor for descriptor, _ in self._entries.values() if kind is None or descriptor.kind == kind),
            key=lambda descriptor: (descriptor.kind, descriptor.name),
        )


adapters = AdapterRegistry()


def register_adapter(descriptor: AdapterDescriptor, factory: AdapterFactory) -> None:
    adapters.register(descriptor, factory)


def resolve_bindings(config: dict, specs: list[dict]) -> dict:
    remote = any(spec.get("runtime", "in_process") != "in_process" for spec in specs)
    communication = "mcp.http" if remote else "local.in_process"
    explicit = config.get("adapter_bindings") or {}
    if not isinstance(explicit, dict):
        raise ValueError("adapter_bindings must be a mapping")
    if explicit.get("communication", communication) != communication:
        raise ValueError(f"incompatible communication binding: expected {communication}")
    models = {}
    for seat, spec in enumerate(specs):
        expected = "remote" if spec.get("runtime", "in_process") != "in_process" else "engine.llm"
        selected = spec.get("model_adapter", explicit.get("model", expected))
        if selected != expected:
            raise ValueError(f"incompatible model binding for seat {seat}: expected {expected}")
        adapters.descriptor("model", selected)
        models[str(seat)] = selected
    adapters.descriptor("communication", communication)
    return {"communication": communication, "models": models,
            "resources": explicit.get("resources", {})}


def _local_model(config):
    from .llm.factory import make_llm_client
    return make_llm_client(config)


def _communication(config):
    from .comm import CommRouter
    return CommRouter(config["topology"])


def _mcp_communication(config):
    if config.get("runtime_context") is None:
        raise ValueError("mcp.http requires a live runtime context")
    return _communication(config)


def _remote_model(config):
    if config.get("runtime_context") is None:
        raise ValueError("remote requires a live runtime context")
    return config["client_factory"]()


for _name, _kind, _factory in (
    ("engine.llm", "model", _local_model), ("remote", "model", _remote_model),
    ("local.in_process", "communication", _communication), ("mcp.http", "communication", _mcp_communication),
):
    register_adapter(AdapterDescriptor(name=_name, kind=_kind), _factory)
