"""Pinned public release and item selection contract."""

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
    id="hidden_profile",
    release="v1",
    environment_id="hidden_profile",
    description="Repeated four-participant evidence pooling with private packets, sealed reports and unanimous decisions.",
    engine=EngineConfig(environment_id="hidden_profile", defaults={"num_agents": 4}),
    roles=[RoleConfig(id="participant", count=4, accepts=["llm", "scripted"])],
    parameters=[
        ParameterConfig(name="sequence_id", type="categorical", source="item"),
        ParameterConfig(name="condition", type="categorical", source="item"),
        ParameterConfig(name="discussion_cycles", type="integer", domain=(1, 20)),
        ParameterConfig(name="commitment_cycles", type="integer", domain=(2, 20)),
        # What the same four seats carry from one decision round to the next.
        ParameterConfig(
            name="round_memory", type="categorical", domain=["none", "outcomes", "notes", "full"]
        ),
        ParameterConfig(name="max_note_chars", type="integer", domain=(50, 8000)),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/hidden-profile/hidden_profile/items/profiles_v1.jsonl",
        item_bank_sha256=load_bank().item_bank_sha256,
    ),
    measures=[
        MeasureConfig(
            name="optimal_choice_rate", producer="environment", direction="maximize"
        ),
        MeasureConfig(
            name="no_consensus_rounds", producer="environment", direction="minimize"
        ),
        MeasureConfig(
            name="post_score_absolute_error",
            producer="environment",
            direction="minimize",
        ),
    ],
    oracle_version="distinct-evidence-sum-v1",
)
