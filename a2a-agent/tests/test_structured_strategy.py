import asyncio
import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from a2a_agent.strategies import StructuredOutputStrategy
from a2a_engine.remote.contract import TurnInvocation
from a2a_engine.remote.contract import TokenUsage
from a2a_engine.remote.seats import LocalProcessLauncher, validate_runtime
from pydantic import ValidationError


def invocation(kind="turn", **overrides):
    return TurnInvocation(episode_id="episode", seat=0, turn_id="turn", kind=kind,
                          deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                          prompt="environment prompt verbatim: inbox from seat 1", **overrides)


class MockModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def streaming_with_retry(self, messages, **kwargs):
        self.requests.append((copy.deepcopy(messages), kwargs))
        return next(self.responses)


def register(strategy):
    asyncio.run(strategy.calls(invocation("register", observation={
        "action_to_tool": {"dm": "comm.send", "participant_groupchat": "comm.send", "all_groupchat": "comm.send",
                           "schedule": "env.schedule", "reschedule": "env.reschedule"},
        "action_schema": {"schedule": {"type": "object"}},
    })))


@pytest.mark.parametrize("kind", ["dm", "participant_groupchat", "all_groupchat", "schedule", "reschedule"])
def test_register_mapping_translates_every_action(kind):
    action = {"type": kind, "content": "hello", "to": 1} if "chat" in kind or kind == "dm" else (
        {"type": kind, "meeting_id": 0, "slot": 1} if kind == "schedule" else
        {"type": kind, "item_id": 4, "from_slot": 1, "to_slot": 2, "justification": "coordination"})
    model = MockModel([{"text": json.dumps({"actions": [action], "thinking": "internal thinking"}),
                        "prompt_tokens": 10, "completion_tokens": 5, "duration_s": .125, "_raw_response": {"private": "raw"}}])
    strategy = StructuredOutputStrategy(model)
    register(strategy)
    calls = asyncio.run(strategy.calls(invocation(phase="DECISION", capability="capability")))
    endpoint, name, arguments = calls[0]
    assert (endpoint, name) == (("comm", "send") if kind in {"dm", "participant_groupchat", "all_groupchat"} else ("env", kind))
    assert arguments == ({"channel": kind, "content": "hello", "to": 1} if endpoint == "comm" else {k: v for k, v in action.items() if k != "type"})
    assert model.requests[0][0] == [{"role": "system", "content": invocation().prompt}, {"role": "user", "content": invocation().prompt}]
    assert strategy.thinking == "internal thinking"
    assert strategy.telemetry.usage.total_tokens == 15 and strategy.telemetry.latency_ms == 125
    assert "private" not in strategy.telemetry.model_dump_json() and "internal thinking" not in strategy.telemetry.model_dump_json()
    assert len(strategy.history) == 2


def test_full_ordered_cell_and_unknown_actions_do_not_assert_authority():
    strategy = StructuredOutputStrategy(MockModel([]))
    register(strategy)
    strategy.mapping["evil"] = "env.end_turn"
    calls = strategy.extract_calls({"text": json.dumps({"actions": [
        {"type": "schedule", "meeting_id": 0, "slot": 0},
        {"type": "reschedule", "item_id": 1, "from_slot": 0, "to_slot": 1, "justification": "move"},
        {"type": "evil", "seat": 1}, {"type": "unknown"},
        {"type": "dm", "to": 1, "content": "hello", "seat": 7, "episode_id": "forged"},
    ]})})
    assert [name for _, name, _ in calls] == ["schedule", "reschedule", "send"]
    assert calls[-1][2] == {"channel": "dm", "to": 1, "content": "hello"}


def test_reflection_is_nonhistory_and_retry_keeps_verbatim_parent_prompt():
    strategy = StructuredOutputStrategy(MockModel([{"text": '{"actions":[]}'}, {"text": '{"deltas":[-3,0,3]}'}, {"text": '[]'}]))
    register(strategy)
    asyncio.run(strategy.calls(invocation(phase="VOLUNTARY")))
    before = copy.deepcopy(strategy.history)
    reflection = asyncio.run(strategy.reflect(invocation("reflect", observation={"num_slots": 3})))
    assert strategy.history == before
    assert [e["probability_busy"] for e in reflection["estimates"]] == [0, .5, 1]
    assert strategy.model.requests[1][1]["temperature"] == 0
    retry = invocation(phase="DECISION_RETRY", parent_phase="VOLUNTARY", observation={"conflict": "blocked"})
    asyncio.run(strategy.calls(retry))
    assert strategy.model.requests[-1][0] == [{"role": "system", "content": strategy.system_prompt}, *before, {"role": "user", "content": retry.prompt}]


def test_model_error_does_not_poison_history():
    strategy = StructuredOutputStrategy(MockModel([None]))
    register(strategy)
    with pytest.raises(RuntimeError, match="model execution failed"):
        asyncio.run(strategy.calls(invocation()))
    assert not strategy.history


@pytest.mark.parametrize("value", [-1, 1000000001, True, 1.5, "2", float("inf")])
def test_usage_wire_fields_reject_unbounded_or_noninteger_telemetry(value):
    with pytest.raises(ValidationError):
        TokenUsage(prompt_tokens=value)


def test_model_child_inherits_only_required_provider_configuration(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "own-provider")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sibling-provider")
    monkeypatch.setenv("A2A_SIGNING_KEY", "runner-key")
    monkeypatch.setenv("A2A_MODEL_CONFIG", "sibling-config")
    monkeypatch.setattr('a2a_engine.remote.seats.subprocess.Popen', lambda command, **kw: captured.update(command=command, **kw))
    LocalProcessLauncher("http://127.0.0.1:1/join", "own-ticket", {"type": "llm", "model": "gpt-test", "temperature": 0.2})
    assert captured["command"][-1] == "structured_output"
    assert captured["env"]["OPENAI_API_KEY"] == "own-provider"
    assert not set(captured["env"]) & {"ANTHROPIC_API_KEY", "A2A_SIGNING_KEY"}
    assert json.loads(captured["env"]["A2A_MODEL_CONFIG"]) == {"model": "gpt-test", "temperature": .2}


def test_native_tools_remains_explicitly_unsupported():
    with pytest.raises(ValueError, match="unsupported harness"):
        validate_runtime({"runtime": "local_process", "harness": "native_tools"})


def test_provider_timeout_is_clipped_and_expired_invocation_never_calls_model():
    strategy = StructuredOutputStrategy(MockModel([{"text": "[]"}]))
    register(strategy)
    asyncio.run(strategy.calls(invocation()))
    options = strategy.model.requests[0][1]
    assert 0 < options["timeout"] <= 30 and options["max_retries"] == 1
    expired = invocation()
    expired.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(TimeoutError):
        asyncio.run(strategy.calls(expired))
    assert len(strategy.model.requests) == 1 and len(strategy.history) == 2


def test_cancelled_provider_call_cannot_overlap_next_model_invocation():
    started, finish = threading.Event(), threading.Event()

    class SlowModel:
        requests = 0

        def streaming_with_retry(self, messages, **options):
            self.requests += 1
            started.set()
            assert finish.wait(2)
            return {"text": "[]"}

    strategy = StructuredOutputStrategy(SlowModel())
    register(strategy)

    async def run():
        task = asyncio.create_task(strategy.calls(invocation()))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            next_turn = invocation()
            next_turn.deadline = datetime.now(timezone.utc) + timedelta(seconds=.05)
            with pytest.raises(TimeoutError):
                await strategy.calls(next_turn)
            assert strategy.model.requests == 1 and not strategy.history
        finally:
            finish.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("as_exception", [False, True])
def test_reflection_logprob_fallback_preserves_measurements_without_history(as_exception):
    raw = {"content": [{"token": "1", "logprob": -.2,
                        "top_logprobs": [{"token": "0", "logprob": -2}, {"token": "x", "logprob": -5}]}]}

    class CalibrationModel(MockModel):
        api_format = "openai"

        def streaming_with_retry(self, messages, **options):
            if as_exception and options.get("logprobs"):
                self.requests.append((copy.deepcopy(messages), options))
                raise ValueError("logprobs not supported")
            return super().streaming_with_retry(messages, **options)

    error = [] if as_exception else [{"_error": "logprobs not supported"}]
    strategy = StructuredOutputStrategy(CalibrationModel([*error, {"text": '{"deltas":[3]}', "_raw_response": raw}]))
    register(strategy)
    result = asyncio.run(strategy.reflect(invocation("reflect", observation={"num_slots": 1})))
    assert result["estimates"][0]["logprobs"] == {"0": -2, "1": -.2, "_top_logprob_floor": -5}
    first, fallback = strategy.model.requests
    assert first[0] == fallback[0] and first[1]["logprobs"] is True
    assert "logprobs" not in fallback[1] and "top_logprobs" not in fallback[1]
    assert fallback[1]["timeout"] <= first[1]["timeout"]
    assert not strategy.history
