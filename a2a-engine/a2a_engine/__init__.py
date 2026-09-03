"""a2a-engine: environment-agnostic framework for agent-to-agent coordination experiments."""

from __future__ import annotations

from typing import Any


_EXPORTS = {
    "AgentInterface": ("a2a_engine.agent", "AgentInterface"),
    "LLMAgent": ("a2a_engine.agent", "LLMAgent"),
    "ExecutionPlan": ("a2a_engine.experiment", "ExecutionPlan"),
    "CellPlan": ("a2a_engine.experiment", "CellPlan"),
    "load_experiment": ("a2a_engine.experiment", "load_experiment"),
    "expand_cells": ("a2a_engine.experiment", "expand_cells"),
    "resolve_storage": ("a2a_engine.experiment", "resolve_storage"),
    "expand_env": ("a2a_engine.experiment", "expand_env"),
    "ReleaseDeclaration": ("a2a_engine.environment", "ReleaseDeclaration"),
    "ParameterConfig": ("a2a_engine.declaration", "ParameterConfig"),
    "ItemPolicy": ("a2a_engine.declaration", "ItemPolicy"),
    "MeasureConfig": ("a2a_engine.declaration", "MeasureConfig"),
    "RoleConfig": ("a2a_engine.declaration", "RoleConfig"),
    "ExperimentConfig": ("a2a_engine.environment", "ExperimentConfig"),
    "InputConfig": ("a2a_engine.environment", "InputConfig"),
    "load_release_declaration": ("a2a_engine.environment", "load_release_declaration"),
    "load_experiment_config": ("a2a_engine.environment", "load_experiment_config"),
    "AdapterDescriptor": ("a2a_engine.adapters", "AdapterDescriptor"),
    "AdapterRegistry": ("a2a_engine.adapters", "AdapterRegistry"),
    "adapters": ("a2a_engine.adapters", "adapters"),
    "register_adapter": ("a2a_engine.adapters", "register_adapter"),
    "run_with_parallelism": ("a2a_engine.parallel", "run_with_parallelism"),
    "ParticipantBinding": ("a2a_engine.schemas", "ParticipantBinding"),
    "EpisodeConfigBase": ("a2a_engine.schemas", "EpisodeConfigBase"),
    "Event": ("a2a_engine.schemas", "Event"),
    "EpisodeTrace": ("a2a_engine.schemas", "EpisodeTrace"),
    "ReleaseReference": ("a2a_engine.schemas", "ReleaseReference"),
    "EpisodeReference": ("a2a_engine.schemas", "EpisodeReference"),
    "EventLog": ("a2a_engine.tracing", "EventLog"),
    "JsonlEventSink": ("a2a_engine.event_sink", "JsonlEventSink"),
    "read_event_sink": ("a2a_engine.event_sink", "read_event_sink"),
    "project_events_to_trace": ("a2a_engine.stream_projection", "project_events_to_trace"),
    "project_stream_to_trace": ("a2a_engine.stream_projection", "project_stream_to_trace"),
    "derive_seed": ("a2a_engine.seeds", "derive_seed"),
    "build_provenance": ("a2a_engine.provenance", "build_provenance"),
    "provenance_of": ("a2a_engine.provenance", "provenance_of"),
    "Design": ("a2a_engine.design", "Design"),
    "Disposition": ("a2a_engine.design", "Disposition"),
    "ParticipantConfig": ("a2a_engine.design", "ParticipantConfig"),
    "DesignValidationError": ("a2a_engine.design", "DesignValidationError"),
    "parse_design_text": ("a2a_engine.design", "parse_design_text"),
    "compile_design": ("a2a_engine.compiler", "compile"),
    "validate_design": ("a2a_engine.compiler", "validate"),
    "ItemBank": ("a2a_engine.items", "ItemBank"),
    "write_episode": ("a2a_engine.tracing", "write_episode"),
    "read_episode": ("a2a_engine.tracing", "read_episode"),
    "register_environment": ("a2a_engine.registry", "register_environment"),
    "get_environment": ("a2a_engine.registry", "get_environment"),
    "get_environment_spec": ("a2a_engine.registry", "get_environment_spec"),
    "EnvironmentSpec": ("a2a_engine.registry", "EnvironmentSpec"),
    "EpisodeManifest": ("a2a_engine.manifest", "EpisodeManifest"),
    "EpisodeStore": ("a2a_engine.storage", "EpisodeStore"),
    "StoreCheck": ("a2a_engine.storage", "StoreCheck"),
    "check_store": ("a2a_engine.storage", "check_store"),
    "make_store": ("a2a_engine.storage", "make_store"),
    "register_store": ("a2a_engine.storage", "register_store"),
    "list_stores": ("a2a_engine.storage", "list_stores"),
    "list_environments": ("a2a_engine.registry", "list_environments"),
    "discover_environments": ("a2a_engine.registry", "discover_environments"),
    "EpisodeDataset": ("a2a_engine.dataset", "EpisodeDataset"),
    "GameMessage": ("a2a_engine.dataset", "GameMessage"),
    "EpisodeRecord": ("a2a_engine.dataset", "EpisodeRecord"),
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
