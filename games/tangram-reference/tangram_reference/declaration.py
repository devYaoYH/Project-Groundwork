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

BANK_PATH = Path(__file__).parent / "items" / "sessions_v1.jsonl"
DECLARATION = ReleaseDeclaration(
    id="tangram_reference",
    release="v1",
    environment_id="tangram_reference",
    blurb="Repeated private visual reference and convention formation with seven-piece tangrams.",
    engine=EngineConfig(
        environment_id="tangram_reference",
        defaults={"num_agents": 2, "num_rounds": 6, "max_exchanges": 6},
    ),
    required_modalities={"text", "image"},
    roles=[
        RoleConfig(
            id=r, accepts=["llm", "scripted"], required_modalities={"text", "image"}
        )
        for r in ("director", "matcher")
    ],
    parameters=[
        ParameterConfig(name="inventory_size", type="integer", source="item"),
        ParameterConfig(name="num_rounds", type="integer", domain=(2.0, 6.0)),
        ParameterConfig(name="max_exchanges", type="integer", domain=(1.0, 20.0)),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/tangram-reference/tangram_reference/items/sessions_v1.jsonl",
        item_bank_sha256=ItemBank.load(BANK_PATH).item_bank_sha256,
    ),
    oracle_version="exact-slot-order-v1",
    measures=[
        MeasureConfig(name="accuracy", producer="environment", direction="maximize"),
        MeasureConfig(
            name="round_success",
            producer="environment",
            grain="sequence",
            index_label="round",
            direction="maximize",
        ),
        MeasureConfig(name="messages", producer="environment", direction="minimize"),
    ],
)
