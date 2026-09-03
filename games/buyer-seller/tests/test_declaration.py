from pathlib import Path

import pytest

from a2a_engine.compiler import compile
from a2a_engine.design import parse_design_text
from a2a_engine.registry import get_environment_spec
from a2a_engine.items import ItemBank
from a2a_engine.testing import (
    assert_declaration_matches_config,
    assert_item_parameters_match_bank,
)
from buyer_seller.game import BuyerSellerConfig, best_joint_utility


def test_buyer_seller_declaration_matches_the_config_contract():
    declaration = get_environment_spec("buyer_seller").declaration
    assert declaration is not None
    assert_declaration_matches_config(declaration, BuyerSellerConfig)


def test_buyer_seller_item_parameters_are_backed_by_the_pinned_bank():
    declaration = get_environment_spec("buyer_seller").declaration
    assert declaration is not None
    bank = ItemBank.load(
        Path(__file__).resolve().parents[3] / declaration.item_policy.bank_path,
        expected_sha256=declaration.item_policy.item_bank_sha256,
    )
    assert_item_parameters_match_bank(declaration, bank)


def test_discount_factor_is_item_sourced_and_changes_the_pinned_oracle_result():
    declaration = get_environment_spec("buyer_seller").declaration
    assert declaration is not None
    discount_factor = next(
        parameter for parameter in declaration.parameters if parameter.name == "discount_factor"
    )
    assert discount_factor.source == "item"

    bank = ItemBank.load(
        Path(__file__).resolve().parents[3] / declaration.item_policy.bank_path,
        expected_sha256=declaration.item_policy.item_bank_sha256,
    )
    half = bank.get("buyer_seller.low_cost.delta_050")
    full = bank.get("buyer_seller.low_cost.delta_100")
    assert {key: half.params[key] for key in ("seller_cost", "buyer_value", "num_items")} == {
        key: full.params[key] for key in ("seller_cost", "buyer_value", "num_items")
    }
    assert half.params["discount_factor"] == 0.5
    assert full.params["discount_factor"] == 1.0
    assert half.oracle_result["best_joint_utility"] == 38.5
    assert full.oracle_result["best_joint_utility"] == 66.0
    assert half.oracle_result["best_joint_utility"] != full.oracle_result["best_joint_utility"]
    for item in (half, full):
        assert item.oracle_result["best_joint_utility"] == best_joint_utility(
            item.params["seller_cost"],
            item.params["buyer_value"],
            item.params["num_items"],
            item.params["discount_factor"],
        )


@pytest.mark.parametrize(
    ("discount_disposition", "episodes_per_cell", "expected_cells"),
    [
        ("{factor: [0.5, 1.0]}", 1, 2),
        ("{pin: 0.5}", 1, 1),
        ("{randomize: true}", 2, 1),
    ],
)
def test_discount_dispositions_select_complete_oracle_bearing_item_rows(
    discount_disposition, episodes_per_cell, expected_cells
):
    """Delta dispositions select rows; they never directly write a design value."""

    declaration = get_environment_spec("buyer_seller").declaration
    assert declaration is not None
    bank = ItemBank.load(
        Path(__file__).resolve().parents[3] / declaration.item_policy.bank_path,
        expected_sha256=declaration.item_policy.item_bank_sha256,
    )
    design = parse_design_text(f"""schema_version: 1
release: buyer_seller@v1
parameters:
  seller_cost: {{pin: 8}}
  buyer_value: {{pin: 30}}
  num_items: {{pin: 3}}
  discount_factor: {discount_disposition}
units:
  episodes_per_cell: {episodes_per_cell}
roster:
  - id: seller
    kind: scripted
    binding: seller-baseline
  - id: buyer
    kind: scripted
    binding: buyer-baseline
seed:
  root: 41
""")

    plan = compile(design, declaration, bank, experiment_id="oracle-test", experiment_name="oracle-test")

    assert len(plan.cells) == expected_cells
    for cell in plan.cells:
        for episode in cell.episodes:
            item = bank.get(episode.config["item_id"])
            assert episode.config["discount_factor"] == item.params["discount_factor"]
            assert episode.config["discount_factor"] in {0.5, 1.0}
            assert item.oracle_result["best_joint_utility"] == best_joint_utility(
                item.params["seller_cost"],
                item.params["buyer_value"],
                item.params["num_items"],
                item.params["discount_factor"],
            )
            if discount_disposition == "{randomize: true}":
                assert episode.config["provenance"]["item_attributes"] == {
                    "discount_factor": item.params["discount_factor"]
                }
