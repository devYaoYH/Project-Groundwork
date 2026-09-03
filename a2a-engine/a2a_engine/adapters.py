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

