"""Release declaration for the word-guess environment."""

from pathlib import Path

from a2a_engine.declaration import ItemPolicy, MeasureConfig, ParameterConfig, ReleaseDeclaration, RoleConfig
from a2a_engine.environment import EngineConfig
from a2a_engine.items import ItemBank


_BANK_PATH = Path(__file__).parent / "items" / "words_v1.jsonl"

DECLARATION = ReleaseDeclaration(
    id="word_guess",
    release="v1",
    environment_id="word_guess",
    version="v1",
    blurb="Two participants coordinate yes-or-no questions to identify a secret word.",
    source_url="https://github.com/devYaoYH/Project-Groundwork/tree/main/games/word-guess",
    engine=EngineConfig(environment_id="word_guess", defaults={"num_agents": 2}),
    parameters=[
        # One distinct word per bank row: factoring on it selects items rather
        # than setting a knob, and its levels are whatever the bank holds.
        ParameterConfig(name="secret_word", type="categorical", source="item"),
        ParameterConfig(name="max_turns", type="integer", domain=(1.0, 50.0)),
    ],
    roles=[
        RoleConfig(id="guesser", accepts=["llm", "scripted", "human"]),
        RoleConfig(id="host", accepts=["llm", "scripted", "human"]),
    ],
    item_policy=ItemPolicy(
        mode="enumerate",
        bank_path="games/word-guess/word_guess/items/words_v1.jsonl",
        item_bank_sha256=ItemBank.load(_BANK_PATH).item_bank_sha256,
    ),
    measures=[
        MeasureConfig(name="won", producer="environment", direction="maximize"),
        MeasureConfig(name="turns_used", producer="environment", direction="minimize"),
        MeasureConfig(name="efficiency", producer="environment", direction="minimize"),
    ],
    oracle_version="answer-key-v1",
)
