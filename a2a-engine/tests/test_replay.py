"""Replay is a fail-closed substitution, not a cheaper live retry."""

import pytest

from a2a_engine.llm.replay import ReplayMismatch, ReplayingClient, current_replay
from a2a_engine.schemas import Event
from a2a_engine.tracing import EventLog
from a2a_engine.turns import current_log, finish_turn, turn


def _entry(event):
    return {"episode_id": "exp.cell.0", "event": event.model_dump(mode="json")}


def _source():
    log = EventLog()
    token = current_log.set(log)
    try:
        log.append("game_start", {"seed": 4})
        with turn("a", "a", {"step": 0}):
            from a2a_engine.turns import llm_call, request, response
            with llm_call():
                request("gpt-4o-mini", "openai", {"messages": [{"role": "user", "content": "first"}]})
                response("sample-one", model="gpt-4o-mini")
            finish_turn("sample-one")
        with turn("a", "a", {"step": 1}):
            pass
        return [_entry(event) for event in log.snapshot()]
    finally:
        current_log.reset(token)


def test_prior_turn_uses_recorded_sample_without_provider_call():
    source = _source()
    replay = ReplayingClient(source)
    log = EventLog()
    log_token = current_log.set(log)
    replay_token = current_replay.set(replay)
    calls = []
    try:
        log.append("game_start", {"seed": 4})
        with turn("a", "a", {"step": 0}):
            text = replay.call("oneshot", "gpt-4o-mini", [{"role": "user", "content": "first"}], lambda: calls.append("prior"))
            finish_turn(text)
        with turn("a", "a", {"step": 1}):
            text = replay.call("oneshot", "gpt-4o-mini", [], lambda: calls.append("current") or "new")
            finish_turn(text)
        assert text == "new"
        assert calls == ["current"]
        assert [e.model_dump() for e in log.snapshot()[:len(replay.prefix)]] == [e.model_dump() for e in [Event.model_validate(x["event"]) for x in source[:len(replay.prefix)]]]
    finally:
        current_replay.reset(replay_token)
        current_log.reset(log_token)


def test_missing_recorded_response_refuses_live_fallback():
    source = [entry for entry in _source() if entry["event"]["type"] != "llm.response"]
    replay = ReplayingClient(source)
    token = current_log.set(EventLog())
    replay_token = current_replay.set(replay)
    calls = []
    try:
        current_log.get().append("game_start", {"seed": 4})
        with turn("a", "a", {"step": 0}):
            with pytest.raises(ReplayMismatch, match="missing replayable response"):
                replay.call("oneshot", "gpt-4o-mini", [{"role": "user", "content": "first"}], lambda: calls.append("provider"))
        assert calls == []
    finally:
        current_replay.reset(replay_token)
        current_log.reset(token)


def test_redacted_recording_cannot_replay_prior_turn():
    source = _source()
    for entry in source:
        if entry["event"]["type"] == "llm.response":
            entry["event"]["data"].pop("result")
            entry["event"]["data"].pop("text")
    with pytest.raises(ReplayMismatch, match="missing replayable content"):
        ReplayingClient(source)


def test_changed_prior_prompt_fails_before_live_fallback():
    replay = ReplayingClient(_source())
    token = current_log.set(EventLog())
    replay_token = current_replay.set(replay)
    calls = []
    try:
        current_log.get().append("game_start", {"seed": 4})
        with turn("a", "a", {"step": 0}):
            with pytest.raises(ReplayMismatch, match="prompt changed"):
                replay.call("oneshot", "gpt-4o-mini", [{"role": "user", "content": "different"}],
                            lambda: calls.append("provider"))
        assert calls == []
    finally:
        current_replay.reset(replay_token)
        current_log.reset(token)
