from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from a2a_engine.declaration import ParameterConfig, ReleaseDeclaration
from a2a_engine.testing import assert_declaration_matches_config


class DemoConfig(BaseModel):
    rate: float
    count: int
    enabled: bool
    label: str


def _declaration(*parameters: ParameterConfig) -> ReleaseDeclaration:
    return ReleaseDeclaration(
        environment_id="demo",
        version="v1",
        parameters=list(parameters),
    )


def test_declared_parameters_must_exist_with_compatible_scalar_types():
    declaration = _declaration(
        ParameterConfig(name="rate", type="continuous", domain=(0.0, 1.0)),
        ParameterConfig(name="count", type="integer", domain=(1.0, 10.0)),
        ParameterConfig(name="enabled", type="boolean", domain=[True, False]),
        ParameterConfig(name="label", type="categorical", domain=["a", "b"]),
    )

    assert_declaration_matches_config(declaration, DemoConfig)


def test_missing_or_incompatible_parameters_fail_the_check_in_assertion():
    missing = _declaration(
        ParameterConfig(name="absent", type="integer", domain=(1.0, 2.0))
    )
    with pytest.raises(AssertionError, match="absent"):
        assert_declaration_matches_config(missing, DemoConfig)

    incompatible = _declaration(
        ParameterConfig(name="label", type="boolean", domain=[True, False])
    )
    with pytest.raises(AssertionError, match="incompatible"):
        assert_declaration_matches_config(incompatible, DemoConfig)


def test_item_sourced_parameters_must_not_restate_the_bank_as_a_domain():
    # The failure mode this whole distinction exists to prevent: word-guess once
    # declared six secret words against a bank holding four.
    with pytest.raises(ValidationError, match="derived from the item bank"):
        ParameterConfig(
            name="secret_word",
            type="categorical",
            domain=["dog", "apple", "car"],
            source="item",
        )


def test_release_fixed_does_not_apply_to_item_sourced_parameters():
    with pytest.raises(ValidationError, match="release-fixed does not apply"):
        ParameterConfig(name="density", type="continuous",
                        source="item", fixed=True)


def test_item_key_is_meaningless_without_an_item_source():
    with pytest.raises(ValidationError, match="item_key only applies"):
        ParameterConfig(name="mc_ratio", type="continuous", domain=(0.0, 1.0), item_key="mc_bucket")


def test_design_sourced_is_the_default_and_keeps_its_declared_domain():
    parameter = ParameterConfig(
        name="max_turns", type="integer", domain=(1.0, 50.0)
    )
    assert parameter.source == "design"
    assert parameter.domain == (1.0, 50.0)
    assert parameter.bank_key == "max_turns"


def test_bank_key_falls_back_to_the_parameter_name():
    named = ParameterConfig(name="mc_ratio", type="continuous",
                            source="item", item_key="mc_bucket")
    plain = ParameterConfig(name="density", type="continuous",
                            source="item")
    assert named.bank_key == "mc_bucket"
    assert plain.bank_key == "density"
