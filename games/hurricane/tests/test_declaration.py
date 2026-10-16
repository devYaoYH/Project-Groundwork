from pathlib import Path

from a2a_engine.items import ItemBank
from a2a_engine.registry import get_environment_spec
from a2a_engine.testing import (
    assert_declaration_matches_config,
    assert_item_parameters_match_bank,
)
from hurricane.environment import HurricaneConfig, HurricaneEnvironment


def declaration():
    spec = get_environment_spec("hurricane").declaration
    assert spec is not None
    return spec


def test_declaration_matches_the_config_contract():
    assert_declaration_matches_config(declaration(), HurricaneConfig)


def test_item_parameters_are_backed_by_the_pinned_bank():
    policy = declaration().item_policy
    bank = ItemBank.load(
        Path(__file__).resolve().parents[3] / policy.bank_path,
        expected_sha256=policy.item_bank_sha256,
    )
    assert_item_parameters_match_bank(declaration(), bank)
    assert {item.item_id for item in bank.items} == {
        "storm.central_city",
        "storm.northern_edge",
        "storm.southern_village",
        "storm.wide_front",
    }


def test_release_yaml_pins_the_same_bank_as_the_declaration():
    import yaml

    release = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "environments/hurricane_tiny_v1.yaml").read_text()
    )
    assert release["item_policy"]["item_bank_sha256"] == declaration().item_policy.item_bank_sha256
    assert release["inputs"][0]["sha256"] == declaration().item_policy.item_bank_sha256
    assert {m["name"] for m in release["metrics"]} <= {m.name for m in declaration().measures}


def test_every_declared_environment_measure_is_emitted():
    trace = HurricaneEnvironment({"num_rounds": 2}, dry_run=True).run()
    declared = {m.name for m in declaration().measures if m.producer == "environment"}
    assert declared <= set(trace.metrics)
    assert all(isinstance(trace.metrics[name], (int, float)) for name in declared)
