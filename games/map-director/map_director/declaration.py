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


# Keep the V1 declaration and immutable bank available for older release files.
DECLARATION_V1 = DECLARATION
BANK_V2 = ItemBank.load(Path(__file__).parent / "items" / "maps_v2.jsonl")
DECLARATION_V2 = DECLARATION_V1.model_copy(deep=True)
DECLARATION_V2.release = DECLARATION_V2.version = "v2"
DECLARATION_V2.blurb = "Repeated map grounding with independent landmark jitter and diverse valid routes through ordered passages."
DECLARATION_V2.engine.defaults.update(task_version="v2", item_id="jitter_4_seed_17")
DECLARATION_V2.parameters += [
    ParameterConfig(name="jitter_radius", type="continuous", source="item"),
    ParameterConfig(name="noise_seed", type="integer", source="item"),
]
DECLARATION_V2.item_policy = ItemPolicy(mode="enumerate", bank_path="games/map-director/map_director/items/maps_v2.jsonl",
                                      item_bank_sha256=BANK_V2.item_bank_sha256)
DECLARATION_V2.oracle_version = "ordered-gates-corridor-v2"
DECLARATION_V2.measures += [MeasureConfig(name="gates_passed", producer="environment", grain="sequence",
                                       index_label="round", direction="maximize")]
DECLARATION_V2.metrics = list(DECLARATION_V2.measures)
DECLARATION_V2.description = DECLARATION_V2.blurb
DECLARATION_V2 = ReleaseDeclaration.model_validate(DECLARATION_V2.model_dump())
# New catalogue designs select V2; explicit old release files still select V1.
DECLARATION = DECLARATION_V2


def bank_for(version):
    if version == "v1":
        return BANK
    if version == "v2":
        return BANK_V2
    raise ValueError("Unsupported Map Director task_version")
