"""Seam tests: judgment storage and resume.

The judge moved from negotiation's Firestore-only storage to a shared,
backend-pluggable layer. The property that must not regress is resume
correctness: judging is expensive, and getting the key wrong either re-judges
everything or — far worse — silently mixes judgments from different prompt
versions into one aggregate.
"""

import json

import pytest

from a2a_judge.store import (
    LocalJudgmentStore,
    compress_transcript,
    decompress_transcript,
    judgment_doc_id,
    pending,
)


JUDGMENT = {"attribution": "agent_a conceded early", "score": 3}


# --- key scheme --------------------------------------------------------------


def test_doc_id_is_the_compound_key():
    assert judgment_doc_id("g1", "gpt-4o", "v2") == "g1__gpt-4o__v2"


def test_doc_id_sanitizes_slashes_in_model_ids():
    """Vertex/OpenRouter ids contain slashes, which would nest document paths."""
    assert judgment_doc_id("g1", "meta/llama-4", "v1") == "g1__meta_llama-4__v1"


def test_different_judge_models_and_versions_do_not_collide():
    ids = {
        judgment_doc_id("g1", "gpt-4o", "v1"),
        judgment_doc_id("g1", "gpt-4o", "v2"),
        judgment_doc_id("g1", "claude", "v1"),
    }
    assert len(ids) == 3


# --- local store -------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    assert store.save("g1", JUDGMENT, judge_model="gpt-4o", prompt_version="v1")

    records = store.load_all()
    assert len(records) == 1
    assert records[0]["game_id"] == "g1"
    assert records[0]["score"] == 3
    assert records[0]["judged_at"]


def test_rejudging_the_same_key_supersedes(tmp_path):
    """Append-only on disk, last-write-wins on read."""
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    store.save("g1", {"score": 1}, judge_model="m", prompt_version="v1")
    store.save("g1", {"score": 9}, judge_model="m", prompt_version="v1")

    records = store.load_all()
    assert len(records) == 1
    assert records[0]["score"] == 9


def test_different_prompt_versions_coexist(tmp_path):
    """Version history is the reason for the compound key."""
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    store.save("g1", {"score": 1}, judge_model="m", prompt_version="v1")
    store.save("g1", {"score": 2}, judge_model="m", prompt_version="v2")

    assert len(store.load_all()) == 2
    assert store.completed() == {"g1": {"v1", "v2"}}


def test_transcript_is_stored_compressed(tmp_path):
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    text = "agent_a: hello\nagent_b: hi\n" * 50
    store.save("g1", JUDGMENT, judge_model="m", prompt_version="v1", input_transcript=text)

    record = store.load_all()[0]
    assert "input_transcript_gz" in record
    assert decompress_transcript(record["input_transcript_gz"]) == text
    assert len(record["input_transcript_gz"]) < len(text)


def test_transcript_is_omitted_when_not_supplied(tmp_path):
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    store.save("g1", JUDGMENT, judge_model="m", prompt_version="v1")
    assert "input_transcript_gz" not in store.load_all()[0]


def test_empty_and_missing_stores_are_not_errors(tmp_path):
    assert LocalJudgmentStore(tmp_path / "absent.jsonl").load_all() == []
    assert LocalJudgmentStore(tmp_path / "absent.jsonl").completed() == {}


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    """A truncated line from an interrupted run must not lose the whole file."""
    path = tmp_path / "j.jsonl"
    store = LocalJudgmentStore(path)
    store.save("g1", JUDGMENT, judge_model="m", prompt_version="v1")
    with path.open("a") as f:
        f.write("{not json\n")
    store.save("g2", JUDGMENT, judge_model="m", prompt_version="v1")

    assert {r["game_id"] for r in store.load_all()} == {"g1", "g2"}


# --- resume ------------------------------------------------------------------


def test_pending_skips_games_already_judged_at_this_version(tmp_path):
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    store.save("g1", JUDGMENT, judge_model="m", prompt_version="v1")

    assert pending(["g1", "g2", "g3"], store, "v1") == ["g2", "g3"]


def test_bumping_the_prompt_version_re_judges_everything(tmp_path):
    """The distinction resume exists to make."""
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    for gid in ("g1", "g2"):
        store.save(gid, JUDGMENT, judge_model="m", prompt_version="v1")

    assert pending(["g1", "g2"], store, "v1") == []
    assert pending(["g1", "g2"], store, "v2") == ["g1", "g2"]


def test_pending_on_an_empty_store_returns_everything(tmp_path):
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    assert pending(["g1", "g2"], store, "v1") == ["g1", "g2"]


def test_pending_preserves_input_order(tmp_path):
    """Callers shard the pending list; a reordering would break shard stability."""
    store = LocalJudgmentStore(tmp_path / "j.jsonl")
    store.save("g2", JUDGMENT, judge_model="m", prompt_version="v1")
    assert pending(["g3", "g2", "g1"], store, "v1") == ["g3", "g1"]


# --- compression -------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "short", "unicode: café ☕", "x" * 10000])
def test_transcript_compression_round_trips(text):
    assert decompress_transcript(compress_transcript(text)) == text
