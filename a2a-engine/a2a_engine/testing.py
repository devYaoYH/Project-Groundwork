"""Check-in assertions shared by environment release declarations."""

from __future__ import annotations

from types import UnionType
from typing import Any, Union, get_args, get_origin

from .environment import ParameterConfig, ReleaseDeclaration
from .items import ItemBank, derive_item_domain


def _contains(annotation: Any, expected: type) -> bool:
    if annotation is expected:
        return True
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return any(_contains(argument, expected) for argument in get_args(annotation))
    return False


def _compatible(parameter: ParameterConfig, annotation: Any) -> bool:
    if parameter.type == "boolean":
        return _contains(annotation, bool)
    if parameter.type == "integer":
        return _contains(annotation, int) and not _contains(annotation, bool)
    if parameter.type == "continuous":
        return _contains(annotation, float) or _contains(annotation, int)
    return _contains(annotation, str)


def assert_declaration_matches_config(
    declaration: ReleaseDeclaration, config_cls: type[Any]
) -> None:
    """Assert every declared parameter is an assignable config field.

    Declarations add research semantics that Pydantic cannot infer, but their
    names and scalar types must stay coupled to the executable config model.
    """

    fields = getattr(config_cls, "model_fields", {})
    for parameter in declaration.parameters:
        field = fields.get(parameter.name)
        if field is None:
            raise AssertionError(
                f"declaration parameter {parameter.name!r} is absent from {config_cls.__name__}"
            )
        if not _compatible(parameter, field.annotation):
            raise AssertionError(
                f"declaration parameter {parameter.name!r} ({parameter.type}) is incompatible with "
                f"{config_cls.__name__}.{parameter.name} ({field.annotation!r})"
            )


def assert_item_parameters_match_bank(
    declaration: ReleaseDeclaration, bank: ItemBank
) -> None:
    """Assert every item-sourced parameter names a column the bank actually has.

    The counterpart to :func:`assert_declaration_matches_config`: that one keeps
    a declaration honest about the config class, this one keeps it honest about
    the item bank.  Levels are projected rather than declared, so the only way
    an item parameter can lie is by naming a column no row carries — and the
    projection raises on exactly that.
    """

    for parameter in declaration.parameters:
        if parameter.source != "item":
            continue
        try:
            domain, levels = derive_item_domain(parameter, bank)
        except KeyError as exc:
            raise AssertionError(str(exc)) from exc
        if not levels:
            raise AssertionError(
                f"item parameter {parameter.name!r} projects no levels from {bank.path.name}"
            )
        if parameter.type in {"continuous", "integer"} and domain is None:
            raise AssertionError(
                f"item parameter {parameter.name!r} is declared {parameter.type} but "
                f"{bank.path.name} holds no numeric value for {parameter.bank_key!r}"
            )
