"""The vendored provider stack emits the same lifecycle as the shared stack."""

import asyncio
import json

from a2a_engine.event_sink import JsonlEventSink, read_event_sink
from a2a_engine.tracing import EventLog
from a2a_engine.turns import current_log, finish_turn, turn
from negotiation_game.backend.agents import api
from negotiation_game.backend.agents.llm import LLMAgentStdlib


class Stream:
    def __iter__(self):
        return iter([b'data: {"choices":[{"delta":{"content":"yes"},"finish_reason":"stop"}]}\n',
                     b'data: [DONE]\n'])

    def close(self):
        pass


def test_vendored_stack_records_wire_payload_and_chunk(tmp_path, monkeypatch):
    sent = []

    def urlopen(request, **kwargs):
        sent.append(json.loads(request.data))
        return Stream()

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    sink = JsonlEventSink(tmp_path / "events.jsonl", episode_uid="u1", environment_id="negotiation")
    log = EventLog(sink=sink)
    token = current_log.set(log)
    try:
        with turn("agent_a", "agent_a", {"round": 1}):
            result = api.call_llm_streaming("openai", "https://example.test", "secret", "gpt-4o-mini",
                                            [{"role": "user", "content": "trade?"}], 32, 0.2)
            finish_turn(result["text"])
        log.all()
    finally:
        current_log.reset(token)

    events = [entry["event"] for entry in read_event_sink(sink.path)]
    request = next(e["data"] for e in events if e["type"] == "llm.request")
    assert request["messages"] == {"messages": sent[0]["messages"]}
    assert request["params"]["stream_options"] == {"include_usage": True}
    assert [e["type"] for e in events if e["type"].startswith("llm.")] == [
        "llm.request", "llm.chunk", "llm.response", "llm.attempt",
    ]


def test_executor_preserves_turn_context_for_vendored_calls(tmp_path, monkeypatch):
    sink = JsonlEventSink(tmp_path / "events.jsonl", episode_uid="u1", environment_id="negotiation")
    log = EventLog(sink=sink)
    token = current_log.set(log)
    agent = LLMAgentStdlib(api_base="https://example.test", api_key="secret")
    agent._rate_limit = lambda: asyncio.sleep(0)

    def request(loop):
        from a2a_engine.turns import current_turn, request as record_request
        assert current_turn.get()["participant_id"] == "agent_a"
        record_request("gpt-4o-mini", "openai", {"messages": [{"role": "user", "content": "hello"}]})
        return {"text": "yes"}

    monkeypatch.setattr(agent, "_request_openai", request)
    try:
        with turn("agent_a", "agent_a", {}):
            assert asyncio.run(agent._call_api_with_retries()) == "yes"
        log.all()
    finally:
        current_log.reset(token)
    assert [e["event"]["type"] for e in read_event_sink(sink.path)] == [
        "turn.started", "llm.request", "llm.attempt",
    ]
