from pathlib import Path
from a2a_engine.declaration import (
    ReleaseDeclaration,
    RoleConfig,
    ParameterConfig,
    ItemPolicy,
    MeasureConfig,
)
from a2a_engine.environment import EngineConfig
from a2a_engine.items import ItemBank

BANK_PATH = Path(__file__).parent / "items" / "maps_v1.jsonl"
BANK = ItemBank.load(BANK_PATH)
DECLARATION = ReleaseDeclaration(
    id="map_director",
    release="v1",
    environment_id="map_director",
    version="v1",
    blurb="Repeated private-map route grounding with discrepant landmarks and persistent dyad context.",
    source_url="https://groups.inf.ed.ac.uk/maptask/maptask-description.html",
    required_modalities={"text", "image"},
    engine=EngineConfig(
        environment_id="map_director",
        defaults={"num_agents": 2, "num_rounds": 2, "max_turns": 6},
    ),
    roles=[
        RoleConfig(
            id=r, accepts=["llm", "scripted"], required_modalities={"text", "image"}
        )
        for r in ("giver", "follower")
    ],
    parameters=[
        ParameterConfig(name="discrepancy", type="categorical", source="item"),
        ParameterConfig(
            name="num_rounds", type="integer", domain=(2.0, 2.0), fixed=True
        ),
        ParameterConfig(name="max_turns", type="integer", domain=(1.0, 20.0)),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/map-director/map_director/items/maps_v1.jsonl",
        item_bank_sha256=BANK.item_bank_sha256,
    ),
    measures=[
        MeasureConfig(
            name="success",
            producer="environment",
            grain="sequence",
            index_label="round",
            direction="maximize",
        ),
        MeasureConfig(
            name="route_error",
            producer="environment",
            grain="sequence",
            index_label="round",
            direction="minimize",
        ),
        MeasureConfig(
            name="turns_used",
            producer="environment",
            grain="sequence",
            index_label="round",
            direction="minimize",
        ),
        MeasureConfig(
            name="success_rate", producer="environment", direction="maximize"
        ),
    ],
    oracle_version="ordered-frechet-v1",
)
