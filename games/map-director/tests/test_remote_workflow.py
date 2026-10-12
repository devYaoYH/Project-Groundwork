"""Actual /2 registration, authenticated image transport, action staging and cleanup."""

from contextlib import ExitStack
from dataclasses import dataclass

import pytest
from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.content import contains_image
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from map_director.environment import MapDirectorEnvironment
from map_director.remote import MAP_TOOLS
from map_director.runtime_strategy import MapDirectorStrategy


@dataclass
class RuntimeContext:
    environment: object
    io: object
    episode: object

    def close(self):
        self.environment.registry.unregister(self.episode)


def remote_game(tmp_path, *, missing=False):
    invocations = []
    policies = []
    with ExitStack() as stack:
        environment = EnvironmentApp(MAP_TOOLS)
        io = stack.enter_context(LoopbackServer(environment.app))
        episode = environment.registry.provision(
            "map-test", [0, 1], environment_id="map_director"
        )
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        for seat in (0, 1):
            policy = MapDirectorStrategy()
            policies.append(policy)
            original = policy.calls

            async def calls(invocation, original=original):
                invocations.append(invocation.model_copy(deep=True))
                if missing and invocation.kind == "turn":
                    return []
                return await original(invocation)

            policy.calls = calls
            callback.run(
                runtime.join(
                    io.base_url + "/episodes/map-test/join",
                    callback.base_url,
                    episode.seats[seat].ticket,
                    policy=policy,
                )
            )
        context = RuntimeContext(environment, io, episode)
        config = {
            "asset_root": str(tmp_path / "remote-assets"),
            "agents": [{"type": "scripted", "runtime": "external"}] * 2,
            "turn_timeout_s": 10,
        }
        game = MapDirectorEnvironment(config, dry_run=True, runtime_context=context)
        trace = game.run()
        assert not episode.active
        return trace, invocations, policies


def test_v2_two_round_private_image_transport_and_parity(tmp_path):
    expected = MapDirectorEnvironment(
        {"asset_root": str(tmp_path / "local-assets")}, dry_run=True
    ).run()
    actual, invocations, policies = remote_game(tmp_path)
    assert actual.final_state == expected.final_state
    assert actual.metrics == expected.metrics
    assert [(e.type, e.data) for e in actual.events] == [
        (e.type, e.data) for e in expected.events
    ]
    actions = [i for i in invocations if i.kind == "turn"]
    assert len(actions) == 4
    assert all(contains_image(i.observation["content"]) for i in actions)
    assert [i.observation["round"] for i in actions] == [1, 1, 2, 2]
    assert all(i.protocol_version == "a2a-turns/2" for i in actions)
    registrations = [i for i in invocations if i.kind == "register"]
    assert [i.observation for i in registrations] == [
        {"role": "giver"},
        {"role": "follower"},
    ]
    assert policies[0].agent is not policies[1].agent
    assert [o["round"] for o in policies[0].agent.observations] == [1, 2]
    assert [o["round"] for o in policies[1].agent.observations] == [1, 2]
    assert (
        actions[0].observation["content"][1]["sha256"]
        != actions[1].observation["content"][1]["sha256"]
    )


def test_missing_remote_action_fails_without_invented_success(tmp_path):
    with pytest.raises(RuntimeError, match="map_director runtime action failed"):
        remote_game(tmp_path, missing=True)
