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
        # The valuation triple travels with the closed-form oracle result, so
        # a design selects rows on it rather than writing it.  ``num_items``
        # varies 2/3 across the bank: a design that neither factors nor pins
        # it must say so, because otherwise it varies inside every cell.
        ParameterConfig(name="seller_cost", type="continuous", source="item"),
        ParameterConfig(name="buyer_value", type="continuous", source="item"),
        ParameterConfig(name="num_items", type="integer", source="item"),
        ParameterConfig(name="discount_factor", type="continuous", domain=(0.01, 1.0)),
        ParameterConfig(name="enforce_monotonic_offers", type="boolean", domain=[True, False]),
        ParameterConfig(name="max_rounds", type="integer", domain=(1.0, 50.0)),
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
    oracle_version="closed-form-v1",
)
