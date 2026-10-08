import copy
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from a2a_engine.remote.dispatch import LoopbackServer
from calendar_game.game import CalendarGame


# Explicit event-local differences only. Unknown fields survive normalization.
RUNTIME_FIELDS = {
    "agent_registered": {"runtime", "protocol_version", "agent_info", "harness"},
    "turn_start": {"turn_id", "deadline"}, "decide_start": {"turn_id", "deadline"},
    "turn_end": {"turn_id", "deadline", "closed_by", "attempts", "telemetry_source", "text", "thinking", "usage", "latency_ms", "raw_api_response"},
    "decide_end": {"turn_id", "deadline", "closed_by", "attempts", "telemetry_source", "text", "thinking", "usage", "latency_ms", "raw_api_response"},
}


def normalized_events(trace):
    return [(event.type, {key: value for key, value in event.data.items() if key not in RUNTIME_FIELDS.get(event.type, set())})
            for event in trace.events]


def config(**updates):
    return {"seed": 42, "num_agents": 2, "num_slots": 3, "num_meetings": 1, "density": 0,
            "decision_retries": 0, "enable_reflection": False, "enable_fallback": False,
            "max_turns_per_round": 2, "join_timeout_s": 15,
            "agents": [{"type": "scripted"}, {"type": "scripted"}], **updates}


def scenario():
    return {"seed": 42, "calendars": [[None] * 3, [None] * 3],
            "meetings": [{"id": 0, "participants": [0, 1], "speaker_order": [0, 1], "duration": 1, "cost": 1}]}


@pytest.mark.parametrize("graph", ["complete", "ring", "star", "edges"])
@pytest.mark.parametrize("remote_seats", [(0,), (1,), (0, 1)])
def test_packaged_children_match_full_scripted_episode(graph, remote_seats):
    default = {"graph": graph, "channels": {"dm": {"enabled": True}}, "budget": {"per_agent_per_round": 1}}
    if graph == "star":
        default["hub"] = 0
    elif graph == "edges":
        default.update(edges=[[0, 1]], directed=True)
    cfg = config(communication={"topology": {"default": default}})
    local = CalendarGame(cfg).run_with_scenario(copy.deepcopy(scenario()))
    mixed = copy.deepcopy(cfg)
    for seat in remote_seats:
        mixed["agents"][seat]["runtime"] = "local_process"
    game = CalendarGame(mixed)
    remote = game.run_with_scenario(copy.deepcopy(scenario()))
    assert normalized_events(remote) == normalized_events(local)
    assert remote.final_state == local.final_state
    assert remote.metrics == local.metrics
    assert any(event.type == "topology_changed" for event in remote.events)
    assert all(child.process.poll() is not None for child in game.runtime_context.children)
    assert not game.runtime_manager.io.thread.is_alive()


def test_normalization_never_hides_game_fields_or_unknown_data():
    from a2a_engine.schemas import Event, EpisodeTrace
    data = {"prompt_sent": "prompt", "inbox_drained": [{"content": "message"}], "reason": "rejected",
            "tool_calls": [{"type": "schedule", "slot": 0}], "unfamiliar_field": 123, "turn_id": "remote"}
    trace = EpisodeTrace(episode_uid="test", config={"environment_id": "calendar", "num_agents": 2}, events=[Event(type="turn_start", data=data)])
    normalized = normalized_events(trace)[0][1]
    assert normalized == {key: value for key, value in data.items() if key != "turn_id"}
    for key in normalized:
        changed = copy.deepcopy(trace)
        changed.events[0].data[key] = "changed"
        assert normalized_events(changed) != normalized_events(trace)


def test_local_process_model_uses_only_loopback_mock_provider(monkeypatch):
    requests = []

    async def complete(request):
        payload = await request.json()
        requests.append(payload)
        last = payload["messages"][-1]["content"]
        actions = [{"type": "schedule", "meeting_id": 0, "slot": 0}] if "DECISION" in last else []
        text = json.dumps({"thinking": "agent-only", "actions": actions})
        chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return Response("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n", media_type="text/event-stream")

    monkeypatch.setenv("OPENAI_API_KEY", "mock-only-key")
    with LoopbackServer(Starlette(routes=[Route("/chat/completions", complete, methods=["POST"])])) as provider:
        cfg = config(agents=[{"type": "llm", "runtime": "local_process", "harness": "structured_output",
                              "model": "gpt-test", "api_base": provider.base_url, "max_tokens": 128}, {"type": "scripted"}])
        game = CalendarGame(cfg)
        trace = game.run_with_scenario(scenario())
    assert not trace.stopped and trace.metrics["meetings_scheduled"] == 1
    starts = [event.data["prompt_sent"] for event in trace.events if event.type in {"turn_start", "decide_start"} and event.data["agent_id"] == 0]
    assert [request["messages"][-1]["content"] for request in requests] == starts
    assert requests[0]["messages"][0]["role"] == "system"
    assert len(requests[-1]["messages"]) == 1 + 2 * len(requests) - 1
    ends = [event for event in trace.events if event.type in {"turn_end", "decide_end"} and event.data["agent_id"] == 0]
    assert all(event.data["usage"]["total_tokens"] == 15 for event in ends)
    assert all(event.data["raw_api_response"] is None for event in ends)
    assert "agent-only" not in trace.model_dump_json() and "mock-only-key" not in trace.model_dump_json()
    assert all(child.process.poll() is not None for child in game.runtime_context.children)
