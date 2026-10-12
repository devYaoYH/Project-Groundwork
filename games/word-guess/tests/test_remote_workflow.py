import asyncio
from contextlib import ExitStack
from copy import deepcopy

import pytest

from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from calendar_game.remote import CalendarRuntimeContext
from word_guess.game import WordGuessGame
from word_guess.remote import WORD_GUESS_TOOLS
from word_guess.runtime_strategy import WordGuessStrategy
from .test_workflow import CASES, MockProvider, controlled


def run_remote(guesses, replies, turns, *, missing=False, failed=False, expiry=False):
    invocations = []
    with ExitStack() as stack:
        environment = EnvironmentApp(WORD_GUESS_TOOLS)
        io = stack.enter_context(LoopbackServer(environment.app))
        episode = environment.registry.provision("words", [0, 1], environment_id="word_guess")
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        for seat, lines in enumerate((guesses, replies)):
            policy = WordGuessStrategy(client=MockProvider(lines * max(1, turns)))
            original = policy.calls
            async def calls(invocation, original=original):
                invocations.append(invocation.model_copy(deep=True))
                if invocation.kind == "turn":
                    if failed:
                        raise RuntimeError("private provider details")
                    if expiry:
                        await asyncio.sleep(1)
                    if missing:
                        return []
                return await original(invocation)
            policy.calls = calls
            callback.run(runtime.join(io.base_url + "/episodes/words/join", callback.base_url,
                                      episode.seats[seat].ticket, policy=policy))
        context = CalendarRuntimeContext(environment, io, episode)
        config = {"secret_word": "dog", "max_turns": turns, "agents": [{"runtime": "external"}] * 2,
                  "turn_timeout_s": 0.15 if expiry else 10}
        game = WordGuessGame(config, runtime_context=context, dry_run=True)
        trace = game.run()
        return trace, invocations, game


@pytest.mark.parametrize("guesses,replies,turns", CASES[:4])
def test_v2_exact_events_observations_results_and_privacy(guesses, replies, turns):
    local, calls = controlled([g.strip() for g in guesses], [r.strip() for r in replies], turns)
    expected = local.run()
    actual, invocations, game = run_remote(guesses, replies, turns)
    assert [(e.type, e.data) for e in actual.events] == [(e.type, e.data) for e in expected.events]
    assert actual.final_state == expected.final_state and actual.metrics == expected.metrics
    actions = [i for i in invocations if i.kind == "turn"]
    assert [("guesser" if i.seat == 0 else "host", i.observation) for i in actions] == calls
    registration = [i for i in invocations if i.kind == "register"]
    assert "secret_word" not in registration[0].observation
    assert "dog" not in registration[0].prompt
    assert registration[1].observation["secret_word"] == "dog"
    assert all(len(records) == 1 for agent in (game.guesser, game.host) for records in agent.records)


@pytest.mark.parametrize("mode", ["missing", "failed", "expiry"])
def test_remote_failure_does_not_invent_actions_or_success(mode):
    with pytest.raises(RuntimeError, match="word_guess runtime action failed") as error:
        run_remote(["I guess: dog"], ["correct!"], 1, **{mode: True})
    assert "private" not in str(error.value)


def test_text_submission_stable_id_dedup_and_exactly_one():
    from a2a_engine.remote.contract import CapabilityClaims, TurnInvocationV2
    from a2a_engine.remote.capabilities import Capabilities
    from a2a_engine.remote.turns import TurnRecorder
    from word_guess.remote import stage_text
    from datetime import datetime, timedelta, timezone
    invocation = TurnInvocationV2(environment_id="word_guess", episode_id="e", attempt_id="a", seat=0,
                                  turn_id="t", kind="turn", operation="act", phase="ACT",
                                  deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
                                  mcp={name: f"http://127.0.0.1:1234/{name}" for name in ("env", "comm", "lifecycle")})
    claims = CapabilityClaims(episode_id="e", attempt_id="a", seat=0, turn_id="t", phase="ACT",
                              exp=invocation.deadline.timestamp(), allowed_tools=["env.submit_action"])
    recorder = TurnRecorder(invocation, claims, Capabilities(), stage_text)
    tool = WORD_GUESS_TOOLS[0]
    assert recorder.call(tool, {"text": "guess"}, "id").status == "accepted"
    assert recorder.call(tool, {"text": "guess"}, "id").status == "accepted"
    assert recorder.call(tool, {"text": "changed"}, "id").code == "call_id_conflict"
    assert recorder.call(tool, {"text": "second"}, "id2").code == "already_submitted"
    assert recorder.call(tool, {"text": "x" * 16385}, "id3").code == "invalid_arguments"
    assert [r["action"] for r in recorder.close() if "action" in r] == [{"text": "guess"}]


def test_real_client_bounds_requests_and_preserves_retry_prompts(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from a2a_engine.llm import api
    from a2a_engine.llm.retry import RetryPolicy
    import urllib.error
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise urllib.error.HTTPError("http://provider.invalid", 503, "controlled", {}, None)
        return " I guess: dog\nignored"
    monkeypatch.setattr(api, "call_llm_oneshot", request)
    client = api.LLMClient(model="mock", retry=RetryPolicy(max_attempts=2, backoff_base=0, jitter_ratio=0))
    strategy = WordGuessStrategy(client=client)
    async def play():
        await strategy.calls(SimpleNamespace(kind="register", observation={"role": "guesser"}))
        invocation = SimpleNamespace(kind="turn", observation={"history": [], "turns_remaining": 2},
                                     deadline=datetime.now(timezone.utc) + timedelta(seconds=3))
        assert await strategy.calls(invocation) == [("env", "submit_action", {"text": "I guess: dog"})]
    asyncio.run(play())
    assert len(calls) == 2 and 0 < calls[1]["timeout"] <= calls[0]["timeout"] < 3
    assert calls[0]["messages"] == calls[1]["messages"] == strategy.agent.build_messages({"history": [], "turns_remaining": 2}, {})
    assert not strategy.model_lock.locked()


def test_lingering_provider_remains_busy_after_cancellation():
    import threading
    from types import SimpleNamespace
    entered, release = threading.Event(), threading.Event()
    class BlockingProvider:
        def oneshot(self, messages):
            entered.set()
            assert release.wait(5)
            return "I guess: dog"
    strategy = WordGuessStrategy(client=BlockingProvider())
    async def play():
        await strategy.calls(SimpleNamespace(kind="register", observation={"role": "guesser"}))
        invocation = SimpleNamespace(kind="turn", observation={"history": [], "turns_remaining": 2})
        task = asyncio.create_task(strategy.calls(invocation))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert strategy.model_lock.locked()
            with pytest.raises(RuntimeError, match="still active"):
                await strategy.calls(invocation)
        finally:
            release.set()
    asyncio.run(play())
    assert not strategy.model_lock.locked()


@pytest.mark.parametrize("remote_seats", [(0,), (1,), (0, 1)])
def test_local_process_and_mixed_seats_use_same_graph_and_persist(tmp_path, remote_seats):
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    from expt_runner.run_experiment import _make_run_context, _run_one
    from a2a_engine.remote.server import RuntimeManager
    config = {"environment_id": "word_guess", "secret_word": "dog", "max_turns": 6,
              "agents": [{"type": "scripted", "runtime": "local_process" if seat in remote_seats else "in_process"}
                         for seat in range(2)]}
    expected = WordGuessGame({"secret_word": "dog", "max_turns": 6}, dry_run=True).run()
    context = _make_run_context("word-process", "cell", config, 0, dry_run=True)
    context["verify_readback"] = True
    store = SQLiteEpisodeStore(path=tmp_path / "words.db")
    with RuntimeManager(WORD_GUESS_TOOLS) as manager:
        context["runtime_manager"] = manager
        _run_one(context, store, tmp_path / "results")
        assert not manager.episodes
    actual = next(store.iter_episodes())
    assert [(event.type, event.data) for event in actual.events] == [(event.type, event.data) for event in expected.events]
    assert actual.final_state == expected.final_state and actual.metrics == expected.metrics


@pytest.mark.parametrize("seat", [0, 1])
def test_seat_flip_parity(seat_flip_trace, seat):
    config = {"environment_id": "word_guess", "secret_word": "dog", "seed": 41, "max_turns": 6,
              "agents": [{"type": "scripted"}, {"type": "scripted"}]}
    traces = [seat_flip_trace(WordGuessGame, config, seat, runtime)
              for runtime in ("in_process", "local_process", "external")]
    def play(trace):
        return [(e.type, e.data) for e in trace.events], trace.final_state, trace.metrics
    assert play(traces[0]) == play(traces[1]) == play(traces[2])


def test_registration_failure_maps_to_seats_unavailable(remote_harness_v2):
    from a2a_engine.remote.lobby import SeatsUnavailable
    harness = remote_harness_v2(seats=(0, 1))
    game = WordGuessGame({"secret_word": "dog", "agents": [{"runtime": "external"}] * 2,
                          "turn_timeout_s": .1}, dry_run=True, runtime_context=harness.context)
    # Calendar's policy cannot consume a Word Guess registration.
    with pytest.raises(SeatsUnavailable, match="^seat_unavailable") as error:
        game.run()
    assert error.value.seats == (0,)
    assert [e.type for e in game.events.all()] == ["game_start"]
    assert not harness.episode.active
