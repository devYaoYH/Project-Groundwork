"""Release declaration for the buyer-seller environment."""

from pathlib import Path

from a2a_engine.declaration import ItemPolicy, MeasureConfig, ParameterConfig, ReleaseDeclaration, RoleConfig
from a2a_engine.environment import EngineConfig
from a2a_engine.items import ItemBank


_BANK_PATH = Path(__file__).parent / "items" / "parameters_v1.jsonl"

DECLARATION = ReleaseDeclaration(
    id="buyer_seller",
    release="v1",
    environment_id="buyer_seller",
    version="v1",
    blurb="Sequential bargaining over multiple units with asymmetric private values.",
    source_url="https://github.com/devYaoYH/Project-Groundwork/tree/main/games/buyer-seller",
    engine=EngineConfig(environment_id="buyer_seller", defaults={"num_agents": 2}),
    parameters=[
        # These four values are the complete frozen tuple for the configured
        # bargaining oracle. A design selects rows on them rather than writing
        # them. In particular, discount_factor changes the discounted optimum
        # and therefore belongs to the item alongside its oracle result.
        ParameterConfig(name="seller_cost", type="continuous", source="item"),
        ParameterConfig(name="buyer_value", type="continuous", source="item"),
        ParameterConfig(name="num_items", type="integer", source="item"),
        ParameterConfig(name="discount_factor", type="continuous", source="item"),
        ParameterConfig(name="enforce_monotonic_offers", type="boolean", domain=[True, False]),
        # The release fixes its 10-round protocol horizon. Every bank row has
        # at most three units, so the frozen horizon admits the row's complete
        # discounted first-best.
        ParameterConfig(name="max_rounds", type="integer", domain=(1.0, 50.0), fixed=True),
    ],
    roles=[
        RoleConfig(id="seller", accepts=["llm", "scripted", "human"]),
        RoleConfig(id="buyer", accepts=["llm", "scripted", "human"]),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/buyer-seller/buyer_seller/items/parameters_v1.jsonl",
        item_bank_sha256=ItemBank.load(_BANK_PATH).item_bank_sha256,
    ),
    measures=[
        MeasureConfig(name="efficiency", producer="environment", direction="maximize"),
        MeasureConfig(name="joint_utility", producer="environment", direction="maximize"),
    ],
    oracle_version="closed-form-v3",
)
