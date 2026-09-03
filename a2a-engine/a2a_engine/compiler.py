"""Authoritative compilation from a research design to episode configs.

The compiler is deliberately server-side Python.  It sees the pinned release
and item bank, validates both before spend, and stamps the provenance that a
browser must never be trusted to create.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import random
from dataclasses import dataclass
from typing import Any, Iterable

from .design import Design, DesignValidationError, ValidationIssue
from .environment import ParameterConfig, ReleaseDeclaration
from .items import Item, ItemBank
from .provenance import build_provenance
from .seeds import derive_seed


@dataclass(frozen=True)
class CompiledEpisode:
    cell_id: str
    episode_idx: int
    config: dict[str, Any]


@dataclass(frozen=True)
class CompiledCell:
    cell_id: str
    levels: dict[str, Any]
    episodes: tuple[CompiledEpisode, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    """A fixed, fully expanded plan ready to become runner input."""

    cells: tuple[CompiledCell, ...]

    @property
    def episodes_planned(self) -> int:
        return sum(len(cell.episodes) for cell in self.cells)

    @property
    def preview_episode_config(self) -> dict[str, Any] | None:
        for cell in self.cells:
            if cell.episodes:
                return copy.deepcopy(cell.episodes[0].config)
        return None

    def as_api_dict(self) -> dict[str, Any]:
        return {
            "cells": [
                {
                    "cell_id": cell.cell_id,
                    "levels": cell.levels,
                    "episodes_planned": len(cell.episodes),
                }
                for cell in self.cells
            ],
            "episodes_planned": self.episodes_planned,
            "preview_episode_config": self.preview_episode_config,
        }


def validate(
    design: Design,
    declaration: ReleaseDeclaration,
    bank: ItemBank,
    *,
    release_id: str | None = None,
) -> list[ValidationIssue]:
    """Return every semantic error without compiling a partial plan."""

    issues: list[ValidationIssue] = []
    parameters = {parameter.name: parameter for parameter in declaration.parameters}
    _validate_release_reference(design, declaration, release_id, issues)
    _validate_dispositions(design, parameters, bank, issues)
    _validate_roster(design, declaration, issues)

    if declaration.item_policy is None:
        issues.append(ValidationIssue("release", "the selected release has no item policy"))
        return issues
    if design.seed.mode == "static" and declaration.item_policy.mode == "sample":
        issues.append(ValidationIssue(
            "seed.mode", "static seeds are incompatible with a release that samples items"
        ))

    if issues:
        return issues

    item_levels = _item_levels(design, parameters)
    for levels in _cell_levels(design):
        pool = _pool(bank.items, item_levels, levels, parameters)
        if not pool:
            formatted = _format_levels({**_pins(design), **levels})
            issues.append(ValidationIssue(
                "parameters", f"cell selects no items from the pinned bank: {formatted}"
            ))
            continue
        if declaration.item_policy.mode == "enumerate" and design.units.episodes_per_cell > len(pool):
            issues.append(ValidationIssue(
                "units.episodes_per_cell",
                f"enumerate policy has {len(pool)} item(s) for cell {_format_levels(levels)}, "
                f"but the design requests {design.units.episodes_per_cell} episodes",
            ))
    return issues


def compile(
    design: Design,
    declaration: ReleaseDeclaration,
    bank: ItemBank,
    *,
    experiment_id: str,
    experiment_name: str,
    release_id: str | None = None,
) -> ExecutionPlan:
    """Compile one valid design into exact, provenance-stamped episode configs."""

    errors = validate(design, declaration, bank, release_id=release_id)
    if errors:
        raise DesignValidationError(errors)
    if declaration.item_policy is None:  # guarded above; narrows the type for mypy/readers
        raise ValueError("release declaration has no item policy")

    release_id = release_id or str(declaration.id or declaration.environment_id)
    release_facts = {
        "release_id": release_id,
        "release_version": declaration.version,
        "declaration_sha256": declaration.content_sha256(),
        "item_bank_sha256": declaration.item_policy.item_bank_sha256,
        "oracle_version": declaration.oracle_version,
    }
    parameters = {parameter.name: parameter for parameter in declaration.parameters}
    design_sha256 = design.content_sha256()
    pinned = _pins(design)
    randomised = _randomized(design)
    compiled_cells: list[CompiledCell] = []

    for axis_levels in _cell_levels(design):
        levels = {**pinned, **axis_levels}
        cell_id = _cell_id(levels)
        pool = _pool(bank.items, _item_levels(design, parameters), axis_levels, parameters)
        episodes: list[CompiledEpisode] = []
        for episode_idx in range(design.units.episodes_per_cell):
            seed = derive_seed(
                design.seed.root, cell_id, episode_idx, mode=design.seed.mode
            )
            item = _select_item(pool, declaration.item_policy.mode, episode_idx, seed)
            config = _base_config(declaration, design)
            config.update(_design_levels(levels, parameters))
            # These values are supplied by the frozen selected row, not by the
            # design.  A factor or pin only selected the row; it never supplied
            # a scalar that could disagree with the bank.
            config.update(_selected_item_values(item, declaration.parameters))
            config["item_id"] = item.item_id
            config["experiment_name"] = experiment_name
            config["episode_id"] = f"{experiment_name}.{cell_id}.{episode_idx:03d}"
            config["seed"] = seed
            config["root_seed"] = design.seed.root
            config["seed_mode"] = design.seed.mode
            config["release"] = _trace_release(declaration, release_id)
            config["provenance"] = build_provenance(
                config=config,
                experiment_name=experiment_name,
                cell_id=cell_id,
                episode_idx=episode_idx,
                release=release_facts,
                experiment_id=experiment_id,
                design_sha256=design_sha256,
                item_id=item.item_id,
                item_attributes={
                    name: item.attribute(parameters[name].bank_key) for name in randomised
                },
            )
            episodes.append(CompiledEpisode(cell_id, episode_idx, config))
        compiled_cells.append(CompiledCell(cell_id, levels, tuple(episodes)))
    return ExecutionPlan(tuple(compiled_cells))


def _validate_release_reference(
    design: Design,
    declaration: ReleaseDeclaration,
    release_id: str | None,
    issues: list[ValidationIssue],
) -> None:
    environment_id = declaration.environment_id or ""
    version = declaration.version or "v1"
    accepted = {str(declaration.id or environment_id), f"{environment_id}@{version}"}
    if release_id:
        accepted.add(release_id)
    if design.release not in accepted:
        issues.append(ValidationIssue(
            "release", f"design pins {design.release!r}, not the selected release "
            f"{environment_id}@{version!s}"
        ))


def _validate_dispositions(
    design: Design,
    parameters: dict[str, ParameterConfig],
    bank: ItemBank,
    issues: list[ValidationIssue],
) -> None:
    for name, disposition in design.parameters.items():
        path = f"parameters.{name}"
        parameter = parameters.get(name)
        if parameter is None:
            issues.append(ValidationIssue(path, f"unknown parameter {name!r}"))
            continue
        if parameter.fixed:
            issues.append(ValidationIssue(path, f"parameter {name!r} is fixed by the release"))
            continue
        if disposition.randomize is not None and parameter.source != "item":
            issues.append(ValidationIssue(
                path, f"parameter {name!r} is design-sourced; factor it or pin it because "
                "the release already provides its default"
            ))
        values = disposition.factor if disposition.factor is not None else (
            [disposition.pin] if disposition.pin is not None else []
        )
        for value in values:
            if not _value_matches_parameter(value, parameter):
                issues.append(ValidationIssue(
                    path, f"level {value!r} is outside the declared {parameter.type} domain"
                ))
                continue
            if parameter.source == "item" and not any(
                item.attribute(parameter.bank_key) == value for item in bank.items
            ):
                issues.append(ValidationIssue(
                    path, f"level {value!r} names no stratum in the pinned item bank"
                ))

    for parameter in parameters.values():
        if parameter.source != "item" or parameter.name in design.parameters:
            continue
        try:
            levels = bank.attribute_levels(parameter.bank_key)
        except KeyError as exc:
            issues.append(ValidationIssue(f"parameters.{parameter.name}", str(exc)))
            continue
        if len(levels) > 1:
            values = ", ".join(repr(value) for value, _ in levels)
            issues.append(ValidationIssue(
                f"parameters.{parameter.name}",
                f"missing disposition for item attribute {parameter.name!r}; "
                f"the pinned bank holds levels {values}",
            ))


def _validate_roster(
    design: Design, declaration: ReleaseDeclaration, issues: list[ValidationIssue]
) -> None:
    ids = [participant.id for participant in design.roster]
    if len(ids) != len(set(ids)):
        issues.append(ValidationIssue("roster", "participant ids must be unique"))
    for index, participant in enumerate(design.roster):
        if participant.kind != "human" and not participant.binding:
            issues.append(ValidationIssue(
                f"roster[{index}].binding", "a non-human participant needs a binding"
            ))
    expected_accepts = [
        role.accepts for role in declaration.roles for _ in range(role.count)
    ]
    if len(design.roster) != len(expected_accepts):
        issues.append(ValidationIssue(
            "roster", f"release requires {len(expected_accepts)} participant(s), "
            f"but the design names {len(design.roster)}"
        ))
        return
    for index, (participant, accepted) in enumerate(zip(design.roster, expected_accepts)):
        if participant.kind not in accepted:
            issues.append(ValidationIssue(
                f"roster[{index}].kind",
                f"role at position {index} accepts {', '.join(accepted)}, not {participant.kind}",
            ))


def _value_matches_parameter(value: Any, parameter: ParameterConfig) -> bool:
    if parameter.type == "boolean":
        return isinstance(value, bool) and (parameter.domain is None or value in parameter.domain)
    if parameter.type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return False
    elif parameter.type == "continuous":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
    elif parameter.type == "categorical":
        if not isinstance(value, str):
            return False
    if parameter.domain is None:
        return True
    if isinstance(parameter.domain, tuple):
        return parameter.domain[0] <= value <= parameter.domain[1]
    return value in parameter.domain


def _pins(design: Design) -> dict[str, Any]:
    return {
        name: disposition.pin
        for name, disposition in design.parameters.items()
        if disposition.pin is not None
    }


def _randomized(design: Design) -> list[str]:
    return [
        name for name, disposition in design.parameters.items()
        if disposition.randomize is True
    ]


def _item_levels(design: Design, parameters: dict[str, ParameterConfig]) -> dict[str, Any]:
    return {
        name: disposition
        for name, disposition in design.parameters.items()
        if parameters[name].source == "item" and disposition.randomize is not True
    }


def _cell_levels(design: Design) -> Iterable[dict[str, Any]]:
    axes = [
        (name, disposition.factor)
        for name, disposition in sorted(design.parameters.items())
        if disposition.factor is not None
    ]
    if not axes:
        return ({},)
    return tuple(
        dict(zip((name for name, _ in axes), values))
        for values in itertools.product(*(values for _, values in axes))
    )


def _pool(
    items: Iterable[Item],
    item_dispositions: dict[str, Any],
    axis_levels: dict[str, Any],
    parameters: dict[str, ParameterConfig],
) -> list[Item]:
    selected = {
        name: disposition.pin if disposition.pin is not None else axis_levels[name]
        for name, disposition in item_dispositions.items()
        if disposition.pin is not None or name in axis_levels
    }
    return [
        item for item in items
        if all(
            item.attribute(parameters[name].bank_key) == value
            for name, value in selected.items()
        )
    ]


def _design_levels(levels: dict[str, Any], parameters: dict[str, ParameterConfig]) -> dict[str, Any]:
    return {name: value for name, value in levels.items() if parameters[name].source == "design"}


def _selected_item_values(item: Item, parameters: Iterable[ParameterConfig]) -> dict[str, Any]:
    return {
        parameter.name: item.attribute(parameter.bank_key)
        for parameter in parameters
        if parameter.source == "item"
    }


def _select_item(pool: list[Item], mode: str, episode_idx: int, seed: int) -> Item:
    if not pool:
        raise ValueError("cannot select an item from an empty pool")
    if mode == "enumerate":
        return pool[episode_idx]
    return random.Random(seed).choice(pool)


def _base_config(declaration: ReleaseDeclaration, design: Design) -> dict[str, Any]:
    defaults = copy.deepcopy(declaration.engine.defaults if declaration.engine else {})
    defaults["environment_id"] = declaration.environment_id
    defaults.setdefault("num_agents", len(design.roster))
    defaults["agents"] = [
        {
            "id": participant.id,
            "type": participant.kind,
            **(
                {"model": participant.binding}
                if participant.kind == "llm" and participant.binding
                else ({"binding": participant.binding} if participant.binding else {})
            ),
        }
        for participant in design.roster
    ]
    return defaults


def _trace_release(declaration: ReleaseDeclaration, release_id: str) -> dict[str, Any]:
    return {
        "schema_version": declaration.schema_version,
        "id": release_id,
        "release": declaration.version,
        "content_sha256": declaration.content_sha256(),
        "inputs": [item.model_dump(mode="json") for item in declaration.inputs],
    }


def _cell_id(levels: dict[str, Any]) -> str:
    blob = json.dumps(levels, sort_keys=True, separators=(",", ":"), default=str)
    return f"cell-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:12]}"


def _format_levels(levels: dict[str, Any]) -> str:
    return json.dumps(levels, sort_keys=True, default=str)
