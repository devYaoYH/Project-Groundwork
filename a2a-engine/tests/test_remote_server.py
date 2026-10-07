import asyncio
from datetime import datetime, timezone

import pytest

from a2a_agent.mcp_client import connect
from a2a_engine.remote.contract import CapabilityClaims
from calendar_game.agents import DecideResult


async def call(harness, endpoint, name, args=None, *, token=None, call_id="call", url=None):
    invocation = harness.client.pending
    async with connect(url or invocation.mcp[endpoint], token or invocation.capability) as session:
        result = await session.call_tool(name, args or {}, meta={"a2a/call_id": call_id} if call_id else None)
        assert result.isError == (result.structuredContent["status"] == "rejected")
        return result.structuredContent


def test_fixed_tools_and_read_only_snapshots(open_remote_turn):
    harness = open_remote_turn

    async def check():
        for endpoint, expected in (("env", {"get_observation", "schedule", "reschedule"}),
                                   ("comm", {"list_peers", "send", "read_inbox"})):
            for token in (harness.client.pending.capability, "no-authority"):
                async with connect(harness.client.pending.mcp[endpoint], token) as session:
                    result = await session.list_tools()
                    assert {tool.name for tool in result.tools} == expected
        assert (await call(harness, "env", "get_observation"))["data"] == harness.client.pending.observation
        assert (await call(harness, "comm", "read_inbox", call_id="inbox"))["data"] == []
        assert (await call(harness, "comm", "list_peers", call_id="peers"))["data"]["seats"] == [1]

    asyncio.run(check())
    assert harness.episode.seats[0].recorder.records == []


def test_schedule_stages_not_commits_and_deduplicates(open_remote_turn):
    harness = open_remote_turn
    args = {"meeting_id": 0, "slot": 0}
    first = asyncio.run(call(harness, "env", "schedule", args))
    assert first["status"] == "accepted" and first["resolves"] == "end_of_phase"
    assert asyncio.run(call(harness, "env", "schedule", args)) == first
    assert len(harness.episode.seats[0].recorder.records) == 1
    conflict = asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 1}))
    assert conflict["code"] == "call_id_conflict"
    result = harness.client._act(DecideResult)
    # The policy's independent schedule is another staged call, never an early commit.
    assert result.tool_calls == [{"type": "schedule", "meeting_id": 0, "slot": 0}] * 2
    assert harness.episode.seats[0].recorder.closed


@pytest.mark.parametrize("kind", ["tampered", "expired", "closed", "wrong_episode", "wrong_seat", "wrong_turn", "stale_attempt", "unregistered"])
def test_authentication_failures_never_stage(open_remote_turn, kind):
    harness = open_remote_turn
    recorder = harness.episode.seats[0].recorder
    invocation = harness.client.pending
    token = invocation.capability
    url = None
    capabilities = harness.environment.registry.capabilities
    if kind == "tampered":
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif kind == "closed":
        recorder.close()
    elif kind == "wrong_episode":
        harness.environment.registry.provision("other", (0,))
        url = f"{harness.io.base_url}/episodes/other/env/mcp"
    elif kind == "unregistered":
        harness.episode.seats[0].registered = False
    else:
        changes = {"expired": {"exp": datetime.now(timezone.utc).timestamp() - 1},
                   "wrong_seat": {"seat": 1}, "wrong_turn": {"turn_id": "wrong"},
                   "stale_attempt": {"attempt_id": "other-attempt"}}[kind]
        if kind in {"expired", "wrong_turn"}:
            capabilities.revoke(recorder.claims)
        token = capabilities.mint(CapabilityClaims(**{**recorder.claims.model_dump(), **changes}))
    result = asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 0}, token=token, url=url))
    assert result["code"] == "unauthorized"
    assert recorder.records == []


@pytest.mark.parametrize("tool,args,call_id,code", [
    ("reschedule", {"item_id": 1, "from_slot": 0, "to_slot": 1, "justification": "move"}, "r", "unsupported"),
    ("schedule", {"meeting_id": 0, "slot": 0, "seat": 1}, "r", "invalid_arguments"),
    ("schedule", {"meeting_id": 0, "slot": "0"}, "r", "invalid_arguments"),
    ("schedule", {"meeting_id": 0, "slot": 0}, None, "missing_call_id"),
])
def test_authenticated_rejections(open_remote_turn, tool, args, call_id, code):
    harness = open_remote_turn
    result = asyncio.run(call(harness, "env", tool, args, call_id=call_id))
    assert result["code"] == code
    assert harness.episode.seats[0].recorder.records == [{"rejection": {"code": code, "reason": result["reason"]}}]


def test_tool_grants_and_cross_endpoint(open_remote_turn):
    harness = open_remote_turn
    assert asyncio.run(call(harness, "env", "send", {"channel": "dm", "to": 1, "content": "no"}))["code"] == "unauthorized"
    assert asyncio.run(call(harness, "env", "end_turn"))["code"] == "unauthorized"
    assert harness.episode.seats[0].recorder.records == []


def test_attempt_replacement_cannot_reuse_old_capability(open_remote_turn):
    harness = open_remote_turn
    old = harness.client.pending.capability
    harness.environment.registry.unregister(harness.episode)
    replacement = harness.environment.registry.provision("episode", (0,))
    result = asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 0}, token=old))
    assert result["code"] == "unauthorized"
    assert replacement.seats[0].recorder is None
    harness.environment.registry.unregister(replacement)


def test_router_budget_reservation_and_phase_scope(open_remote_turn):
    from calendar_game.game import CalendarGame

    harness = open_remote_turn
    client = harness.client
    client.seat.recorder.close()
    client.router = CalendarGame({"num_agents": 2, "dm_cap": 1}).router
    client.prepare({"phase": "CHEAP_TALK", "round": 0, "turn": 0, "prompt_sent": "talk",
                    "calendar_render": "Slot 0: [FREE]", "inbox_drained": []}, 0)
    args = {"channel": "dm", "to": 1, "content": "hello"}
    first = asyncio.run(call(harness, "comm", "send", args))
    assert first["status"] == "accepted"
    assert asyncio.run(call(harness, "comm", "send", args)) == first
    assert asyncio.run(call(harness, "comm", "send", args, call_id="second"))["code"] == "routing_denied"
    assert client.seat.recorder.reserved == 1
    assert asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 0}, call_id="s"))["code"] == "wrong_phase"
    assert len([record for record in client.seat.recorder.records if "action" in record]) == 2


def test_atomic_duplicate_calls(open_remote_turn):
    harness = open_remote_turn

    async def concurrent():
        async with connect(harness.client.pending.mcp["env"], harness.client.pending.capability) as session:
            return await asyncio.gather(*[session.call_tool("schedule", {"meeting_id": 0, "slot": 0},
                                                          meta={"a2a/call_id": "same"}) for _ in range(8)])

    result = asyncio.run(concurrent())
    assert all(item.structuredContent["status"] == "accepted" for item in result)
    assert len(harness.client.seat.recorder.records) == 1


def test_call_caps_include_invalid_and_read_only_calls(open_remote_turn):
    harness = open_remote_turn
    recorder = harness.client.seat.recorder
    recorder.max_calls = 3
    for _ in range(3):
        assert asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 0}, call_id=None))["code"] == "missing_call_id"
    assert asyncio.run(call(harness, "env", "get_observation", call_id="read"))["code"] == "call_cap"
    assert asyncio.run(call(harness, "env", "schedule", {"meeting_id": 0, "slot": 0}, call_id="write"))["code"] == "call_cap"
    assert len(recorder.records) == 3 and not recorder.cache


def test_closed_capability_rejected_in_existing_sdk_connection(open_remote_turn):
    harness = open_remote_turn

    async def check():
        async with connect(harness.client.pending.mcp["env"], harness.client.pending.capability) as session:
            await session.list_tools()
            harness.client.seat.recorder.close()
            result = await session.call_tool("schedule", {"meeting_id": 0, "slot": 0}, meta={"a2a/call_id": "late"})
            assert result.isError and result.structuredContent["code"] == "unauthorized"

    asyncio.run(check())
    assert harness.client.seat.recorder.records == []


def test_two_live_episodes_share_app_but_not_seat_authority(open_remote_turn):
    from types import SimpleNamespace

    from a2a_agent.server import ScriptedRuntime
    from a2a_engine.remote.dispatch import LoopbackServer
    from calendar_game.agents import GameConfig
    from calendar_game.remote import CalendarRuntimeContext, RemoteSeatClient

    first = open_remote_turn
    episode = first.environment.registry.provision("sibling", (0,))
    runtime = ScriptedRuntime(episode.seats[0].ticket)
    try:
        with LoopbackServer(runtime.app) as callback:
            callback.run(runtime.join(f"{first.io.base_url}/episodes/sibling/join", callback.base_url))
            context = CalendarRuntimeContext(first.environment, first.io, episode)
            client = RemoteSeatClient(context, 0, first.client.router)
            client.register(0, GameConfig(2, 2, 0, [0, 1], decision_retries=0))
            client.start_round({"id": 99, "participants": [0, 1], "duration": 1, "cost": 1}, "Slot 0: [FREE]", 0)
            client.prepare({"phase": "DECISION", "round": 0, "turn": 0, "prompt_sent": "sibling",
                            "calendar_snapshot_render": "Slot 0: [FREE]"}, 0)
            second = SimpleNamespace(client=client)
            assert asyncio.run(call(first, "env", "get_observation"))["data"]["meeting"]["id"] == 0
            assert asyncio.run(call(second, "env", "get_observation"))["data"]["meeting"]["id"] == 99
            wrong = asyncio.run(call(first, "env", "schedule", {"meeting_id": 99, "slot": 0},
                                     url=client.pending.mcp["env"], call_id="cross"))
            assert wrong["code"] == "unauthorized"
            assert asyncio.run(call(first, "env", "schedule", {"meeting_id": 0, "slot": 0}, call_id="a"))["status"] == "accepted"
            assert asyncio.run(call(second, "env", "schedule", {"meeting_id": 99, "slot": 0}, call_id="a"))["status"] == "accepted"
            assert first.client.seat.recorder.records == [{"action": {"type": "schedule", "meeting_id": 0, "slot": 0}}]
            assert client.seat.recorder.records == [{"action": {"type": "schedule", "meeting_id": 99, "slot": 0}}]
    finally:
        first.environment.registry.unregister(episode)
