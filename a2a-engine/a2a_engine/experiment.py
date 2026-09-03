"""YAML experiment loader: presets + defaults + cell override merge semantics.

Schema:

    name: my_experiment
    description: ...
    presets_path: presets.yaml   # optional, relative to the experiment file
    sinks_path: storage.yaml     # optional, relative to the experiment file
    sinks:                       # named storage targets, reusable across files
      lab_db:
        backend: sqlite
        path: ./results/a2a.db
    storage:                     # where this experiment's episodes land
      extends: lab_db            # inherit a named sink, then override
      path: ./results/pilot.db
    defaults:
      environment_id: calendar
      preset: base               # optional named preset
      num_agents: 3
      ...                        # any GameConfig field
    cells:
      - label: baseline
        count: 5
        preset: high_talk        # optional per-cell preset
        config:                  # overrides on top of preset + defaults
          seed: 1
          environment_id: word_guess  # optional: a cell may switch games

Resolution order, lowest precedence first: **preset -> defaults -> cell config**.
That ordering is inherited from a2a-negotiation's runner, where presets are
coarse scenario templates that defaults and cells refine.

Presets and sinks both support single inheritance via ``extends:``. A definition
may only extend one declared earlier in the file, which keeps resolution a single
forward pass and makes cycles impossible to express.

Storage values interpolate ``${VAR}`` and ``${VAR:-fallback}`` from the process
release, so credentials and account-specific names live in ``.env`` rather
than in committed YAML. A bare ``${VAR}`` that is unset is an error; write
``${VAR:-}`` to opt into an empty default.
"""

import os
import re
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from a2a_engine.environment import ReleaseDeclaration, ExperimentConfig, load_experiment_config

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class CellPlan(BaseModel):
    label: str
    count: int = 1
    preset: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    name: str
    description: str = ""
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sinks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    storage: dict[str, Any] | None = None
    observability: dict[str, Any] | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    cells: list[CellPlan] = Field(default_factory=list)

    def environment_ids(self) -> list[str]:
        """Every environment this experiment touches, defaults plus per-cell overrides.

        A cell may override ``environment_id``, which is what lets one file smoke-test
        the whole suite. The runner uses this to import and preflight each environment.
        """
        names: list[str] = []
        for cell in self.cells:
            name = cell.config.get("environment_id") or self.defaults.get("environment_id")
            if name and name not in names:
                names.append(str(name))
        default = self.defaults.get("environment_id")
        if not names and default:
            names.append(str(default))
        return names


def expand_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` / ``${VAR:-fallback}`` in strings."""

    def substitute(match: re.Match) -> str:
        var, fallback = match.group(1), match.group(2)
        if var in os.environ:
            return os.environ[var]
        if fallback is not None:
            return fallback
        raise ValueError(
            f"Release variable {var!r} is referenced but not set. "
            f"Add it to your .env, or write ${{{var}:-}} to allow an empty value."
        )

    if isinstance(value, str):
        return _ENV_PATTERN.sub(substitute, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def resolve_storage(
    spec: "ExecutionPlan",
    *,
    game_default: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the effective ``storage:`` block for an experiment.

    Precedence, lowest first: the environment's registered default, the named sink named
    by ``extends:``, the inline ``storage:`` keys, then caller overrides (CLI).

    When the resolved backend differs from the environment's registered default, the
    environment default is dropped rather than merged — carrying a remote ``prefix``
    into a SQLite sink would be noise at best and a confusing manifest at worst.
    """
    inline = dict(spec.storage or {})
    sink_name = inline.pop("extends", None)

    sink: dict[str, Any] = {}
    if sink_name:
        if sink_name not in spec.sinks:
            raise ValueError(
                f"storage.extends references unknown sink {sink_name!r}. "
                f"Known sinks: {sorted(spec.sinks) or '<none>'}"
            )
        sink = dict(spec.sinks[sink_name])

    overrides = dict(overrides or {})
    resolved = _deep_merge(_deep_merge(sink, inline), overrides)

    base = dict(game_default or {})
    if base:
        declared_backend = base.get("backend")
        effective_backend = resolved.get("backend", declared_backend)
        if declared_backend and effective_backend != declared_backend:
            base = {}
        resolved = _deep_merge(base, resolved)

    if sink_name:
        resolved.setdefault("sink", sink_name)
    return expand_env(resolved)


def resolve_sinks(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Flatten ``extends:`` inheritance between named sinks."""
    resolved: dict[str, dict[str, Any]] = {}
    for name, sink in raw.items():
        sink = dict(sink or {})
        base_name = sink.pop("extends", None)
        if base_name is None:
            resolved[name] = sink
            continue
        if base_name not in resolved:
            raise ValueError(
                f"Sink {name!r} extends unknown sink {base_name!r} "
                "(the parent must be defined earlier in the file)"
            )
        resolved[name] = _deep_merge(resolved[base_name], sink)
    return resolved


def resolve_presets(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Flatten ``extends:`` inheritance into fully-resolved presets."""
    resolved: dict[str, dict[str, Any]] = {}
    for name, preset in raw.items():
        preset = dict(preset or {})
        base_name = preset.pop("extends", None)
        if base_name is None:
            resolved[name] = preset
            continue
        if base_name not in resolved:
            raise ValueError(
                f"Preset {name!r} extends unknown preset {base_name!r} "
                "(the parent must be defined earlier in the file)"
            )
        resolved[name] = _deep_merge(resolved[base_name], preset)
    return resolved


def _load_sidecar(
    path: Path, raw: dict, *, key: str, path_key: str, default_name: str
) -> dict[str, dict[str, Any]]:
    """Merge a shared sidecar YAML (presets.yaml / storage.yaml) with inline defs.

    The sidecar is auto-discovered when it sits next to the experiment file, a
    convention inherited from a2a-negotiation. Inline definitions win, so an
    experiment can always override the shared file without editing it.
    """
    inline = dict(raw.pop(key, {}) or {})
    sidecar_path = raw.pop(path_key, None)
    if sidecar_path is None and (path.parent / default_name).exists():
        sidecar_path = default_name
    if not sidecar_path:
        return inline
    resolved_path = Path(sidecar_path)
    if not resolved_path.is_absolute():
        resolved_path = path.parent / resolved_path
    with open(resolved_path) as f:
        external = yaml.safe_load(f) or {}
    return {**(external.get(key, {}) or {}), **inline}


def load_experiment(path: str | Path) -> ExecutionPlan:
    """Load and validate an experiment YAML, resolving presets and sinks."""
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # The typed v1 format is intentionally additive.  Normalize it into the
    # long-lived ExecutionPlan/CellPlan execution model so existing games and
    # legacy YAML keep their exact runner semantics during migration.
    if "release" in raw and "episodes" in raw:
        experiment, release = load_experiment_config(path)
        return _typed_to_legacy_spec(experiment, release, path)

    presets_raw = _load_sidecar(
        path, raw, key="presets", path_key="presets_path", default_name="presets.yaml"
    )
    sinks_raw = _load_sidecar(
        path, raw, key="sinks", path_key="sinks_path", default_name="storage.yaml"
    )

    # A bare string is shorthand for "use this named sink as-is".
    storage = raw.get("storage")
    if isinstance(storage, str):
        raw["storage"] = {"extends": storage}

    participants = raw.pop("participants", None)
    spec = ExecutionPlan(**raw)
    spec.presets = resolve_presets(presets_raw)
    spec.sinks = resolve_sinks(sinks_raw)
    _hydrate_legacy(spec, path, participants)
    return spec


def _hydrate(participants: list[str], source: Path, expected: int | None) -> list[dict]:
    """Resolve positional participant names, checking the count is deliberate."""
    from a2a_engine.agent_pool import hydrate_participants, load_agent_pool

    if expected is not None and len(participants) != expected:
        raise ValueError(
            f"{source.name}: {len(participants)} participants declared but the "
            f"release declares {expected} agent slots; a mismatch is silently "
            f"truncated otherwise"
        )
    return hydrate_participants(participants, load_agent_pool(source))


def _hydrate_legacy(spec: ExecutionPlan, source: Path,
                    participants: list[str] | None) -> None:
    """Hydrate a pre-typed experiment that names participants from the pool."""
    participants = participants or spec.defaults.pop("participants", None)
    spec.defaults.pop("participants", None)
    if not participants:
        return
    if spec.defaults.get("agents"):
        raise ValueError(
            f"{source.name}: declare either 'agents' (already hydrated) or "
            f"'participants' (resolved from the agent pool), not both"
        )
    expected = spec.defaults.get("num_agents")
    spec.defaults["agents"] = _hydrate(list(participants), source, expected)
    spec.defaults.setdefault("num_agents", len(participants))


def _typed_to_legacy_spec(
    experiment: ExperimentConfig,
    release: ReleaseDeclaration,
    source_path: Path,
) -> ExecutionPlan:
    """Lower one typed experiment into concrete one-episode legacy cells."""
    defaults = dict(release.engine.defaults)
    defaults["environment_id"] = release.engine.environment_id
    if experiment.agents:
        defaults["agents"] = [agent.as_game_config() for agent in experiment.agents]
    elif experiment.participants:
        declared = sum(role.count for role in release.roles) or None
        defaults["agents"] = _hydrate(experiment.participants, source_path, declared)
    if "num_agents" not in defaults and release.roles:
        defaults["num_agents"] = sum(role.count for role in release.roles)
    defaults["release"] = {
        "schema_version": release.schema_version,
        "id": release.id,
        "release": release.release,
        "content_sha256": release.content_sha256(),
        "inputs": [item.model_dump(mode="json") for item in release.inputs],
        "roles": [item.model_dump(mode="json") for item in release.roles],
        "resources": [item.model_dump(mode="json") for item in release.resources],
        "metrics": [item.model_dump(mode="json") for item in release.metrics],
        "adapter_bindings": release.adapter_bindings.model_dump(mode="json"),
    }
    cells: list[CellPlan] = []
    for plan in experiment.episodes:
        for index, seed in enumerate(plan.expanded_seeds()):
            label = plan.label if plan.count == 1 else f"{plan.label}-{index}"
            cells.append(CellPlan(
                label=label,
                count=1,
                config={**plan.overrides, "seed": seed},
            ))

    storage = experiment.storage.model_dump(exclude_none=True)
    observability = experiment.observability.model_dump(exclude_none=True)
    local_spans_path = observability.get("local_spans_path")
    if local_spans_path:
        observability["local_spans_path"] = str((source_path.parent / local_spans_path).resolve())
    return ExecutionPlan(
        name=experiment.name,
        description=experiment.description,
        storage=storage,
        observability=observability,
        defaults=defaults,
        cells=cells,
    )


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def expand_cells(
    spec: ExecutionPlan,
    resolve_config: Callable[[dict], dict] | dict[str, Callable[[dict], dict]] | None = None,
) -> list[tuple[CellPlan, dict[str, Any]]]:
    """For each cell, return (cell, resolved_config).

    The config is preset -> defaults -> cell overrides, deep-merged. If the environment
    registered a ``resolve_config`` hook it runs once per cell here — before
    fan-out, so every run in the cell shares one derived scenario and that
    scenario is recorded in each run's config.

    ``resolve_config`` may be a single callable (applied to every cell) or a
    ``{environment_id: callable}`` mapping. The mapping form is what lets one
    experiment span several games, each with its own resolver.
    """
    out: list[tuple[CellPlan, dict[str, Any]]] = []
    for cell in spec.cells:
        preset_name = cell.preset or cell.config.get("preset") or spec.defaults.get("preset")
        preset_config: dict[str, Any] = {}
        if preset_name:
            if preset_name not in spec.presets:
                raise ValueError(
                    f"Unknown preset {preset_name!r} in cell {cell.label!r}. "
                    f"Known: {sorted(spec.presets)}"
                )
            preset_config = spec.presets[preset_name]

        defaults = {k: v for k, v in spec.defaults.items() if k != "preset"}
        overrides = {k: v for k, v in cell.config.items() if k != "preset"}

        resolved = _deep_merge(_deep_merge(preset_config, defaults), overrides)
        if preset_name:
            resolved["preset"] = preset_name

        hook = resolve_config
        if isinstance(resolve_config, dict):
            hook = resolve_config.get(str(resolved.get("environment_id") or ""))
        if hook is not None:
            resolved = hook(resolved)

        out.append((cell, resolved))
    return out
