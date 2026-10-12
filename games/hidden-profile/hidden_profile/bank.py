"""Frozen evidence and oracle checks; never project oracle data into a seat."""

from pathlib import Path
from a2a_engine.items import ItemBank

BANK_PATH = Path(__file__).parent / "items" / "profiles_v1.jsonl"
CANDIDATES = ("A", "B", "C")


def score(facts):
    unique = {}
    for fact in facts:
        identity = fact["id"]
        if identity in unique and unique[identity] != fact:
            raise ValueError("conflicting evidence identity")
        unique[identity] = fact
    return {
        c: sum(f["points"] for f in unique.values() if f["candidate"] == c)
        for c in CANDIDATES
    }


def ranking(totals):
    return sorted(CANDIDATES, key=lambda c: (-totals[c], c))


def load_bank():
    bank = ItemBank.load(BANK_PATH)
    for item in bank.items:
        row = item.raw
        pooled = score(row["facts"])
        packets = [
            score([f for f in row["facts"] if f["id"] in ids]) for ids in row["packets"]
        ]
        expected = {
            "pooled_scores": pooled,
            "pooled_ranking": ranking(pooled),
            "packet_scores": packets,
            "packet_rankings": [ranking(s) for s in packets],
        }
        if row["oracle_result"] != expected:
            raise ValueError(f"frozen oracle mismatch for {item.item_id}")
    return bank


def project(row, seat):
    ids = row["packets"][seat]
    return [
        {k: f[k] for k in ("candidate", "points", "statement")}
        for f in row["facts"]
        if f["id"] in ids
    ]
