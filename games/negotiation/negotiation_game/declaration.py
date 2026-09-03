"""Release declaration for the negotiation environment."""

from pathlib import Path

from a2a_engine.declaration import ItemPolicy, MeasureConfig, ParameterConfig, ReleaseDeclaration, RoleConfig
from a2a_engine.environment import EngineConfig
from a2a_engine.items import ItemBank


_BANK_PATH = Path(__file__).parents[1] / "data" / "scenario_pools" / "run_009" / "filtered.json"

DECLARATION = ReleaseDeclaration(
    id="negotiation",
    release="v1",
    environment_id="negotiation",
    version="v1",
    blurb="Two agents negotiate a resource allocation against a stored oracle optimum.",
    source_url="https://github.com/devYaoYH/Project-Groundwork/tree/main/games/negotiation",
    engine=EngineConfig(environment_id="negotiation", defaults={"num_agents": 2}),
    parameters=[
        # Scenario pools are bucketed at generation time; the bank column is
        # named for the bucket it was filtered into.
        ParameterConfig(
            name="mc_ratio", type="continuous",
            source="item", item_key="mc_bucket",
        ),
        ParameterConfig(name="mode", type="categorical", domain=["stable", "rotating"]),
        ParameterConfig(name="num_rounds", type="integer", domain=(1.0, 20.0)),
        # Cheap talk is the environment's headline treatment, not scaffolding:
        # switching it off and varying its length are the two most obvious
        # designs anyone would write here.
        ParameterConfig(name="cheap_talk_turns", type="integer", domain=(0.0, 20.0)),
        ParameterConfig(name="enable_cheap_talk", type="boolean", domain=[True, False]),
    ],
    roles=[RoleConfig(id="negotiator", count=2, accepts=["llm", "scripted", "human"])],
    item_policy=ItemPolicy(
        mode="sample",
        bank_path="games/negotiation/data/scenario_pools/run_009/filtered.json",
        item_bank_sha256=ItemBank.load(_BANK_PATH).item_bank_sha256,
    ),
    measures=[
        MeasureConfig(name="num_rounds", producer="environment", direction="neutral"),
        MeasureConfig(name="joint_reward", producer="environment", direction="maximize"),
    ],
    oracle_version="scenario-pool-run-009",
)
