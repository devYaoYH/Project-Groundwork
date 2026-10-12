import asyncio
import runpy
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.contract import PROTOCOL_VERSION_V2
from a2a_engine.remote.lobby import EpisodeSession, Registration, SeatsUnavailable
from a2a_engine.remote.server import EnvironmentApp
from negotiation_game.backend.engine import GameEngine
from negotiation_game.game import NegotiationConfig, NegotiationGame, _to_engine_config
from negotiation_game.remote import NEGOTIATION_TOOLS, RemoteNegotiator
from negotiation_game.runtime_strategy import NegotiationStrategy

ROOT = Path(__file__).resolve().parents[3]
fixture = runpy.run_path(str(ROOT / 'tests/runtime/negotiation_fixture.py'))
LegacyEngine = runpy.run_path(str(Path(__file__).with_name('legacy_engine.py')))['LegacyEngine']


def baseline(config, mode):
    cfg = _to_engine_config(NegotiationConfig(**config))
    clients = [fixture['make_controlled'](mode, seat, config['enable_cheap_talk']) for seat in (0, 1)]
    engine = LegacyEngine(cfg, clients[config['first_speaker']], clients[1 - config['first_speaker']])
    events = []
    async def emit(kind, data):
        events.append((kind, deepcopy(data)))
    engine.on_event(emit)
    return asyncio.run(engine.run_game()), events, clients


@pytest.mark.parametrize('mode', ['heuristic', 'mock'])
@pytest.mark.parametrize('talk', [True, False])
def test_real_loopback_v2_observations_history_events_and_results(mode, talk):
    config = {**fixture['CONFIG'], 'enable_cheap_talk': talk}
    expected, events, clients = baseline(config, mode)
    with ExitStack() as stack:
        environment = EnvironmentApp(NEGOTIATION_TOOLS)
        io = stack.enter_context(LoopbackServer(environment.app))
        episode = environment.registry.provision('negotiation', [0, 1], environment_id='negotiation')
        stack.callback(environment.registry.unregister, episode)
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        policies = []
        for seat in (0, 1):
            policy = NegotiationStrategy(agent=fixture['make_controlled'](mode, seat, talk))
            policies.append(policy)
            callback.run(runtime.join(f'{io.base_url}/episodes/negotiation/join', callback.base_url,
                                      episode.seats[seat].ticket, policy=policy))
        context = SimpleNamespace(environment=environment, io=io, episode=episode)
        remotes = [RemoteNegotiator(context, seat) for seat in (0, 1)]
        engine = GameEngine(_to_engine_config(NegotiationConfig(**config)), remotes[1], remotes[0])
        actual_events = []
        async def emit(kind, data):
            actual_events.append((kind, deepcopy(data)))
        engine.on_event(emit)
        async def play():
            await engine._start_game()
            session = EpisodeSession({**config, 'agents': [{'runtime': 'external'}, {'runtime': 'external'}]}, context)
            with session.started({1: Registration({}), 0: Registration({})},
                                 protocols=frozenset({PROTOCOL_VERSION_V2}), register_timeout_s=120):
                return await engine.run_game()
        assert asyncio.run(play()) == expected
        assert actual_events == events
        if mode == 'mock':
            for seat in (0, 1):
                assert policies[seat].agent.messages == clients[seat].messages
                assert policies[seat].agent._messages == clients[seat]._messages
        for remote in remotes:
            assert all(len([r for r in records if 'action' in r]) == 1 for records in remote.records)


def test_candidate_transport_retains_invalid_allocations_and_deduplicates():
    from datetime import datetime, timedelta, timezone
    from a2a_engine.remote.capabilities import Capabilities
    from a2a_engine.remote.contract import CapabilityClaims, TurnInvocationV2
    from a2a_engine.remote.turns import TurnRecorder
    from negotiation_game.remote import stage_candidate
    capabilities = Capabilities()
    invocation = TurnInvocationV2(environment_id='negotiation', episode_id='test', attempt_id='attempt', seat=0,
        turn_id='turn', kind='turn', operation='decide_allocation', phase='DECIDE_ALLOCATION',
        deadline=datetime.now(timezone.utc) + timedelta(seconds=5), mcp={})
    claims = CapabilityClaims(episode_id='test', attempt_id='attempt', seat=0, turn_id='turn',
        phase='DECIDE_ALLOCATION', allowed_tools=['env.submit_candidate'], exp=invocation.deadline.timestamp())
    recorder = TurnRecorder(invocation, claims, capabilities, stage_candidate)
    spec = NEGOTIATION_TOOLS[0]
    payload = {'value': {'wood': -1, 'stone': 999}}
    assert recorder.call(spec, payload, 'call').status == 'accepted'
    assert recorder.call(spec, payload, 'call').status == 'accepted'
    assert recorder.call(spec, {'value': {}}, 'call').code == 'call_id_conflict'
    assert recorder.call(spec, payload, 'second').code == 'already_submitted'
    assert recorder.records[0]['action']['value'] == payload['value']
    recorder.close()
    assert recorder.call(spec, payload, 'late').code == 'closed_turn'


def test_real_local_process_mixed_seat_preserves_mapping():
    config = {**fixture['CONFIG'], 'num_rounds': 1, 'enable_cheap_talk': False,
              'agents': [{'type': 'heuristic', 'runtime': 'local_process'}, {'type': 'heuristic'}]}
    from a2a_engine.remote.server import RuntimeManager
    with RuntimeManager(NegotiationGame.runtime_capability().tools) as manager:
        trace = NegotiationGame(config, runtime_manager=manager).run()
        assert trace.metrics['num_rounds'] == 1
        assert trace.final_state['first_speaker'] == 1
        assert not manager.episodes


@pytest.mark.parametrize('mode', ['missing', 'failed', 'expiry', 'staged_failed', 'staged_expiry'])
def test_failed_remote_operation_never_resolves_or_completes_episode(mode):
    from a2a_agent.mcp_client import execute
    from a2a_engine.remote.server import ManagedEpisode, RuntimeManager
    with ExitStack() as stack:
        manager = stack.enter_context(RuntimeManager(NEGOTIATION_TOOLS))
        episode = manager.environment.registry.provision('failure', [0, 1], environment_id='negotiation')
        managed = ManagedEpisode(manager, episode)
        manager.episodes.add(managed)
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        for seat in (0, 1):
            policy = NegotiationStrategy(agent=fixture['make_controlled']('heuristic', seat))
            original = policy.calls
            async def calls(invocation, original=original):
                if invocation.operation == 'cheap_talk':
                    if mode.startswith('staged'):
                        await execute(invocation, await original(invocation))
                    if mode in {'failed', 'staged_failed'}:
                        raise RuntimeError('private provider details')
                    if mode in {'expiry', 'staged_expiry'}:
                        await asyncio.sleep(2)
                    if mode == 'missing':
                        return []
                return await original(invocation)
            policy.calls = calls
            callback.run(runtime.join(manager.io.base_url + '/episodes/failure/join', callback.base_url,
                                      episode.seats[seat].ticket, policy=policy))
        game = NegotiationGame({**fixture['CONFIG'], 'agents': [{'runtime': 'external'}] * 2},
                               runtime_context=managed)
        original_make = game._make_agents
        agents = []
        def make_agents():
            agents.extend(original_make())
            if 'expiry' in mode:
                agents[0].timeout_s = .2
            return agents
        game._make_agents = make_agents
        with pytest.raises(RuntimeError, match='negotiation runtime (operation failed|candidate missing)') as error:
            game.run()
        assert 'private' not in str(error.value)
        assert not any(event.type in {'early_decision', 'round_complete', 'game_complete'} for event in game.events)
        assert managed.closed and not manager.episodes
        if mode.startswith('staged'):
            assert len([r for r in agents[0].records[-1] if 'action' in r]) == 1
        recorder = episode.seats[1].recorder
        assert recorder.closed
        assert recorder.call(NEGOTIATION_TOOLS[0], {'value': '{}'}, 'late').code == 'closed_turn'


def test_provider_retries_and_heuristic_fallback_stay_inside_one_operation():
    from a2a_engine.llm.retry import RateLimiter, RetryPolicy
    from negotiation_game.backend.agents.llm import LLMAgentBase
    class TransientError(Exception):
        status_code = 503
    class Provider(LLMAgentBase):
        def __init__(self, exhausted):
            super().__init__(model='controlled')
            self._retry_policy = RetryPolicy(max_attempts=3, backoff_base=0, backoff_max=0,
                                            jitter_ratio=0, on_exhausted='return_none')
            self._limiter = RateLimiter(0)
            self._fallback = fixture['SeededHeuristic'](0)
            self.exhausted, self.messages = exhausted, []
        async def _call_api(self):
            self.messages.append(deepcopy(self._messages))
            if self.exhausted or len(self.messages) < 3:
                raise TransientError('provider request failed')
            return {'text': '{"thinking":"recovered","speech":"hello"}', 'model': 'controlled'}
    async def play(exhausted):
        cfg = _to_engine_config(NegotiationConfig(**fixture['CONFIG']))
        local, remote = Provider(exhausted), Provider(exhausted)
        engine = GameEngine(cfg, local, local)
        info = {'agent_id': 'agent_a', 'round_number': 1, 'turn_number': 0,
                'game_config_public': engine._public_config('agent_a'), 'conversation': [],
                'own_memory': [], 'project_update': None}
        expected = await local.cheap_talk('agent_a', 1, 0, info['game_config_public'], [], [])
        calls = await NegotiationStrategy(agent=remote).calls(SimpleNamespace(operation='cheap_talk', observation=info))
        assert len(calls) == 1
        output = calls[0][2]
        assert output['value'] == expected
        assert remote.messages == local.messages and len(remote.messages) == 3
        assert remote._messages == local._messages
        if exhausted:
            assert output['api_meta']['retry_count'] == 3
            assert output['api_meta']['used_fallback']
            assert output['api_meta']['error_type'] == 'TransientError'
            assert len(remote._messages) == 1  # failed user prompt was removed
        else:
            assert output['thinking'] == 'recovered'
    asyncio.run(play(False))
    asyncio.run(play(True))


def test_cancelled_backend_provider_cannot_overlap_history_or_next_call():
    import threading
    from negotiation_game.backend.agents.llm import LLMAgentBase
    entered, release = threading.Event(), threading.Event()
    class BlockingProvider(LLMAgentBase):
        async def _call_api(self):
            def request():
                entered.set()
                assert release.wait(5)
                return {'text': '{"speech":"done"}'}
            return await asyncio.to_thread(request)
    agent = BlockingProvider(model='controlled')
    agent._limiter.min_interval = 0
    strategy = NegotiationStrategy(agent=agent)
    engine = GameEngine(_to_engine_config(NegotiationConfig(**fixture['CONFIG'])), agent, agent)
    invocation = SimpleNamespace(operation='cheap_talk', observation={
        'agent_id': 'agent_a', 'round_number': 1, 'turn_number': 0,
        'game_config_public': engine._public_config('agent_a'), 'conversation': [],
        'own_memory': [], 'project_update': None})
    async def play():
        task = asyncio.create_task(strategy.calls(invocation))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            history = deepcopy(agent._messages)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(RuntimeError, match='still active'):
                await strategy.calls(invocation)
            assert agent._messages == history
        finally:
            release.set()
    asyncio.run(play())
    assert not strategy.model_lock.locked()


@pytest.mark.parametrize('payload', [
    {'value': 'x' * 32769}, {'value': {'wood': 1.5}}, {'value': {'wood': 10**13}},
    {'value': {'projects': {'p' * 129: 1}}}, {'supported': ['arbitrary_method']},
    {'api_meta': {'raw_response': 'excluded'}}, {'unknown': 'excluded'},
])
def test_candidate_bounds_and_metadata_allowlist(payload):
    from pydantic import ValidationError
    from negotiation_game.remote import CandidateOutput
    with pytest.raises(ValidationError):
        CandidateOutput(**payload)


def test_strategy_metadata_drops_raw_provider_data_and_sanitizes_errors():
    class Agent:
        last_api_meta = {'error_type': 'ProviderError', 'error_message': 'secret credential',
                         'retry_count': 3, 'used_fallback': True, 'raw_response': 'secret body'}
        async def cheap_talk(self, *args, **kwargs):
            return 'fallback'
    info = {'agent_id': 'agent_a', 'round_number': 1, 'turn_number': 0,
            'game_config_public': {}, 'conversation': [], 'own_memory': [], 'project_update': None}
    output = asyncio.run(NegotiationStrategy(agent=Agent()).calls(SimpleNamespace(operation='cheap_talk', observation=info)))
    assert 'secret' not in str(output)
    assert output[0][2]['api_meta']['error_message'] == 'provider request failed'


def test_remote_context_closes_even_when_backend_config_conversion_fails():
    from a2a_engine.remote.server import RuntimeManager
    from a2a_engine.remote.lobby import open_episode
    config = NegotiationConfig(mode='invalid', agents=[{'runtime': 'local_process'}, {'type': 'heuristic'}]).model_dump()
    with RuntimeManager(NEGOTIATION_TOOLS) as manager:
        session = open_episode(manager, config, scripted=True)
        game = NegotiationGame(config, episode=session)
        with session:
            with pytest.raises(ValueError):
                game.run()
            assert not session.closed and not session.managed.closed
        assert session.closed and session.managed.closed
        assert not manager.episodes


@pytest.mark.parametrize('seat', [0, 1])
def test_seat_flip_parity(seat_flip_trace, seat):
    config = {**fixture['CONFIG'], 'num_rounds': 1, 'enable_cheap_talk': False,
              'resource_types': ['wood'], 'resource_supply': {'wood': 2},
              'resource_costs': {'wood': 1}, 'agent_budget': 1, 'max_resource_types_per_turn': 1,
              'agents': [{'type': 'heuristic'}, {'type': 'heuristic'}]}
    traces = [seat_flip_trace(NegotiationGame, config, seat, runtime)
              for runtime in ('in_process', 'local_process', 'external')]
    for trace in traces[1:]:
        assert [(e.type, e.data) for e in trace.events] == [(e.type, e.data) for e in traces[0].events]
        assert trace.final_state == traces[0].final_state
        assert trace.metrics == traces[0].metrics


@pytest.mark.parametrize('failed_seat', [0, 1])
def test_registration_failure_notifies_only_previously_registered_seats(failed_seat):
    from a2a_engine.remote.server import RuntimeManager
    with ExitStack() as stack:
        manager = stack.enter_context(RuntimeManager(NEGOTIATION_TOOLS))
        config = {**fixture['CONFIG'], 'agents': [{'runtime': 'external'}, {'runtime': 'external'}]}
        episode = manager.environment.registry.provision('register-failure', [0, 1], environment_id='negotiation')
        from a2a_engine.remote.server import ManagedEpisode
        managed = ManagedEpisode(manager, episode)
        manager.episodes.add(managed)
        session = stack.enter_context(EpisodeSession(config, managed))
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        calls_seen = []
        for seat in (0, 1):
            policy = NegotiationStrategy(agent=fixture['make_controlled']('heuristic', seat))
            original = policy.calls
            async def calls(invocation, original=original, seat=seat):
                calls_seen.append((seat, invocation.operation))
                if seat == failed_seat and invocation.operation == 'register':
                    raise RuntimeError('private provider details')
                return await original(invocation)
            policy.calls = calls
            callback.run(runtime.join(manager.io.base_url + '/episodes/register-failure/join', callback.base_url,
                                      episode.seats[seat].ticket, policy=policy))
        game = NegotiationGame(config, episode=session)
        assert list(game.registrations(None)) == [1, 0]
        with pytest.raises(SeatsUnavailable) as error:
            game.run()
        assert error.value.seats == (failed_seat,)
        assert [event.type for event in game.events] == ['game_start']
        assert [seat for seat, operation in calls_seen if operation == 'episode_end'] == ([1] if failed_seat == 0 else [])
        assert not managed.closed


@pytest.mark.parametrize('environment,kind,operation,phase,parent', [
    ('negotiation', 'turn', 'act', 'ACT', None),
    ('negotiation', 'turn', 'cheap_talk', 'DECISION', None),
    ('negotiation', 'turn', 'cheap_talk', 'CHEAP_TALK', 'DECISION'),
    ('negotiation', 'reflect', 'reflect', 'REFLECT', None),
    ('calendar', 'turn', 'cheap_talk', 'CHEAP_TALK', None),
    ('word_guess', 'turn', 'notify_round_result', 'NOTIFY_ROUND_RESULT', None),
])
def test_runtime_admission_enforces_environment_operation_and_phase(environment, kind, operation, phase, parent):
    from datetime import datetime, timedelta, timezone
    from a2a_agent.runtime_v2 import Session
    from a2a_engine.remote.contract import TurnInvocationV2
    runtime = AgentRuntimeV2()
    runtime.sessions[(environment, 'e', 'a', 0)] = Session(
        SimpleNamespace(seat_secret='synthetic', mcp={}), None, runtime.clock() + 60, registered=True)
    invocation = TurnInvocationV2(environment_id=environment, episode_id='e', attempt_id='a', seat=0,
        turn_id='t', kind=kind, operation=operation, phase=phase, parent_phase=parent,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=5), mcp={},
        capability='synthetic' if kind == 'turn' else None, lifecycle_credential='synthetic')
    with pytest.raises(ValueError):
        runtime.start(invocation, 'synthetic')
    assert not runtime.records
