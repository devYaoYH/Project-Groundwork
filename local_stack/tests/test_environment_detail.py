"""The environments API projects item levels without touching the declaration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from a2a_engine.registry import discover_environments
from local_stack.control_plane import ControlPlane


def _control(tmp_path: Path) -> ControlPlane:
    discover_environments()
    return ControlPlane(
        tmp_path / "control.db",
        workspace=Path(__file__).resolve().parents[2],
        trace_database=tmp_path / "episodes.db",
    )


def _parameter(detail: dict, name: str) -> dict:
    return next(parameter for parameter in detail["parameters"] if parameter["name"] == name)


def test_item_levels_are_projected_from_the_bank_with_row_counts(tmp_path):
    detail = _control(tmp_path).environment_detail("negotiation")

    mc_ratio = _parameter(detail, "mc_ratio")
    assert mc_ratio["source"] == "item"
    assert mc_ratio["item_key"] == "mc_bucket"
    # Three buckets the pool was filtered into, not the (0, 1) a hand-written
    # domain used to advertise.
    assert mc_ratio["levels"] == [
        {"value": 0.5, "count": 4},
        {"value": 0.8, "count": 5},
        {"value": 1.0, "count": 6},
    ]
    assert mc_ratio["domain"] == [0.5, 1.0]
    assert mc_ratio["identity_grained"] is False


def test_design_parameters_keep_their_declared_domain_and_project_no_levels(tmp_path):
    mode = _parameter(_control(tmp_path).environment_detail("negotiation"), "mode")

    assert mode["source"] == "design"
    assert mode["domain"] == ["stable", "rotating"]
    assert mode["levels"] is None


def test_a_column_with_one_value_per_item_is_flagged_as_identity_grained(tmp_path):
    detail = _control(tmp_path).environment_detail("word_guess")
    secret_word = _parameter(detail, "secret_word")

    assert secret_word["identity_grained"] is True
    assert len(secret_word["levels"]) == detail["item_policy"]["item_count"]


def test_the_declaration_digest_does_not_move_with_the_projection(tmp_path):
    """Derived levels ride alongside the declaration, never inside it.

    ``declaration_sha256`` must stay a function of the checked-in source so a
    locked design can be checked against it.
    """

    control = _control(tmp_path)
    detail = control.environment_detail("calendar")
    declaration = control._declaration("calendar")

    assert detail["release"]["declaration_sha256"] == declaration.content_sha256()
    assert "levels" not in detail["declaration_yaml"]
    assert _parameter(detail, "density")["levels"] is not None
