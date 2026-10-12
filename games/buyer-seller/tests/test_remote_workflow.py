import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.lobby import EpisodeSession, SeatsUnavailable
from a2a_engine.remote.server import ManagedEpisode, RuntimeManager
from buyer_seller.agents import BuyerAgent, SellerAgent
from buyer_seller.game import BuyerSellerGame
from buyer_seller.remote import BUYER_SELLER_TOOLS, stage_action
from buyer_seller.runtime_strategy import BuyerSellerStrategy
from .test_workflow import MockProvider, assert_trace_parity


def run_remote(config, *, provider_lines=None, mode=None, outcome=None):
    invocations, policies = [], []
    with ExitStack() as stack:
        manager = stack.enter_context(RuntimeManager(BUYER_SELLER_TOOLS))
        episode = manager.environment.registry.provision("bargain", [0, 1], environment_id="buyer_seller")
        managed = ManagedEpisode(manager, episode)
        manager.episodes.add(managed)
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        for seat in (0, 1):
            client = MockProvider(provider_lines[seat]) if provider_lines else None
            policy = BuyerSellerStrategy(client=client)
            policies.append(policy)
            original = policy.calls
            async def calls(invocation, original=original):
                invocations.append(invocation.model_copy(deep=True))
                if invocation.kind == "register" and mode == f"register_failed_{invocation.seat}":
                    raise RuntimeError("private provider details")
                if invocation.kind == "turn":
                    if mode in {"staged_failed", "staged_expiry"}:
                        from a2a_agent.mcp_client import execute
                        await execute(invocation, await original(invocation))
                        if mode == "staged_failed":
                            raise RuntimeError("private provider details")
                        await asyncio.sleep(1)
                    if mode == "failed":
                        raise RuntimeError("private provider details")
                    if mode == "expiry":
                        await asyncio.sleep(1)
                    if mode == "missing":
                        return []
                return await original(invocation)
            policy.calls = calls
            callback.run(runtime.join(manager.io.base_url + "/episodes/bargain/join", callback.base_url,
                                      episode.seats[seat].ticket, policy=policy))
        config = {**config, "environment_id": "buyer_seller", "agents": [{"runtime": "external"}] * 2}
        session = stack.enter_context(EpisodeSession(config, managed))
        game = BuyerSellerGame(config, dry_run=True, episode=session)
        if mode in {"expiry", "staged_expiry"}:
            game.seller.timeout_s = .15
        try:
            trace = game.run()
        finally:
            assert not managed.closed
            session.close()
            if outcome is not None:
                outcome.update(events=[e.type for e in game.events.all()],
                               records=game.seller.records, closed=managed.closed,
                               active=bool(manager.episodes), invocations=invocations)
        assert not manager.episodes
        return trace, invocations, game, policies


@pytest.mark.parametrize("config", [
    {}, {"seller_cost": 30, "buyer_value": 20, "max_rounds": 3},
    {"num_items": 50, "max_rounds": 4, "discount_factor": .5},
])
def test_scripted_v2_exact_parity_and_private_registration(config):
    expected = BuyerSellerGame(config, dry_run=True).run()
    actual, invocations, game, _ = run_remote(config)
    assert_trace_parity(actual, expected)
    for invocation in invocations:
        obs = invocation.observation
        if invocation.kind == "register":
            assert ("cost" in obs) == (invocation.seat == 0)
            assert ("value" in obs) == (invocation.seat == 1)
            assert "seller_cost" not in obs and "buyer_value" not in obs
        elif invocation.kind == "turn":
            assert ("your_cost" in obs) == (invocation.seat == 0)
            assert ("your_value" in obs) == (invocation.seat == 1)
    assert all(len(records) == 1 for agent in (game.seller, game.buyer) for records in agent.records)


def test_mock_provider_v2_exact_prompts_outputs_and_parser_defaults():
    config = {"num_items": 3, "max_rounds": 4, "enforce_monotonic_offers": True,
              "seller_cost": 7.51, "buyer_value": 33.27}
    lines = (["PRICE: $20.126", "PRICE: 40", "no number", ""],
             ["REJECT", "ACCEPT", "ACCEPT and REJECT", "ACCEPT"])
    local = BuyerSellerGame(config, dry_run=True)
    providers = [MockProvider(values) for values in lines]
    local.seller = SellerAgent(providers[0], cost=7.51, num_items=3, max_rounds=4, discount_factor=.9, monotonic=True)
    local.buyer = BuyerAgent(providers[1], value=33.27, num_items=3, max_rounds=4, discount_factor=.9)
    expected = local.run()
    actual, invocations, game, policies = run_remote(config, provider_lines=lines)
    assert_trace_parity(actual, expected)
    assert [p.client.messages for p in policies] == [p.messages for p in providers]
    assert all("33.27" not in str(messages) for messages in policies[0].client.messages)
    assert all("7.51" not in str(messages) for messages in policies[1].client.messages)
    offers = [record[0]["action"] for record in game.seller.records]
    responses = [record[0]["action"] for record in game.buyer.records]
    assert offers[1]["price"] == 40 and offers[2]["parse_failed"]
    assert responses[2]["accept"] is False and responses[2]["parse_failed"]
    buyer_turns = [i for i in invocations if i.kind == "turn" and i.seat == 1]
    assert buyer_turns[1].observation["current_offer"] == 20.13


@pytest.mark.parametrize("mode", ["failed", "missing", "expiry", "staged_failed", "staged_expiry"])
def test_failure_never_invents_offer_or_completes_episode(mode):
    outcome = {}
    with pytest.raises(RuntimeError, match="buyer_seller runtime action failed") as error:
        run_remote({"max_rounds": 1}, mode=mode, outcome=outcome)
    assert "private" not in str(error.value)
    assert outcome["events"] == ["game_start", "round_start"]
    assert outcome["closed"] and not outcome["active"]
    if mode.startswith("staged"):
        assert len(outcome["records"][0]) == 1


def test_remote_bare_numeric_parser_failure_remains_sanitized_and_unapplied():
    outcome = {}
    with pytest.raises(RuntimeError, match="buyer_seller runtime action failed"):
        run_remote({"max_rounds": 1}, provider_lines=(["ask $-1.23"], ["ACCEPT"]), outcome=outcome)
    assert outcome["events"] == ["game_start", "round_start"]
    assert outcome["closed"] and not outcome["active"]


@pytest.mark.parametrize("seat,phase,payload,other", [
    (0, "SELLER_ACT", {"price": -20.123, "text": "offer", "parse_failed": False}, {"accept": True}),
    (1, "BUYER_ACT", {"accept": True, "text": "accept", "parse_failed": True}, {"price": 999}),
])
def test_submission_dedup_bounds_phase_and_no_economic_clamping(seat, phase, payload, other):
    from a2a_engine.remote.capabilities import Capabilities
    from a2a_engine.remote.contract import CapabilityClaims, TurnInvocationV2
    from a2a_engine.remote.turns import TurnRecorder
    invocation = TurnInvocationV2(environment_id="buyer_seller", episode_id="e", attempt_id="a", seat=seat,
        turn_id="t", kind="turn", operation="act", phase=phase,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        mcp={key: f"http://127.0.0.1:1234/{key}" for key in ("env", "comm", "lifecycle")})
    claims = CapabilityClaims(episode_id="e", attempt_id="a", seat=seat, turn_id="t", phase=phase,
        exp=invocation.deadline.timestamp(), allowed_tools=[f"env.{BUYER_SELLER_TOOLS[seat].name}"])
    recorder = TurnRecorder(invocation, claims, Capabilities(), stage_action)
    tool = BUYER_SELLER_TOOLS[seat]
    assert recorder.call(BUYER_SELLER_TOOLS[1-seat], other, "wrong").status == "rejected"
    assert recorder.call(tool, {**payload, "text": "x" * 16385}, "large").code == "invalid_arguments"
    assert recorder.call(tool, payload, "id").status == "accepted"
    assert recorder.call(tool, payload, "id").status == "accepted"
    assert recorder.call(tool, {**payload, "text": "changed"}, "id").code == "call_id_conflict"
    assert recorder.call(tool, payload, "id2").code == "already_submitted"
    assert [r["action"] for r in recorder.close() if "action" in r] == [payload]
    assert recorder.call(tool, payload, "late").status == "rejected"


def test_real_provider_deadline_and_retry_prompt_parity(monkeypatch):
    import urllib.error
    from a2a_engine.llm import api
    from a2a_engine.llm.retry import RetryPolicy
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise urllib.error.HTTPError("http://provider.invalid", 503, "controlled", {}, None)
        return "PRICE: 12.345"
    monkeypatch.setattr(api, "call_llm_oneshot", request)
    strategy = BuyerSellerStrategy(client=api.LLMClient(model="mock", retry=RetryPolicy(max_attempts=2, backoff_base=0, jitter_ratio=0)))
    async def play():
        await strategy.calls(SimpleNamespace(kind="register", observation={"role": "seller", "cost": 10,
            "num_items": 2, "max_rounds": 5, "discount_factor": .9, "monotonic": False, "opening": 25}))
        observation = BuyerSellerGame({}, dry_run=True)._seller_observation(1, 0, [], None)
        result = await strategy.calls(SimpleNamespace(kind="turn", observation=observation,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=3)))
        assert result == [("env", "submit_offer", {"price": 12.345, "text": "PRICE: 12.345", "parse_failed": False})]
    asyncio.run(play())
    assert 0 < calls[1]["timeout"] <= calls[0]["timeout"] < 3
    assert calls[0]["messages"] == calls[1]["messages"]
    assert not strategy.model_lock.locked()


def test_cancelled_provider_cannot_overlap_next_model_call():
    import threading
    entered, release = threading.Event(), threading.Event()
    class BlockingProvider:
        def oneshot(self, messages):
            entered.set()
            assert release.wait(5)
            return "PRICE: 20"
    strategy = BuyerSellerStrategy(client=BlockingProvider())
    async def play():
        await strategy.calls(SimpleNamespace(kind="register", observation={"role": "seller", "cost": 10,
            "num_items": 2, "max_rounds": 5, "discount_factor": .9, "monotonic": False, "opening": 25}))
        invocation = SimpleNamespace(kind="turn", observation=BuyerSellerGame({}, dry_run=True)._seller_observation(1, 0, [], None))
        task = asyncio.create_task(strategy.calls(invocation))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(RuntimeError, match="still active"):
                await strategy.calls(invocation)
        finally:
            release.set()
    asyncio.run(play())
    assert not strategy.model_lock.locked()


@pytest.mark.parametrize("remote_seats", [(0,), (1,), (0, 1)])
def test_local_process_mixed_seats_runner_persistence(tmp_path, remote_seats):
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    from expt_runner.run_experiment import _make_run_context, _run_one
    config = {"environment_id": "buyer_seller", "num_items": 2, "max_rounds": 4,
              "agents": [{"type": "scripted", "runtime": "local_process" if seat in remote_seats else "in_process"}
                         for seat in range(2)]}
    expected = BuyerSellerGame({"num_items": 2, "max_rounds": 4}, dry_run=True).run()
    context = _make_run_context("bargain-process", "cell", config, 0, dry_run=True)
    context["verify_readback"] = True
    store = SQLiteEpisodeStore(path=tmp_path / "bargain.db")
    with RuntimeManager(BUYER_SELLER_TOOLS) as manager:
        context["runtime_manager"] = manager
        _run_one(context, store, tmp_path / "results")
        assert not manager.episodes
    assert_trace_parity(next(store.iter_episodes()), expected)


@pytest.mark.parametrize("seat", [0, 1])
def test_seat_flip_parity(seat_flip_trace, seat):
    config = {"environment_id": "buyer_seller", "seed": 41, "num_items": 2,
              "max_rounds": 4, "agents": [{"type": "scripted"}, {"type": "scripted"}]}
    traces = [seat_flip_trace(BuyerSellerGame, config, seat, runtime)
              for runtime in ("in_process", "local_process", "external")]
    assert_trace_parity(traces[0], traces[1])
    assert_trace_parity(traces[0], traces[2])


@pytest.mark.parametrize("seat", [0, 1])
def test_registration_failure_maps_to_seats_unavailable(seat):
    outcome = {}
    with pytest.raises(SeatsUnavailable, match="^seat_unavailable") as error:
        run_remote({}, mode=f"register_failed_{seat}", outcome=outcome)
    assert error.value.seats == (seat,)
    assert outcome["events"] == ["game_start"]
    assert outcome["closed"] and not outcome["active"]
    assert [i.seat for i in outcome["invocations"] if i.kind == "episode_end"] == list(range(seat))
