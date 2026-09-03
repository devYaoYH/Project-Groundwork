from __future__ import annotations

import hashlib

import pytest

from a2a_engine.declaration import ParameterConfig
from a2a_engine.items import ItemBank, derive_item_domain


def test_jsonl_item_bank_loads_with_a_stable_digest_and_paginates(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text(
        '{"item_id":"one","params":{"difficulty":1},"oracle_result":{"score":2}}\n'
        '{"item_id":"two","params":{"difficulty":2}}\n'
    )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bank = ItemBank.load(path, expected_sha256=digest)

    assert bank.item_bank_sha256 == digest
    assert bank.get("one").oracle_result == {"score": 2}
    page, cursor = bank.page(limit=1)
    assert [item.item_id for item in page] == ["one"]
    assert cursor == "1"


def test_item_bank_rejects_a_digest_mismatch(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"item_id":"one"}\n')

    with pytest.raises(ValueError, match="digest mismatch"):
        ItemBank.load(path, expected_sha256="0" * 64)


def test_attribute_levels_count_rows_per_distinct_value_in_bank_order(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text(
        '{"item_id":"a","params":{"density":0.8}}\n'
        '{"item_id":"b","params":{"density":0.5}}\n'
        '{"item_id":"c","params":{"density":0.8}}\n'
    )

    assert ItemBank.load(path).attribute_levels("density") == [(0.8, 2), (0.5, 1)]


def test_attribute_levels_reach_past_the_params_block_to_the_raw_row(tmp_path):
    # Negotiation's pool carries its bucket beside game_config, not inside it.
    path = tmp_path / "pool.json"
    path.write_text(
        '{"scenarios":['
        '{"candidate_id":"one","mc_bucket":0.5,"game_config":{"agent_budget":3}},'
        '{"candidate_id":"two","mc_bucket":1.0,"game_config":{"agent_budget":4}}'
        ']}'
    )

    assert ItemBank.load(path).attribute_levels("mc_bucket") == [(0.5, 1), (1.0, 1)]


def test_a_column_no_row_carries_is_reported_as_drift(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"item_id":"a","params":{"density":0.8}}\n')

    with pytest.raises(KeyError, match="drifted apart"):
        ItemBank.load(path).attribute_levels("secret_word")


def test_derive_item_domain_shapes_the_domain_to_the_declared_type(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text(
        '{"item_id":"a","params":{"density":0.8,"word":"dog"}}\n'
        '{"item_id":"b","params":{"density":0.5,"word":"apple"}}\n'
    )
    bank = ItemBank.load(path)

    numeric, numeric_levels = derive_item_domain(
        ParameterConfig(name="density", type="continuous", source="item"),
        bank,
    )
    categorical, categorical_levels = derive_item_domain(
        ParameterConfig(name="word", type="categorical", source="item"),
        bank,
    )

    assert numeric == (0.5, 0.8)
    assert numeric_levels == [(0.8, 1), (0.5, 1)]
    assert categorical == ["dog", "apple"]
    assert categorical_levels == [("dog", 1), ("apple", 1)]


def test_derive_item_domain_follows_item_key_when_the_names_differ(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"item_id":"a","mc_bucket":0.5,"params":{}}\n')

    domain, _ = derive_item_domain(
        ParameterConfig(
            name="mc_ratio", type="continuous",
            source="item", item_key="mc_bucket",
        ),
        ItemBank.load(path),
    )

    assert domain == (0.5, 0.5)
