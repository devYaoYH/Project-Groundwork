"""Release declaration for the Calendar scheduling environment."""

from pathlib import Path

from a2a_engine.declaration import ItemPolicy, MeasureConfig, ParameterConfig, ReleaseDeclaration, RoleConfig
from a2a_engine.environment import EngineConfig
from a2a_engine.items import ItemBank


_BANK_PATH = Path(__file__).parents[1] / "tasks" / "tiny_varied_density_5a3p.jsonl"

DECLARATION = ReleaseDeclaration(
    id="calendar",
    release="v1",
    environment_id="calendar",
    version="v1",
    blurb="Assistants coordinate private calendars over a frozen set of solvable schedules.",
    source_url="https://github.com/devYaoYH/Project-Groundwork/tree/main/games/calendar",
    engine=EngineConfig(environment_id="calendar", defaults={"num_agents": 5}),
    parameters=[
        # The bank freezes each agent's pre-existing errands and solves the
        # oracle against them, so these three describe the item that was
        # generated, not a knob the loaded scenario would honour.
        ParameterConfig(name="density", type="continuous", source="item"),
        ParameterConfig(name="num_slots", type="integer", source="item"),
        ParameterConfig(name="num_meetings", type="integer", source="item"),
        ParameterConfig(
            name="communication_protocol",
            type="categorical",
            domain=["dm", "groupchat", "mixed"],
        ),
    ],
    roles=[RoleConfig(
        id="calendar-agent",
        count=5,
        accepts=["llm", "scripted", "human"],
        scripted_bindings={
            "baseline": "scripted",
            "dsm": "dsm",
            "paper_dsm": "paper_dsm",
            "private_dsm": "private_dsm",
            "imap": "imap",
            "sd": "sd",
        },
    )],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/calendar/tasks/tiny_varied_density_5a3p.jsonl",
        item_bank_sha256=ItemBank.load(_BANK_PATH).item_bank_sha256,
    ),
    measures=[
        MeasureConfig(name="coordination_rate", producer="environment", direction="maximize"),
        MeasureConfig(name="efficiency", producer="environment", direction="maximize"),
        MeasureConfig(
            name="per_agent_excess_burden",
            producer="environment",
            scope="participant",
            unit="participant",
            direction="minimize",
        ),
    ],
    oracle_version="cp-sat-v1",
)
