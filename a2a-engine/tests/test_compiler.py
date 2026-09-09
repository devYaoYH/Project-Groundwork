"""Design compilation: factorial expansion, item selection, and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_engine.compiler import compile, validate
from a2a_engine.design import DesignValidationError, parse_design_text
from a2a_engine.environment import (
    EngineConfig,
    ItemPolicy,
    ParameterConfig,
    ReleaseDeclaration,
    RoleConfig,
)
from a2a_engine.items import ItemBank


def release_and_bank(tmp_path: Path, *, mode: str = "enumerate"):
    path = tmp_path / "items.jsonl"
    rows = [
        {"item_id": "one", "params": {"difficulty": "easy", "size": 1}},
        {"item_id": "two", "params": {"difficulty": "easy", "size": 2}},
        {"item_id": "three", "params": {"difficulty": "hard", "size": 1}},
        {"item_id": "four", "params": {"difficulty": "hard", "size": 2}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    bank = ItemBank.load(path)
    declaration = ReleaseDeclaration(
        id="test_release",
        environment_id="test",
        version="v1",
        engine=EngineConfig(environment_id="test", defaults={"answer": "release-default"}),
        parameters=[
            ParameterConfig(name="difficulty", type="categorical", source="item"),
            ParameterConfig(name="size", type="integer", source="item"),
            ParameterConfig(
                name="treatment", type="categorical", domain=["control", "active"]
            ),
            ParameterConfig(name="enabled", type="boolean", domain=[True, False]),
        ],
        roles=[RoleConfig(id="player", accepts=["scripted"])],
        item_policy=ItemPolicy(
            mode=mode, bank_path="items.jsonl", item_bank_sha256=bank.item_bank_sha256
        ),
        oracle_version="test-oracle-v1",
    )
    return declaration, bank


def design(parameters: str, *, episodes: int = 1, mode: str = "derived"):
    return parse_design_text(
        f"""schema_version: 1
release: test@v1
parameters:
{parameters}units:
  episodes_per_cell: {episodes}
roster:
  - id: player
    role: player
    kind: scripted
    binding: baseline
seed:
  mode: {mode}
  root: 42
"""
    )


@pytest.mark.parametrize(
    ("parameters", "expected_cells"),
    [
        ("  difficulty: {factor: [easy, hard]}\n  size: {pin: 1}\n", 2),
        (
            "  difficulty: {factor: [easy, hard]}\n  size: {pin: 1}\n"
            "  treatment: {factor: [control, active]}\n",
            4,
        ),
        (
            "  difficulty: {factor: [easy, hard]}\n  size: {pin: 1}\n"
            "  treatment: {factor: [control, active]}\n"
            "  enabled: {factor: [true, false]}\n",
            8,
        ),
    ],
)
def test_compiler_expands_one_two_and_three_factored_parameters(
    tmp_path, parameters, expected_cells
):
    declaration, bank = release_and_bank(tmp_path)
    plan = compile(
        design(parameters), declaration, bank, experiment_id="e1", experiment_name="study"
    )

    assert len(plan.cells) == expected_cells
    assert plan.episodes_planned == expected_cells
    assert all(
        episode.config["provenance"]["experiment_id"] == "e1"
        for cell in plan.cells
        for episode in cell.episodes
    )
    assert all(
        "role" not in agent
        for cell in plan.cells
        for episode in cell.episodes
        for agent in episode.config["agents"]
    )


def test_compilation_is_byte_identical_for_the_same_design(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    authored = design("  difficulty: {pin: easy}\n  size: {randomize: true}\n")

    first = compile(authored, declaration, bank, experiment_id="e1", experiment_name="study")
    second = compile(authored, declaration, bank, experiment_id="e1", experiment_name="study")

    assert first.preview_episode_config == second.preview_episode_config


def test_disposition_factor_and_pin_apply_differently_for_item_and_design_parameters(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    authored = design(
        "  difficulty: {factor: [easy, hard]}\n"
        "  size: {pin: 1}\n"
        "  treatment: {factor: [control, active]}\n"
        "  enabled: {pin: true}\n"
    )
    plan = compile(authored, declaration, bank, experiment_id="e1", experiment_name="study")

    assert len(plan.cells) == 4
    for cell in plan.cells:
        episode = cell.episodes[0]
        assert episode.config["size"] == 1
        assert episode.config["enabled"] is True
        assert episode.config["treatment"] in {"control", "active"}
        assert episode.config["difficulty"] in {"easy", "hard"}


def test_disposition_randomize_records_item_attributes_without_narrowing_the_pool(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    authored = design(
        "  difficulty: {randomize: true}\n  size: {randomize: true}\n", episodes=4
    )
    plan = compile(authored, declaration, bank, experiment_id="e1", experiment_name="study")
    episodes = plan.cells[0].episodes

    assert {episode.config["difficulty"] for episode in episodes} == {"easy", "hard"}
    assert {episode.config["size"] for episode in episodes} == {1, 2}
    assert all(
        set(episode.config["provenance"]["item_attributes"]) == {"difficulty", "size"}
        for episode in episodes
    )


def test_missing_disposition_and_randomize_on_design_are_rejected(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    authored = design(
        "  difficulty: {randomize: true}\n  treatment: {randomize: true}\n"
    )
    errors = validate(authored, declaration, bank)
    messages = " ".join(error.message for error in errors)

    assert "missing disposition" in messages
    assert "levels 1, 2" in messages
    assert "design-sourced" in messages


def test_unknown_stratum_and_enumerate_capacity_fail_before_compile(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    authored = design(
        "  difficulty: {pin: impossible}\n  size: {pin: 1}\n", episodes=2
    )
    errors = validate(authored, declaration, bank)

    assert any("no stratum" in error.message for error in errors)
    with pytest.raises(DesignValidationError):
        compile(authored, declaration, bank, experiment_id="e1", experiment_name="study")


def test_static_seed_is_rejected_for_a_sampling_release(tmp_path):
    declaration, bank = release_and_bank(tmp_path, mode="sample")
    authored = design(
        "  difficulty: {randomize: true}\n  size: {randomize: true}\n", mode="static"
    )

    assert any(error.path == "seed.mode" for error in validate(authored, declaration, bank))


def test_roster_roles_must_be_declared_unique_and_kind_compatible(tmp_path):
    declaration, bank = release_and_bank(tmp_path)
    declaration = declaration.model_copy(update={
        "roles": [
            RoleConfig(id="player", accepts=["human"]),
            RoleConfig(id="opponent", accepts=["scripted"]),
        ],
    })

    def authored(roster: str):
        return parse_design_text(f"""schema_version: 1
release: test@v1
parameters:
  difficulty: {{pin: easy}}
  size: {{pin: 1}}
units:
  episodes_per_cell: 1
roster:
{roster}seed:
  root: 42
""")

    missing = validate(authored("  - id: one\n    kind: scripted\n    binding: baseline\n"), declaration, bank)
    assert any(issue.path == "roster[0].role" and "select" in issue.message for issue in missing)

    duplicate = validate(authored(
        "  - id: one\n    role: player\n    kind: human\n"
        "  - id: two\n    role: player\n    kind: human\n"
    ), declaration, bank)
    assert any("role 'player' requires exactly 1" in issue.message for issue in duplicate)
    assert any("role 'opponent' requires exactly 1" in issue.message for issue in duplicate)

    unknown = validate(authored(
        "  - id: one\n    role: unknown\n    kind: human\n"
        "  - id: two\n    role: opponent\n    kind: scripted\n    binding: baseline\n"
    ), declaration, bank)
    assert any(issue.path == "roster[0].role" and "unknown declared role" in issue.message for issue in unknown)

    incompatible = validate(authored(
        "  - id: one\n    role: player\n    kind: scripted\n    binding: baseline\n"
        "  - id: two\n    role: opponent\n    kind: scripted\n    binding: baseline\n"
    ), declaration, bank)
    assert any(issue.path == "roster[0].kind" and "accepts human" in issue.message for issue in incompatible)
