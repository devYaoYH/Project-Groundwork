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


@pytest.mark.parametrize('turn_cap', [0, -1])
def test_nonpositive_talk_runs_exactly_1000_passes(turn_cap):
    cfg = GameConfig(episode_uid='cap', num_rounds=1, cheap_talk_turns=turn_cap)
    agents = [ControlledAgent(), ControlledAgent()]
    result, events, _, _ = asyncio.run(execute(GameEngine, cfg, agents))
    for agent in agents:
        talks = [call for call in agent.calls if call[0] == 'talk']
        assert len(talks) == 1000
        assert talks[-1][1][2] == 999
        assert len([call for call in agent.calls if call[0] == 'decide']) == 1
    assert result['rounds'][0]['stats']['num_turns_used'] == 1000
    assert events[-1][0] == 'game_complete'


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
    builders = {'graph', 'domain', 'chain', 'cheap_talk_program', 'decision_program', 'round_program', 'build_program'}
    functions = [(name, fn) for name, fn in vars(workflow).items()
                 if isinstance(fn, FunctionType) and fn.__module__ == workflow.__name__ and name not in builders]
    for name, original in functions:
        def fail(*args, **kwargs):
            return forbidden(*args, **kwargs)
        fail.__name__, fail.__qualname__, fail.__module__ = original.__name__, original.__qualname__, original.__module__
        monkeypatch.setattr(workflow, name, fail)
    output = environment_workflow('negotiation')
    assert output['status'] == 'available'
    assert output == environment_workflow('negotiation')
    graphs = output['workflow']['graphs']
    assert len(graphs) == 5
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
        task = asyncio.create_task(engine.run_game())
        for _ in range(1000):
            if human._pending is not None:
                break
            await asyncio.sleep(.001)
        assert human._loop is asyncio.get_running_loop()
        human.submit_input('{}')
        result = await asyncio.wait_for(task, 5)
        assert result['num_rounds'] == 1
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
