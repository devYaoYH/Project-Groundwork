import asyncio
from copy import deepcopy

import pytest

from a2a_engine.workflow import environment_workflow
from negotiation_game.backend.engine import GameConfig, GameEngine, GameMode
from negotiation_game.backend import workflow
from negotiation_game.game import NegotiationGame
import runpy
from pathlib import Path

LegacyEngine = runpy.run_path(str(Path(__file__).with_name('legacy_engine.py')))['LegacyEngine']


class ControlledAgent:
    def __init__(self, talks=(), decisions=({},)):
        self.talks, self.decisions = list(talks), list(decisions)
        self.calls = []
        self.last_thinking = self.last_reasoning = self.last_speech = ''

    def _init_session(self, *args):
        self.calls.append(('initialize', deepcopy(args)))
        self._system_prompt = 'controlled prompt'

    def _append_user(self, text):
        self.calls.append(('feedback', text))

    def reset_context(self):
        self.calls.append(('reset',))

    async def cheap_talk(self, *args, **kwargs):
        self.calls.append(('talk', deepcopy(args), deepcopy(kwargs)))
        self.last_thinking = 'thinking'
        self.last_reasoning = 'reasoning'
        self.last_speech = 'accompanying speech'
        return self.talks.pop(0) if self.talks else 'continue talking'

    async def decide_allocation(self, *args):
        self.calls.append(('decide', deepcopy(args)))
        return deepcopy(self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0])

    def notify_round_result(self, *args, **kwargs):
        keys = ('round_number', 'own_allocation', 'own_reward', 'opponent_allocation',
                'opponent_reward', 'overdrawn', 'visible_opponent_reward', 'project_details')
        self.calls.append(('notify', deepcopy(dict(zip(keys, args)) if args else kwargs)))

    async def reflect(self, *args, **kwargs):
        keys = ('agent_id', 'total_rounds', 'own_cumulative_reward', 'opponent_cumulative_reward',
                'visible_opponent_reward', 'theoretical_joint_max')
        self.calls.append(('reflect', deepcopy(dict(zip(keys, args)) if args else kwargs)))
        return 'controlled reflection'


async def execute(engine_cls, cfg, agents, *, single_round=False, stop=False, rotate=False):
    engine = engine_cls(deepcopy(cfg), *agents)
    if rotate:
        def update(round_number):
            for state in (engine.agent_a_state, engine.agent_b_state):
                state.project_calculator.projects[0].reward = 10 * round_number
        engine._update_projects_for_round = update
    events = []
    async def emit(kind, data):
        events.append((kind, deepcopy(data)))
        if stop and kind == 'round_complete':
            engine.stop()
    engine.on_event(emit)
    if engine_cls is GameEngine and not single_round:
        await engine._start_game()
    result = await engine.run_round(2) if single_round else await engine.run_game()
    return result, events, agents, engine


@pytest.mark.parametrize('talks,decisions,config', [
    ((['{"wood": 1}'], ['{"stone": 1}']), (({},), ({},)), {}),
    ((['{"wood": 1}'], ['speech', 'speech']), (({},), ({'stone': 1},)), {}),
    ((['speech', '{broken'], ['{"wood": 999}', 'speech']), (({'wood': -1}, {'wood': 999}, {'wood': 1}), ({'wood': 999},)), {}),
    (([], []), (({'projects': {'project_a': 1}},), ({'stone': 1},)), {'enable_cheap_talk': False}),
    ((['{"projects": {"project_a": 1}}'], ['{"stone": 1}']), (({},), ({},)), {'resource_supply': {'wood': 0, 'stone': 0, 'gold': 0}}),
    ((['speech'], ['speech']), (({},), ({},)), {'mode': GameMode.SHIFTING, 'agent_shifting': [True, True]}),
    ((['speech'], ['speech']), (({},), ({},)), {'rotate_projects': True, 'full_transparency': True}),
    (([], []), (({},), ({},)), {'num_rounds': 0}),
    ((['{bad'], ['{bad']), (({},), ({},)), {'num_rounds': 40, 'cheap_talk_turns': 1}),
])
def test_frozen_backend_exact_parity(talks, decisions, config):
    cfg = GameConfig(episode_uid='fixed', num_rounds=2, cheap_talk_turns=2)
    for key, value in config.items():
        setattr(cfg, key, value)
    def agents():
        return [ControlledAgent(talks[i], decisions[i]) for i in range(2)]
    before = asyncio.run(execute(LegacyEngine, cfg, agents(), rotate=cfg.rotate_projects))
    after = asyncio.run(execute(GameEngine, cfg, agents(), rotate=cfg.rotate_projects))
    assert after[:2] == before[:2]
    assert [a.calls for a in after[2]] == [a.calls for a in before[2]]
    assert after[3].agent_a_state == before[3].agent_a_state or after[3].agent_a_state.memory == before[3].agent_a_state.memory


@pytest.mark.parametrize('single_round,stop', [(True, False), (False, True)])
def test_run_round_and_stop_share_graph_preserve_finalization(single_round, stop):
    cfg = GameConfig(episode_uid='fixed', num_rounds=3, cheap_talk_turns=1)
    before = asyncio.run(execute(LegacyEngine, cfg, [ControlledAgent(), ControlledAgent()], single_round=single_round, stop=stop))
    after = asyncio.run(execute(GameEngine, cfg, [ControlledAgent(), ControlledAgent()], single_round=single_round, stop=stop))
    assert after[:2] == before[:2]
    assert [a.calls for a in after[2]] == [a.calls for a in before[2]]
    if stop:
        assert after[1][-1][0] == 'game_complete' and after[0]['stopped']


def test_nonpositive_talk_runs_exactly_1000_passes():
    cfg = GameConfig(episode_uid='cap', num_rounds=1, cheap_talk_turns=0)
    agents = [ControlledAgent(), ControlledAgent()]
    result, events, _, _ = asyncio.run(execute(GameEngine, cfg, agents))
    for agent in agents:
        talks = [call for call in agent.calls if call[0] == 'talk']
        assert len(talks) == 1000
        assert talks[-1][1][2] == 999
        assert len([call for call in agent.calls if call[0] == 'decide']) == 1
    assert result['rounds'][0]['stats']['num_turns_used'] == 1000
    assert events[-1][0] == 'game_complete'



@pytest.mark.parametrize('turn_cap', [0, -1])
def test_nonpositive_talk_boundary_and_graph_budget(turn_cap):
    from types import SimpleNamespace
    cfg = GameConfig(num_rounds=1, cheap_talk_turns=turn_cap)
    runtime = SimpleNamespace(context=SimpleNamespace(engine=SimpleNamespace(config=cfg)))
    state = {'alloc_a': None, 'alloc_b': None, 'turn': 999}
    assert workflow.talk_destination(state, runtime) == 'continue'
    assert workflow.talk_destination({**state, 'turn': 1000}, runtime) == 'cap'
    assert workflow.talk_destination({**state, 'alloc_a': {}, 'alloc_b': {}}, runtime) == 'both_decided'
    assert workflow.execution_limit(cfg) == workflow.execution_limit(GameConfig(num_rounds=1, cheap_talk_turns=1000))


def test_three_total_attempts_then_empty_allocation():
    cfg = GameConfig(episode_uid='retry', num_rounds=1, enable_cheap_talk=False)
    agents = [ControlledAgent(decisions=({'wood': 999},)), ControlledAgent()]
    result, events, _, _ = asyncio.run(execute(GameEngine, cfg, agents))
    assert result['rounds'][0]['agent_a_allocation'] == {}
    assert len([c for c in agents[0].calls if c[0] == 'decide']) == 3
    assert [data['attempt'] for kind, data in events if kind == 'validation_error' and 'attempt' in data] == [1, 2, 3]


def test_export_never_executes_backend_or_selectors(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('export executed backend')
    monkeypatch.setattr(GameEngine, '__init__', forbidden)
    monkeypatch.setattr(NegotiationGame, '__init__', forbidden)
    monkeypatch.setattr(NegotiationGame, 'runtime_capability', forbidden)
    for target in ('negotiation_game.game.make_agent', 'negotiation_game.game._to_engine_config',
                   'negotiation_game.game.publisher_from_config', 'negotiation_game.remote.RemoteNegotiator.__init__',
                   'negotiation_game.remote.TurnDispatcherV2.__init__',
                   'a2a_engine.remote.server.RuntimeManager.__init__'):
        monkeypatch.setattr(target, forbidden)
    # Preserve callable symbols for structural introspection.
    from types import FunctionType
    builders = {'graph', 'domain', 'chain', 'for_seat', 'cheap_talk_program', 'decision_program',
                'round_program', 'reset_program', 'reflections_program', 'build_program'}
    functions = [(name, fn) for name, fn in vars(workflow).items()
                 if isinstance(fn, FunctionType) and fn.__module__ == workflow.__name__ and name not in builders]
    for name, original in functions:
        def fail(*args, **kwargs):
            return forbidden(*args, **kwargs)
        fail.__name__, fail.__qualname__, fail.__module__ = original.__name__, original.__qualname__, original.__module__
        monkeypatch.setattr(workflow, name, fail)
    output = environment_workflow('negotiation')
    assert output['status'] == 'available'
    assert output['workflow']['diagnostics'] == []
    assert output == environment_workflow('negotiation')
    graphs = output['workflow']['graphs']
    assert len(graphs) == 7
    assert {graph['id']: (len(graph['nodes']), len(graph['edges'])) for graph in graphs} == {
        'root': (11, 12),
        'root/reset_contexts': (8, 9),
        'root/reflections': (6, 7),
        'root/round': (14, 16),
        'root/round/cheap_talk': (11, 17),
        'root/round/decision_a': (10, 12),
        'root/round/decision_b': (10, 12),
    }
    assert sum(len(graph['nodes']) for graph in graphs) == 70
    by_id = {graph['id']: graph for graph in graphs}
    root_nodes = {node['id'] for node in by_id['root']['nodes']}
    assert root_nodes == {'__start__', '__end__', 'open_session_a', 'open_session_b',
                          'next_round', 'reset_contexts', 'round', 'stopped', 'summarize',
                          'reflections', 'complete_game'}
    assert {(edge['source'], edge['target'], edge.get('route_key')) for edge in by_id['root']['edges']} == {
        ('__start__', 'open_session_a', None), ('open_session_a', 'open_session_b', None),
        ('open_session_b', 'next_round', None), ('next_round', 'reset_contexts', 'continue'),
        ('next_round', 'stopped', 'stopped'), ('next_round', 'summarize', 'exhausted'),
        ('reset_contexts', 'round', None), ('round', 'next_round', None),
        ('stopped', 'summarize', None), ('summarize', 'reflections', None),
        ('reflections', 'complete_game', None), ('complete_game', '__end__', None),
    }
    round_nodes = {node['id'] for node in by_id['root/round']['nodes']}
    assert not {'choose_a', 'choose_b', 'check', 'decide_a', 'decide_b', 'result_a', 'result_b', 'project_a'} & round_nodes
    talk_nodes = {node['id'] for node in by_id['root/round/cheap_talk']['nodes']}
    assert {'next_pass', 'talk_call', 'choose_b'} <= talk_nodes
    assert not {'check', 'choose_a', 'a_call', 'b_call'} & talk_nodes
    for suffix in ('a', 'b'):
        decision_nodes = {node['id'] for node in by_id[f'root/round/decision_{suffix}']['nodes']}
        assert {'begin', 'prepare_decision'} <= decision_nodes
        assert 'check' not in decision_nodes
    assert not {'initialize', 'register_a', 'register_b', 'end_a', 'end_b'} & {node['id'] for node in graphs[0]['nodes']}
    assert not {'register', 'episode_end'} & {node.get('operation') for graph in graphs for node in graph['nodes']}
    for graph in graphs:
        assert {node['id'] for node in graph['nodes']} == {edge[key] for edge in graph['edges'] for key in ('source', 'target')}
    keys = {edge.get('route_key') for graph in graphs for edge in graph['edges']}
    assert {'both_decided', 'cap', 'bypass_decided', 'retry', 'exhausted', 'stopped', 'reflect', 'bypass'} <= keys
    actors = {node.get('actor_selector') for graph in graphs for node in graph['nodes']}
    assert any(name and name.endswith('select_a') for name in actors)
    assert any(name and name.endswith('select_b') for name in actors)
    import json
    assert len(json.dumps(output)) < 65000
    assert all(not node.get('source_ref', {}).get('path', '').startswith('/') for graph in graphs for node in graph['nodes'])


def test_actual_human_future_stays_on_episode_loop():
    from negotiation_game.backend.agents.human import HumanAgent
    async def run():
        human = HumanAgent()
        engine = GameEngine(GameConfig(num_rounds=1, cheap_talk_turns=1), human, ControlledAgent(['{}']))
        events = []
        async def emit(kind, data):
            events.append(kind)
        engine.on_event(emit)
        await engine._start_game()
        task = asyncio.create_task(engine.run_game())
        for _ in range(1000):
            if human._pending is not None:
                break
            await asyncio.sleep(.001)
        assert human._loop is asyncio.get_running_loop()
        human.submit_input('{}')
        result = await asyncio.wait_for(task, 5)
        assert result['num_rounds'] == 1
        assert events.count('game_start') == 1
    asyncio.run(run())


@pytest.mark.parametrize('violation', ['hidden', 'second', 'wrong_actor', 'wrong_operation'])
def test_negotiation_gateway_rejects_hidden_or_mismatched_invocations(violation):
    from types import SimpleNamespace
    from a2a_engine.workflow_runtime import AgentStep
    actor = ControlledAgent()
    context = SimpleNamespace(engine=None)
    async def adapter(selected, operation, observation, state, context):
        await workflow.invoke(selected, operation, observation, state, context)
        await workflow.invoke(selected, operation, observation, state, context)
    async def run():
        if violation == 'hidden':
            await workflow.invoke(actor, 'register', {}, {}, context)
        else:
            async def mismatched(selected, operation, observation, state, context):
                await workflow.invoke(ControlledAgent() if violation == 'wrong_actor' else selected,
                                      'episode_end' if violation == 'wrong_operation' else operation,
                                      observation, state, context)
            step = AgentStep(lambda state, context: actor, 'register', workflow.no_observation,
                             adapter if violation == 'second' else mismatched)
            await step({}, SimpleNamespace(context=context))
    with pytest.raises(RuntimeError, match='AgentStep'):
        asyncio.run(run())


@pytest.mark.parametrize('actor', ['agent_a', 'agent_b'])
def test_fixed_seat_notification_observation_does_not_mutate_actor(actor):
    from types import SimpleNamespace
    state = {'actor': actor, 'round_num_a': 1, 'round_num_b': 2,
             'alloc_a': {'wood': 1}, 'alloc_b': {'stone': 2},
             'reward_a': 10, 'reward_b': 20, 'overdrawn': False,
             'proj_details_a': {'a': 1}, 'proj_details_b': {'b': 2}}
    original = deepcopy(state)
    context = SimpleNamespace(engine=SimpleNamespace(config=SimpleNamespace(visible_opponent_reward=False)))
    for label in ('agent_a', 'agent_b'):
        observe = workflow.for_seat(workflow.notification_observation, label)
        assert observe(state, context) == workflow.notification_observation({**state, 'actor': label}, context)
        assert state == original


@pytest.mark.parametrize('actor', [None, 'agent_a', 'agent_b'])
def test_fixed_seat_session_observation_does_not_require_or_mutate_actor(actor):
    engine = GameEngine(GameConfig(), ControlledAgent(), ControlledAgent())
    context = workflow.GameContext(engine)
    state = {'round_number': 1, 'reflections': {}}
    if actor is not None:
        state['actor'] = actor
    original = deepcopy(state)
    for label in ('agent_a', 'agent_b'):
        observe = workflow.for_seat(workflow.actor_observation, label)
        assert observe(state, context) == workflow.actor_observation({**state, 'actor': label}, context)
        assert state == original


@pytest.mark.parametrize('enable_talk', [False, True])
@pytest.mark.parametrize('first_speaker', [0, 1])
def test_fixed_seat_round_preserves_notification_order_and_standalone_state(enable_talk, first_speaker):
    cfg = GameConfig(episode_uid='fixed-seat', num_rounds=3, cheap_talk_turns=1,
                     enable_cheap_talk=enable_talk, first_speaker=first_speaker,
                     mode=GameMode.SHIFTING, agent_shifting=[True, False], rotate_projects=True)

    async def run(engine_cls):
        notifications, events = [], []

        class OrderedAgent(ControlledAgent):
            def notify_round_result(self, *args, **kwargs):
                notifications.append(self.label)
                super().notify_round_result(*args, **kwargs)

        agents = [OrderedAgent(['{"wood": 1}'], ({'wood': 1},)),
                  OrderedAgent(['{"stone": 2}'], ({'stone': 2},))]
        for agent, label in zip(agents, ('agent_a', 'agent_b')):
            agent.label = label
        engine = engine_cls(deepcopy(cfg), *agents)

        async def emit(kind, data):
            events.append((kind, deepcopy(data)))

        engine.on_event(emit)
        if engine_cls is GameEngine:
            state = await workflow.round_program().ainvoke(
                {'round_number': 2, 'actor': 'agent_a'}, context=workflow.GameContext(engine),
                config={'recursion_limit': workflow.execution_limit(cfg)})
            assert state['actor'] == 'agent_b'
            assert state['round_number'] == 3
            assert state['result'].round_number == 2
            result = state['result']
        else:
            result = await engine.run_round(2)
        assert notifications == ['agent_a', 'agent_b']
        assert all(not any(call[0] == 'reset' for call in agent.calls) for agent in agents)
        assert engine.results == [result]
        return result, events, [agent.calls for agent in agents], [
            engine.agent_a_state.memory, engine.agent_b_state.memory]

    assert asyncio.run(run(GameEngine)) == asyncio.run(run(LegacyEngine))
