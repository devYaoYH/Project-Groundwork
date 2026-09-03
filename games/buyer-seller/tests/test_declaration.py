from pathlib import Path

from a2a_engine.registry import get_environment_spec
from a2a_engine.items import ItemBank
from a2a_engine.testing import (
    assert_declaration_matches_config,
    assert_item_parameters_match_bank,
)
from buyer_seller.game import BuyerSellerConfig


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
