from pathlib import Path

from a2a_engine.items import ItemBank
from a2a_engine.registry import get_environment_spec
from a2a_engine.testing import (
    assert_declaration_matches_config,
    assert_item_parameters_match_bank,
)
from negotiation_game.game import NegotiationConfig


def test_negotiation_declaration_matches_the_config_contract():
    declaration = get_environment_spec("negotiation").declaration
    assert declaration is not None
    assert_declaration_matches_config(declaration, NegotiationConfig)


def test_negotiation_item_parameters_are_backed_by_the_pinned_bank():
    declaration = get_environment_spec("negotiation").declaration
    assert declaration is not None
    bank = ItemBank.load(
        Path(__file__).resolve().parents[3] / declaration.item_policy.bank_path,
        expected_sha256=declaration.item_policy.item_bank_sha256,
    )
    assert_item_parameters_match_bank(declaration, bank)


def test_compiled_bank_scenario_is_frozen_and_never_sent_to_the_generator(monkeypatch):
    from a2a_engine.compiler import compile
    from a2a_engine.design import parse_design_text
    from a2a_engine.experiment import ExecutionPlan, expand_cells
    from negotiation_game.backend import optimizer
    from negotiation_game.resolve import resolve_config

    def unexpected_generation(*args, **kwargs):
        raise AssertionError("A frozen bank item must not invoke the stochastic generator")

    monkeypatch.setattr(optimizer, "generate_scenario", unexpected_generation)
    declaration = get_environment_spec("negotiation").declaration
    bank = ItemBank.load(Path(__file__).resolve().parents[3] / declaration.item_policy.bank_path)
    design = parse_design_text("""schema_version: 1
release: negotiation@v1
parameters:
  mc_ratio: {pin: 0.5}
  num_rounds: {pin: 1}
  cheap_talk_turns: {pin: 0}
units: {episodes_per_cell: 1}
roster:
  - {id: a, role: negotiator, kind: scripted, binding: baseline}
  - {id: b, role: negotiator, kind: scripted, binding: baseline}
seed: {root: 1}
""")
    plan = compile(
        design, declaration, bank, experiment_id="bank-regression", experiment_name="quick-play"
    )
    config = plan.preview_episode_config
    item = bank.get(config["item_id"])
    for key, value in item.params.items():
        if key == "oracle_stats":
            assert all(config[key][name] == result for name, result in value.items())
        else:
            assert config[key] == value
    assert config["oracle_stats"] == item.oracle_result
    assert config["item_bank_sha256"] == bank.item_bank_sha256
    assert "mc_ratio" not in config
    assert config["num_rounds"] == 1 and config["cheap_talk_turns"] == 0
    assert config["provenance"]["item_id"] == item.item_id

    # Execution consumes the frozen payload even if the bank is unavailable.
    def unexpected_bank_read(*args, **kwargs):
        raise AssertionError("Execution must not reload a frozen scenario")

    monkeypatch.setattr(ItemBank, "load", unexpected_bank_read)
    execution = ExecutionPlan(name="quick-play", cells=[{"label": "one", "config": config}])
    assert expand_cells(execution, resolve_config={"negotiation": resolve_config})[0][1] == config
