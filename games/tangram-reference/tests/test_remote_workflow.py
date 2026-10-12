"""Actual /2 visual turn transport, repeated private pixel policies and lifecycle."""

from contextlib import ExitStack
from dataclasses import dataclass
import pytest
from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.content import contains_image
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from tangram_reference.environment import TangramReferenceEnvironment
from tangram_reference.remote import TANGRAM_TOOLS
from tangram_reference.runtime_strategy import TangramReferenceStrategy


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
        environment = EnvironmentApp(TANGRAM_TOOLS)
        io = stack.enter_context(LoopbackServer(environment.app))
        episode = environment.registry.provision(
            "tangram-test", [0, 1], environment_id="tangram_reference"
        )
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        for seat in (0, 1):
            policy = TangramReferenceStrategy()
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
                    io.base_url + "/episodes/tangram-test/join",
                    callback.base_url,
                    episode.seats[seat].ticket,
                    policy=policy,
                )
            )
        context = RuntimeContext(environment, io, episode)
        game = TangramReferenceEnvironment(
            {
                "num_rounds": 2,
                "seed": 17,
                "asset_root": str(tmp_path / "remote-assets"),
                "agents": [{"type": "scripted", "runtime": "external"}] * 2,
            },
            dry_run=True,
            runtime_context=context,
        )
        trace = game.run()
        assert not episode.active
        return trace, invocations, policies


def test_v2_two_round_pixel_policy_parity_and_private_registration(tmp_path):
    expected = TangramReferenceEnvironment(
        {"num_rounds": 2, "seed": 17, "asset_root": str(tmp_path / "local-assets")},
        dry_run=True,
    ).run()
    actual, invocations, policies = remote_game(tmp_path)
    assert actual.metrics == expected.metrics
    assert actual.final_state == expected.final_state
    assert [(e.type, e.data) for e in actual.events] == [
        (e.type, e.data) for e in expected.events
    ]
    turns = [i for i in invocations if i.kind == "turn"]
    assert len(turns) == 4
    assert [i.observation["round"] for i in turns] == [1, 1, 2, 2]
    assert all(i.protocol_version == "a2a-turns/2" for i in turns)
    assert all(contains_image(i.observation["content"]) for i in turns)
    assert all("data" in i.observation["content"][-1] for i in turns)
    assert (
        turns[0].observation["content"][-1]["sha256"]
        != turns[1].observation["content"][-1]["sha256"]
    )
    registrations = [i for i in invocations if i.kind == "register"]
    assert [i.observation for i in registrations] == [
        {"role": "director", "num_rounds": 2},
        {"role": "matcher", "num_rounds": 2},
    ]
    assert not any(contains_image(i.observation) for i in registrations)
    assert [o["round"] for o in policies[0].agent.observations] == [1, 2]
    assert [o["round"] for o in policies[1].agent.observations] == [1, 2]
    assert all(p.agent.asset_root is None for p in policies)


def test_remote_missing_action_fails_without_fabricated_success(tmp_path):
    with pytest.raises(RuntimeError, match="tangram runtime action failed"):
        remote_game(tmp_path, missing=True)


def test_local_process_strategy_hook_two_round_sqlite_parity(tmp_path):
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    from expt_runner.run_experiment import _make_run_context, _run_one
    from a2a_engine.remote.server import RuntimeManager

    config = {
        "environment_id": "tangram_reference",
        "num_rounds": 2,
        "seed": 17,
        "asset_root": str(tmp_path / "process-assets"),
        "agents": [
            {
                "type": "scripted",
                "runtime": "local_process",
                "capabilities": {
                    "input": ["text", "image"],
                    "evidence": "tangram_reference.agents.ScriptedAgent pixel policy v1",
                },
            }
        ]
        * 2,
    }
    context = _make_run_context("tangram-process", "cell", config, 0, dry_run=True)
    # The runner derives an episode seed from the declared root. Compare both
    # runtimes using that same resolved episode seed and frozen layout draw.
    expected = TangramReferenceEnvironment(
        {
            "num_rounds": 2,
            "seed": context["config"]["seed"],
            "asset_root": str(tmp_path / "expected-assets"),
        },
        dry_run=True,
    ).run()
    context["verify_readback"] = True
    store = SQLiteEpisodeStore(path=tmp_path / "tangram.db")
    with RuntimeManager(TANGRAM_TOOLS) as manager:
        context["runtime_manager"] = manager
        _run_one(context, store, tmp_path / "results")
        assert not manager.episodes
    actual = next(store.iter_episodes())
    assert actual.metrics == expected.metrics
    assert actual.final_state == expected.final_state
    assert [(e.type, e.data) for e in actual.events] == [
        (e.type, e.data) for e in expected.events
    ]
