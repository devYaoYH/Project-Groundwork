"""Calendar design contract tests for deterministic scripted policies."""
from __future__ import annotations

from pathlib import Path

import pytest

from a2a_engine.compiler import compile, validate
from a2a_engine.design import parse_design_text
from a2a_engine.items import ItemBank
from calendar_game.declaration import DECLARATION


_BANK = ItemBank.load(
    Path(__file__).resolve().parents[2] / "tasks" / "tiny_varied_density_5a3p.jsonl"
)


def _design(binding: str, *, kind: str = "scripted"):
    roster = "\n".join(
        f"  - id: calendar-agent_{index}\n"
        "    role: calendar-agent\n"
        f"    kind: {kind}\n"
        f"    binding: {binding}"
        for index in range(5)
    )
    return parse_design_text(f"""schema_version: 1
release: calendar@v1
parameters:
  density: {{randomize: true}}
  num_slots: {{randomize: true}}
  num_meetings: {{randomize: true}}
  communication_protocol: {{pin: dm}}
units:
  episodes_per_cell: 1
roster:
{roster}
seed:
  root: 42
""")


def test_calendar_design_rejects_unknown_scripted_policy_binding():
    errors = validate(_design("unknown"), DECLARATION, _BANK)

    assert any(
        error.path == "roster[0].binding"
        and "unsupported scripted binding 'unknown'" in error.message
        for error in errors
    )


def test_calendar_design_keeps_llm_binding_as_the_model_name():
    plan = compile(
        _design("openai/gpt-5-mini", kind="llm"),
        DECLARATION,
        _BANK,
        experiment_id="calendar-policy-test",
        experiment_name="Calendar policy test",
    )

    assert plan.preview_episode_config["agents"] == [
        {
            "id": f"calendar-agent_{index}",
            "type": "llm",
            "model": "openai/gpt-5-mini",
        }
        for index in range(5)
    ]


@pytest.mark.parametrize(
    ("binding", "runtime_type"),
    [
        ("baseline", "scripted"),
        ("dsm", "dsm"),
        ("paper_dsm", "paper_dsm"),
        ("private_dsm", "private_dsm"),
        ("imap", "imap"),
        ("sd", "sd"),
    ],
)
def test_calendar_design_compiles_scripted_policy_binding_to_runtime_type(binding, runtime_type):
    plan = compile(
        _design(binding),
        DECLARATION,
        _BANK,
        experiment_id="calendar-policy-test",
        experiment_name="Calendar policy test",
    )

    agents = plan.preview_episode_config["agents"]
    assert [agent["type"] for agent in agents] == [runtime_type] * 5
    assert [agent["binding"] for agent in agents] == [binding] * 5


def test_mixed_runtime_roster_and_topology_profiles_compile_before_hashing():
    from a2a_engine.design import Disposition
    from a2a_engine.registry import get_environment_spec
    import calendar_game.game
    design = _design("baseline")
    design.roster[1] = design.roster[1].model_copy(update={"runtime": "local_process", "harness": "scripted"})
    design.roster[2] = design.roster[2].model_copy(update={"runtime": "external", "kind": "llm", "binding": None})
    design.parameters["communication_topology"] = Disposition(factor=["ring", "phase_shift"])
    assert validate(design, DECLARATION, _BANK) == []
    plan = compile(design, DECLARATION, _BANK, experiment_id="mixed", experiment_name="mixed")
    assert len(plan.cells) == 2
    cfg = plan.preview_episode_config
    assert cfg["agents"][1]["runtime"] == "local_process"
    assert cfg["agents"][1]["harness"] == "scripted"
    assert cfg["agents"][2] == {"id": "calendar-agent_2", "type": "llm", "runtime": "external"}
    assert "communication_topology" not in cfg
    assert cfg["communication"]["topology"]["default"]["graph"] == "ring"
    assert cfg["release"]["declared_adapter_bindings"]["communication"] == "local.in_process"
    assert get_environment_spec("calendar").resolve_config is not None


def test_topology_profile_resolution_is_idempotent_and_rejects_conflicting_direct_policy():
    from copy import deepcopy
    from calendar_game.resolve import resolve_config
    authored = {"communication_topology": "phase_shift"}
    resolved = resolve_config(authored)
    assert authored == {"communication_topology": "phase_shift"}
    assert resolve_config(resolved) == resolved
    assert resolved["communication"]["topology"]["phases"]["DECISION"]["channels"] == {}
    with pytest.raises(ValueError, match="conflicts"):
        resolve_config({**deepcopy(resolved), **authored})
    with pytest.raises(ValueError, match="unknown"):
        resolve_config({"communication_topology": "missing"})
