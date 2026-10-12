"""Real loopback transport fixtures shared by Phase 2 package tests."""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest


def _calendar_session(stack, context, seats):
    from a2a_engine.remote.lobby import EpisodeSession
    from calendar_game.game import CalendarGame

    config = {"num_agents": max(2, max(seats, default=0) + 1), "agents": [
        {"type": "scripted", "runtime": "external" if seat in seats else "in_process"}
        for seat in range(max(2, max(seats, default=0) + 1))]}
    session = stack.enter_context(EpisodeSession(config, context))

    def register_client(client, game_config, *, select_dispatcher=True):
        from dataclasses import replace
        client.config = game_config
        target_session = session
        target_config = config
        if client.runtime is not context:
            target_config = {"num_agents": game_config.num_agents, "agents": [
                {"type": "scripted", "runtime": "external" if seat in client.runtime.episode.seats else "in_process"}
                for seat in range(game_config.num_agents)]}
            target_session = stack.enter_context(EpisodeSession(target_config, client.runtime))
        setup = SimpleNamespace(domain=SimpleNamespace(game_configs=[
            replace(game_config, agent_id=seat) for seat in range(target_config["num_agents"])]))
        registrations = CalendarGame(target_config).registrations(setup)
        stage = target_session.started(registrations, protocols=CalendarGame.runtime_capability().protocols,
                                       register_timeout_s=min(client._timeout("register"), 1))
        stack.enter_context(stage)
        if select_dispatcher:
            client._select_dispatcher()
        return stage

    return session, register_client


@pytest.fixture
def seat_flip_trace(tmp_path):
    """Run one scripted seat via each deployment path, including its descriptor."""
    import json
    from copy import deepcopy
    from a2a_engine.remote.lobby import open_episode
    from a2a_engine.remote.seats import LocalProcessLauncher
    from a2a_engine.remote.server import RuntimeManager

    def run(game_cls, config, seat_id, runtime):
        config = deepcopy(config)
        config["agents"][seat_id]["runtime"] = runtime
        capability = game_cls.runtime_capability()
        with ExitStack() as stack:
            manager = stack.enter_context(RuntimeManager(capability.tools, provisioning_dir=tmp_path / "joins"))
            session = stack.enter_context(open_episode(manager, config, scripted=True))
            if runtime == "external":
                descriptor = json.loads(session.managed.descriptors[0].read_text())
                child = LocalProcessLauncher(descriptor["join_url"], descriptor["join_ticket"],
                                             {"type": "scripted"}, environment_id=descriptor["environment_id"])
                session.managed.children.append(child)
            trace = game_cls(config, dry_run=True, episode=session).run()
        assert not manager.episodes
        return trace

    return run


@pytest.fixture
def remote_harness():
    from a2a_agent.server import ScriptedRuntime
    from a2a_engine.remote.dispatch import LoopbackServer
    from a2a_engine.remote.server import EnvironmentApp
    from calendar_game.remote import CALENDAR_TOOLS, CalendarRuntimeContext

    with ExitStack() as stack:
        def create(*, seats=(0,), join=True, episode_id="episode", join_timeout_s=10):
            environment = EnvironmentApp(CALENDAR_TOOLS)
            io = stack.enter_context(LoopbackServer(environment.app))
            episode = environment.registry.provision(episode_id, seats, join_timeout_s=join_timeout_s)
            stack.callback(environment.registry.unregister, episode)
            runtimes = {}
            callbacks = {}
            invocations = []
            for seat in seats:
                runtime = ScriptedRuntime(episode.seats[seat].ticket)
                policy_calls = runtime.policy.calls

                def record(invocation, calls=policy_calls):
                    invocations.append(invocation)
                    return calls(invocation)

                runtime.policy.calls = record
                callback = stack.enter_context(LoopbackServer(runtime.app))
                runtimes[seat] = runtime
                callbacks[seat] = callback
                if join:
                    callback.run(runtime.join(f"{io.base_url}/episodes/{episode_id}/join", callback.base_url))
            context = CalendarRuntimeContext(environment, io, episode)
            session, register_client = _calendar_session(stack, context, seats)
            return SimpleNamespace(environment=environment, io=io, episode=episode, runtimes=runtimes,
                                   callbacks=callbacks, invocations=invocations,
                                   context=context, session=session, register_client=register_client)
        yield create


@pytest.fixture
def remote_harness_v2():
    from a2a_agent.server import AgentRuntimeV2
    from a2a_engine.remote.dispatch import LoopbackServer
    from a2a_engine.remote.server import EnvironmentApp
    from calendar_game.remote import CALENDAR_TOOLS, CalendarRuntimeContext

    with ExitStack() as stack:
        def create(*, seats=(0,), join=True, episode_id="episode", join_timeout_s=10):
            environment = EnvironmentApp(CALENDAR_TOOLS)
            io = stack.enter_context(LoopbackServer(environment.app))
            episode = environment.registry.provision(episode_id, seats, join_timeout_s=join_timeout_s)
            stack.callback(environment.registry.unregister, episode)
            runtime = AgentRuntimeV2()
            callback = stack.enter_context(LoopbackServer(runtime.app))
            invocations = []
            policies = {}
            for seat in seats:
                from a2a_agent.scripted import ScriptedPolicy
                policy = ScriptedPolicy()
                calls = policy.calls

                def record(invocation, original=calls):
                    invocations.append(invocation)
                    return original(invocation)

                policy.calls = record
                policies[seat] = policy
                if join:
                    callback.run(runtime.join(f"{io.base_url}/episodes/{episode_id}/join", callback.base_url,
                                              episode.seats[seat].ticket, policy=policy))
            context = CalendarRuntimeContext(environment, io, episode)
            session, register_client = _calendar_session(stack, context, seats)
            return SimpleNamespace(environment=environment, io=io, episode=episode, runtime=runtime,
                                   callback=callback, invocations=invocations, policies=policies,
                                   context=context, session=session, register_client=register_client)
        yield create


@pytest.fixture
def open_remote_turn(remote_harness):
    from calendar_game.agents import GameConfig
    from calendar_game.game import CalendarGame
    from calendar_game.remote import RemoteSeatClient

    harness = remote_harness()
    game = CalendarGame({"num_agents": 2, "agents": [{"type": "scripted"}] * 2})
    client = RemoteSeatClient(harness.context, 0, game.router)
    harness.register_client(client, GameConfig(2, 2, 0, [0, 1], decision_retries=0))
    client.start_round({"id": 0, "participants": [0, 1], "cost": 1, "duration": 1}, "Slot 0: [FREE]\nSlot 1: [FREE]", 0)
    client.prepare({"phase": "DECISION", "round": 0, "turn": 0, "prompt_sent": "decide",
                    "calendar_snapshot_render": "Slot 0: [FREE]\nSlot 1: [FREE]"}, 0)
    harness.client = client
    return harness
