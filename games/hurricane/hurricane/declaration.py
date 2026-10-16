"""Pinned public release and scenario selection contract."""

from a2a_engine.declaration import (
    ItemPolicy,
    MeasureConfig,
    ParameterConfig,
    ReleaseDeclaration,
    RoleConfig,
)
from a2a_engine.environment import EngineConfig
from .bank import load_bank

DECLARATION = ReleaseDeclaration(
    id="hurricane",
    release="v1",
    environment_id="hurricane",
    version="v1",
    description="Coastal managers run instruments, pool ensemble readings and order evacuations before a hidden hurricane landfall.",
    blurb="Three coastal managers run scarce instruments, share ensemble forecasts and order evacuations before a hidden landfall.",
    source_url="https://github.com/devYaoYH/Project-Groundwork/tree/main/games/hurricane",
    engine=EngineConfig(environment_id="hurricane", defaults={"num_agents": 3}),
    roles=[RoleConfig(id="manager", count=3, accepts=["llm", "scripted", "human"])],
    parameters=[
        # One frozen storm per bank row: factoring on these selects a scenario.
        ParameterConfig(name="scenario_id", type="categorical", source="item"),
        ParameterConfig(name="landfall_zone", type="integer", source="item"),
        ParameterConfig(name="impact_radius", type="integer", source="item"),
        ParameterConfig(name="num_rounds", type="integer", domain=(1, 12)),
        ParameterConfig(name="evacuation_lead_time", type="integer", domain=(0, 12)),
        ParameterConfig(name="discussion_cycles", type="integer", domain=(1, 6)),
        ParameterConfig(name="ensemble_size", type="integer", domain=(1, 20)),
        ParameterConfig(name="recon_budget", type="integer", domain=(0, 20)),
        ParameterConfig(name="recon_sigma", type="continuous", domain=(0.0, 10.0)),
        ParameterConfig(name="model_sigma_initial", type="continuous", domain=(0.0, 10.0)),
        ParameterConfig(name="model_sigma_final", type="continuous", domain=(0.0, 10.0)),
        ParameterConfig(name="casualty_cost_per_capita", type="continuous", domain=(0.0, 1000.0)),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/hurricane/hurricane/items/storms_v1.jsonl",
        item_bank_sha256=load_bank().item_bank_sha256,
    ),
    measures=[
        MeasureConfig(name="total_cost", producer="environment", direction="minimize"),
        MeasureConfig(name="regret", producer="environment", direction="minimize"),
        MeasureConfig(name="population_exposed", producer="environment", direction="minimize"),
        MeasureConfig(name="disclosure_rate", producer="environment", direction="maximize"),
        MeasureConfig(name="estimate_error", producer="environment", direction="minimize"),
        MeasureConfig(name="recommendation_uptake", producer="environment", direction="neutral"),
    ],
    oracle_version="hit-zones-v1",
)
