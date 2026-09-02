"""a2a-engine: game-agnostic framework for agent-to-agent coordination experiments."""

from __future__ import annotations

from typing import Any


_EXPORTS = {
    "AgentInterface": ("a2a_engine.agent", "AgentInterface"),
    "LLMAgent": ("a2a_engine.agent", "LLMAgent"),
    "ExperimentSpec": ("a2a_engine.experiment", "ExperimentSpec"),
    "BatchSpec": ("a2a_engine.experiment", "BatchSpec"),
    "load_experiment": ("a2a_engine.experiment", "load_experiment"),
    "expand_batches": ("a2a_engine.experiment", "expand_batches"),
    "resolve_storage": ("a2a_engine.experiment", "resolve_storage"),
    "expand_env": ("a2a_engine.experiment", "expand_env"),
    "EnvironmentConfig": ("a2a_engine.environment", "EnvironmentConfig"),
    "ExperimentConfig": ("a2a_engine.environment", "ExperimentConfig"),
    "InputConfig": ("a2a_engine.environment", "InputConfig"),
    "load_environment_config": ("a2a_engine.environment", "load_environment_config"),
    "load_experiment_config": ("a2a_engine.environment", "load_experiment_config"),
    "AdapterDescriptor": ("a2a_engine.adapters", "AdapterDescriptor"),
    "AdapterRegistry": ("a2a_engine.adapters", "AdapterRegistry"),
    "adapters": ("a2a_engine.adapters", "adapters"),
    "register_adapter": ("a2a_engine.adapters", "register_adapter"),
    "run_with_parallelism": ("a2a_engine.parallel", "run_with_parallelism"),
    "AgentInfo": ("a2a_engine.schemas", "AgentInfo"),
    "GameConfigBase": ("a2a_engine.schemas", "GameConfigBase"),
    "GameEvent": ("a2a_engine.schemas", "GameEvent"),
    "GameTraceBase": ("a2a_engine.schemas", "GameTraceBase"),
    "EnvironmentReference": ("a2a_engine.schemas", "EnvironmentReference"),
    "EpisodeReference": ("a2a_engine.schemas", "EpisodeReference"),
    "EventLog": ("a2a_engine.tracing", "EventLog"),
    "write_trace": ("a2a_engine.tracing", "write_trace"),
    "read_trace": ("a2a_engine.tracing", "read_trace"),
    "register_game": ("a2a_engine.registry", "register_game"),
    "get_game": ("a2a_engine.registry", "get_game"),
    "get_game_spec": ("a2a_engine.registry", "get_game_spec"),
    "GameSpec": ("a2a_engine.registry", "GameSpec"),
    "RunManifest": ("a2a_engine.manifest", "RunManifest"),
    "TraceStore": ("a2a_engine.storage", "TraceStore"),
    "StoreCheck": ("a2a_engine.storage", "StoreCheck"),
    "check_store": ("a2a_engine.storage", "check_store"),
    "make_store": ("a2a_engine.storage", "make_store"),
    "register_store": ("a2a_engine.storage", "register_store"),
    "list_stores": ("a2a_engine.storage", "list_stores"),
    "list_games": ("a2a_engine.registry", "list_games"),
    "discover_games": ("a2a_engine.registry", "discover_games"),
    "GameDataset": ("a2a_engine.dataset", "GameDataset"),
    "GameMessage": ("a2a_engine.dataset", "GameMessage"),
    "GameRecord": ("a2a_engine.dataset", "GameRecord"),
    "DerivedArtifact": ("a2a_engine.derived", "DerivedArtifact"),
    "trace_digest": ("a2a_engine.derived", "trace_digest"),
    "DerivedMetricResult": ("a2a_engine.derived_metrics", "DerivedMetricResult"),
    "DerivedMetricRegistry": ("a2a_engine.derived_metrics", "DerivedMetricRegistry"),
    "derived_metrics": ("a2a_engine.derived_metrics", "derived_metrics"),
    "register_derived_metric_extractor": ("a2a_engine.derived_metrics", "register_derived_metric_extractor"),
    "materialize_derived_metrics": ("a2a_engine.derived_metrics", "materialize_derived_metrics"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'a2a_engine' has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
