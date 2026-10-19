"""Frozen storm scenarios and their oracle check; never project the storm into a seat."""

from pathlib import Path
from a2a_engine.items import ItemBank

BANK_PATH = Path(__file__).parent / "items" / "storms_v1.jsonl"


def hit_zones(landfall_zone, impact_radius, num_zones):
    return [z for z in range(num_zones) if abs(z - landfall_zone) <= impact_radius]


def oracle(params):
    zones = hit_zones(
        params["landfall_zone"], params["impact_radius"], len(params["populations"])
    )
    return {
        "hit_zones": zones,
        "population_at_risk": sum(params["populations"][z] for z in zones),
    }


def load_bank():
    bank = ItemBank.load(BANK_PATH)
    for item in bank.items:
        if item.oracle_result != oracle(item.params):
            raise ValueError(f"frozen oracle mismatch for {item.item_id}")
    return bank
