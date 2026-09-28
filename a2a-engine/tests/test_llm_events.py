"""Lifecycle evidence is produced at the wire, not at a game's prompt template."""

import json
import urllib.error

from a2a_engine.artifacts import make_artifact_store
from a2a_engine.event_sink import DurableEventSink, current_event_sink, read_event_sink
from a2a_engine.llm import api
from a2a_engine.llm.retry import RetryPolicy, call_with_retry
from a2a_engine.stream_projection import project_events_to_trace
from a2a_engine.tracing import EventLog
from a2a_engine.turns import current_log, finish_turn, llm_call, turn


def _log(tmp_path):
    store = make_artifact_store({"backend": "local"}, root=tmp_path / "artifacts")
    sink = DurableEventSink(tmp_path / "local.events.jsonl", episode_uid="u1",
                            episode_id="exp.cell.000", environment_id="buyer_seller",
                            store=store, launch_id="launch")
    return EventLog(sink=sink), sink, store


class Stream:
    def __iter__(self):
        return iter([b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n',
                     b'data: {"usage":{"prompt_tokens":3,"completion_tokens":1},"choices":[]}\n',
                     b'data: [DONE]\n'])

    def close(self):
        pass


def test_streaming_wire_request_response_and_chunks_have_distinct_durability(tmp_path, monkeypatch):
    requests = []

    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        return Stream()

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    log, sink, store = _log(tmp_path)
    token = current_log.set(log)
    try:
        log.append("game_start", {"num_agents": 2})
        with turn("seller", "seller", {"round": 1}):
            result = api.call_llm_streaming("openai", "https://example.test", "secret", "gpt-4o-mini",
                                            [{"role": "user", "content": "offer?"}], 32, 0.2)
            finish_turn(result["text"])
        log.append("game_end", {"won": True})
        log.all()
    finally:
        current_log.reset(token)
    ref = next(iter(store.iter_event_sinks("launch")))
    durable = [e["event"] for e in read_event_sink(ref)]
    local = [e["event"] for e in read_event_sink(sink.path)]
    assert result["text"] == "hello"
    assert [e["type"] for e in durable] == ["game_start", "turn.started", "llm.request",
                                            "llm.response", "llm.attempt", "turn.finished", "game_end"]
    assert [e["data"]["delta"] for e in local if e["type"] == "llm.chunk"] == ["hello"]
    assert sink.durable_through == len(durable) == ref.watermark
    assert durable[2]["data"]["messages"] == {"messages": requests[0]["messages"]}
    assert durable[2]["data"]["params"]["stream_options"] == {"include_usage": True}
    assert durable[2]["data"]["params"]["max_tokens"] == 32
    assert durable[3]["data"]["usage"] == {"prompt_tokens": 3, "completion_tokens": 1,
                                               "total_tokens": None, "reasoning_tokens": None,
                                               "cached_prompt_tokens": None}
    assert project_events_to_trace(ref).stopped is False


def test_capture_content_false_preserves_envelope_but_not_prompt_or_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_CAPTURE_CONTENT", "false")
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **kw: Stream())
    log, sink, store = _log(tmp_path)
    token = current_log.set(log)
    try:
        with turn("buyer", "buyer", {"private": "value"}):
            api.call_llm_streaming("openai", "https://example.test", "secret", "gpt-4o-mini",
                                   [{"role": "user", "content": "private prompt"}], 12, 0.1)
        log.all()
    finally:
        current_log.reset(token)
    events = [e["event"] for e in read_event_sink(next(iter(store.iter_event_sinks("launch"))))]
    request = next(e["data"] for e in events if e["type"] == "llm.request")
    response = next(e["data"] for e in events if e["type"] == "llm.response")
    assert (request["model"], request["api_format"], request["params"]["stream"]) == ("gpt-4o-mini", "openai", True)
    assert "messages" not in request and "text" not in response
    assert response["usage"]["prompt_tokens"] == 3
    assert response["latency_ms"] is not None
    assert "private prompt" not in sink.path.read_text()
    assert "hello" not in sink.path.read_text()


def test_retry_attempts_keep_call_identity_and_classification(tmp_path, monkeypatch):
    log, sink, _ = _log(tmp_path)
    token = current_log.set(log)
    attempts = 0

    class RateLimited(Exception):
        status_code = 429

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimited("429")
        return "ok"

    monkeypatch.setattr("a2a_engine.llm.retry.time.sleep", lambda _: None)
    try:
        with turn("seller", "seller", {}):
            with llm_call():
                assert call_with_retry(flaky, RetryPolicy(max_attempts=2, jitter_ratio=0)) == "ok"
        log.all()
    finally:
        current_log.reset(token)
    records = [e["event"]["data"] for e in read_event_sink(sink.path)
               if e["event"]["type"] == "llm.attempt"]
    assert [(r["attempt"], r["status_code"], r["retryable"], r["outcome"])
            for r in records] == [(1, 429, True, "error"), (2, None, False, "success")]
    assert records[0]["call_index"] == records[1]["call_index"] == 0


def test_direct_streaming_retry_reuses_one_call_index(tmp_path, monkeypatch):
    log, sink, _ = _log(tmp_path)
    token = current_log.set(log)
    calls = 0

    def urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, None)
        return Stream()

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("a2a_engine.llm.retry.time.sleep", lambda _: None)
    try:
        with turn("seller", "seller", {}):
            result = api.call_llm_streaming_with_retry(
                "openai", "https://example.test", "secret", "gpt-4o-mini",
                [{"role": "user", "content": "offer?"}], 32, 0.2,
                policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
            )
        log.all()
    finally:
        current_log.reset(token)
    entries = [e["event"] for e in read_event_sink(sink.path)]
    requests = [e["data"] for e in entries if e["type"] == "llm.request"]
    attempts = [e["data"] for e in entries if e["type"] == "llm.attempt"]
    assert result["text"] == "hello" and calls == 2
    assert [r["call_index"] for r in requests] == [0, 0]
    assert [(a["attempt"], a["status_code"]) for a in attempts] == [(1, 429), (2, None)]


def test_framework_events_do_not_change_game_projection(tmp_path):
    plain, sink, _ = _log(tmp_path)
    start = plain.append("game_start", {"num_agents": 2})
    end = plain.append("game_end", {"won": True})
    plain.all()
    baseline = project_events_to_trace(sink.path).model_dump()

    other, other_sink, _ = _log(tmp_path / "other")
    other.append("game_start", {"num_agents": 2}, timestamp=start.timestamp)
    other.append("turn.started", {"turn_index": 0})
    other.append("game_end", {"won": True}, timestamp=end.timestamp)
    other.append("llm.response", {"text": "late"})
    other.all()
    projected = project_events_to_trace(other_sink.path).model_dump()
    for trace in (baseline, projected):
        trace.pop("events")
        trace["observability"].pop("event_count")
        trace["observability"].pop("event_sink")
    assert baseline == projected


def test_full_buyer_seller_episode_stays_below_durable_chunk_scale(tmp_path):
    from buyer_seller.game import BuyerSellerGame

    _, sink, store = _log(tmp_path)
    token = current_event_sink.set(sink)
    try:
        game = BuyerSellerGame({"num_items": 3, "max_rounds": 10, "seller_cost": 10,
                                "buyer_value": 30}, dry_run=True)
        trace = game.run()
    finally:
        current_event_sink.reset(token)
    entries = read_event_sink(next(iter(store.iter_event_sinks("launch"))))
    assert 20 <= len(entries) <= 150
    assert len(entries) == len(trace.events)
    assert all(e["event"]["type"] != "llm.chunk" for e in entries)
