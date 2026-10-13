"""Discover trusted installed agent-side seat policies; never execute game setup."""

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import entry_points
from typing import Callable

ENTRY_POINT_GROUP = "a2a_agent.seats"
POLICY_FIELDS = {
    "type",
    "model",
    "api_base",
    "api_format",
    "temperature",
    "max_tokens",
    "capabilities",
}


@dataclass(frozen=True)
class PolicyContext:
    config: dict

    def model_client(self):
        if self.config.get("type", "scripted") != "llm":
            return None
        from a2a_engine.llm.factory import make_llm_client
        from a2a_engine.remote.compose import MODEL_FIELDS

        return make_llm_client(
            {
                "temperature": 0.0,
                **{
                    key: value
                    for key, value in self.config.items()
                    if key in MODEL_FIELDS and key != "type"
                },
            }
        )


@dataclass(frozen=True)
class SeatPlugin:
    environment_id: str
    factory: Callable
    supported_policy_kinds: frozenset[str] = frozenset({"scripted", "llm"})
    runtime_adapter: str = "a2a-turns/2"

    def create_policy(self, config):
        if not isinstance(config, dict) or set(config) - POLICY_FIELDS:
            raise ValueError("unsupported agent policy")
        kind = config.get("type", "scripted")
        if kind not in self.supported_policy_kinds:
            raise ValueError("unsupported agent strategy")
        if kind == "llm" and not config.get("model"):
            raise ValueError("provider strategy requires a model")
        return self.factory(PolicyContext(dict(config)))


@lru_cache(maxsize=1)
def seat_plugins():
    from a2a_engine.registry import installed_environments

    environments = set(installed_environments())
    plugins = {}
    for entry in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda item: item.name):
        if entry.name in plugins:
            raise ValueError(f"Duplicate seat plugin: {entry.name}")
        plugin = entry.load()
        if (
            not isinstance(plugin, SeatPlugin)
            or plugin.environment_id != entry.name
            or entry.name not in environments
        ):
            raise ValueError(f"Invalid installed seat plugin: {entry.name}")
        if plugin.runtime_adapter not in {"a2a-turns/1", "a2a-turns/2"}:
            raise ValueError(f"Unsupported seat runtime adapter: {entry.name}")
        plugins[entry.name] = plugin
    return plugins


def get_seat_plugin(environment_id):
    try:
        return seat_plugins()[environment_id]
    except KeyError:
        raise ValueError("unsupported environment strategy") from None
