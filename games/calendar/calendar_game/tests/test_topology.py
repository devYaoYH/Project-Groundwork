from copy import deepcopy
import json

import pytest

from a2a_engine.manifest import config_hash
from calendar_game.agents import Agent, BaseClient, DecideResult, TurnResult
from calendar_game.calendar import Calendar
from calendar_game.clients.llm import LLMClient
from calendar_game.game import CalendarGame
from calendar_game.observability import InstrumentedCalendarClient


class PolicyClient(BaseClient):
    def __init__(self, cheap=(), voluntary=(), decision=(), retry=(), invalid_decision=False):
        self.cheap = list(cheap)
        self.voluntary = list(voluntary)
        self.decision = list(decision)
        self.retry = list(retry)
        self.invalid_decision = invalid_decision
        self.received = []
        self.phase_inboxes = []
        self.voluntary_calls = 0

    def register(self, agent_id, game_config):
        self.agent_id = agent_id
        self.game_config = game_config

    def start_round(self, meeting, calendar_render, round_num):
        self.meeting = meeting
        self.sent = False

    def turn(self, messages, turn_index=None, max_turns_per_round=None):
        self.received.extend(messages)
        calls = [] if self.sent else self.cheap
        self.sent = True
        return TurnResult(list(calls), None, None, None, None, None)

    def observe_messages(self, messages):
        self.phase_inboxes.append(list(messages))

    def decide(self, meeting, calendar_render):
        slot = 99 if self.invalid_decision else meeting['id'] - 1
        return DecideResult([*self.decision, {'type': 'schedule', 'meeting_id': meeting['id'], 'slot': slot}], None, None, None, None, None)

    def voluntary_decide(self, meeting, calendar_render):
        self.voluntary_calls += 1
        return DecideResult(list(self.voluntary), None, None, None, None, None)

    def retry_decide(self, attempt, max_attempts, conflict):
        return DecideResult([*self.retry, {'type': 'schedule', 'meeting_id': self.meeting['id'], 'slot': self.meeting['id'] - 1}], None, None, None, None, None)


def run_episode(clients, topology=None, participants=None, meetings=1, **kwargs):
    participants = [0, 1] if participants is None else participants
    config = {
        'seed': 1, 'num_agents': len(clients), 'num_slots': 4, 'num_meetings': meetings,
        'max_turns_per_round': 2, 'decision_retries': 0, 'enable_fallback': False, 'enable_reflection': False,
        **kwargs,
    }
    if topology is not None:
        config['communication'] = {'topology': topology}
    game = CalendarGame(config, dry_run=True)
    scenario = {
        'seed': 1, 'num_agents': len(clients), 'num_slots': 4,
        'calendars': [[None] * 4 for _ in clients],
        'meetings': [{'id': i + 1, 'participants': participants, 'speaker_order': participants, 'duration': 1, 'cost': 1} for i in range(meetings)],
        'prior_meetings': [],
    }
    agents = []
    for client in clients:
        agent = Agent(client)
        agent.calendar = Calendar(4)
        agents.append(agent)
    return game._run_with_agents(agents, scenario), agents


def data(trace, kind):
    return [event.data for event in trace.events if event.type == kind]


@pytest.mark.parametrize('graph, extra, recipients', [
    ('complete', {}, [1, 2, 3]), ('ring', {}, [1, 3]), ('star', {'hub': 2}, [2]),
    ('edges', {'edges': [[0, 3], [0, 2]], 'directed': True}, [2, 3]),
    ('edges', {'edges': [[3, 0], [2, 0]], 'directed': False}, [2, 3]),
])
def test_real_episode_graph_recipients_activation_and_next_inbox(graph, extra, recipients):
    clients = [PolicyClient(cheap=[{'type': 'all_groupchat', 'content': 'proposal'}]), *[PolicyClient() for _ in range(3)]]
    trace, _ = run_episode(clients, {'default': {'graph': graph, **extra, 'channels': {'all_groupchat': {}}}})
    assert data(trace, 'all_groupchat_sent')[0]['to_agents'] == recipients
    for seat in range(1, 4):
        assert bool(clients[seat].received) == (seat in recipients)
        assert clients[seat].voluntary_calls == int(seat not in [0, 1] and seat in recipients)
    assert trace.metrics['meetings_scheduled'] == 1
    assert trace.final_state['per_agent_messages_received'] == [0, *[int(i in recipients) for i in range(1, 4)]]


@pytest.mark.parametrize('graph, extra, recipients', [
    ('complete', {}, [2, 1]), ('ring', {}, [1]), ('star', {'hub': 2}, [2]),
    ('edges', {'edges': [[0, 1], [0, 2]], 'directed': True}, [2, 1]),
])
def test_participant_chat_uses_meeting_order_and_never_activates_nonmembers(graph, extra, recipients):
    clients = [PolicyClient(cheap=[{'type': 'participant_groupchat', 'content': 'participants'}]), *[PolicyClient() for _ in range(3)]]
    trace, _ = run_episode(clients, {'default': {'graph': graph, **extra, 'channels': {'participant_groupchat': {}}}}, participants=[0, 2, 1])
    assert data(trace, 'participant_groupchat_sent')[0]['to_agents'] == recipients
    assert clients[3].received == [] and clients[3].voluntary_calls == 0


@pytest.mark.parametrize('channels', [{}, {'dm': {'enabled': False}}])
def test_disabled_channels_silence_and_empty_prompt(channels):
    trace, _ = run_episode([PolicyClient(cheap=[{'type': 'dm', 'to': 1}]), PolicyClient()], {'default': {'channels': channels}})
    assert data(trace, 'dm_sent') == []
    assert data(trace, 'invalid_tool_call')[0]['reason'] == 'dm tool is disabled by topology in CHEAP_TALK'
    assert 'Communication is disabled' in data(trace, 'turn_start')[0]['prompt_sent']
    assert '"type": "dm"' not in data(trace, 'agent_registered')[0]['system_prompt']


@pytest.mark.parametrize('enabled', [False, True])
def test_decision_phase_override_delivery_at_turn_end_and_cell_separation(enabled):
    topology = {'default': {'channels': {'dm': {}}}}
    if enabled:
        topology['phases'] = {'DECISION': {}}
    clients = [PolicyClient(decision=[{'type': 'dm', 'to': 1, 'content': 'decision-message'}]), PolicyClient()]
    trace, _ = run_episode(clients, topology)
    sends = data(trace, 'dm_sent')
    assert len(sends) == int(enabled)
    if enabled:
        assert sends[0]['phase'] == 'DECISION'
        start = data(trace, 'decide_start')[1]
        assert start['inbox_drained'][0]['content'] == 'decision-message'
        assert 'decision-message' in start['prompt_sent']
        assert clients[1].phase_inboxes[-1][0]['content'] == 'decision-message'
        kinds = [event.type for event in trace.events]
        assert kinds.index('decide_end') < kinds.index('dm_sent') < kinds.index('cell_applied')
        assert 'Communication tools enabled: dm' in data(trace, 'decide_start')[0]['prompt_sent']
    else:
        assert data(trace, 'invalid_tool_call')[0]['phase'] == 'DECISION'
    assert all([action['type'] for action in cell['actions']] == ['schedule'] for cell in data(trace, 'cell_applied'))
    assert trace.metrics['meetings_scheduled'] == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_voluntary_override_and_decision_inbox(enabled):
    topology = {'default': {'channels': {'dm': {}}}, 'phases': {'VOLUNTARY': {} if enabled else {'channels': {}}}}
    clients = [PolicyClient(cheap=[{'type': 'dm', 'to': 2, 'content': 'activate'}]), PolicyClient(),
               PolicyClient(voluntary=[{'type': 'dm', 'to': 1, 'content': 'voluntary-message'}])]
    trace, _ = run_episode(clients, topology)
    assert clients[2].voluntary_calls == 1
    sends = [send for send in data(trace, 'dm_sent') if send['phase'] == 'VOLUNTARY']
    assert len(sends) == int(enabled)
    assert all(not cell['actions'] for cell in data(trace, 'cell_applied') if cell['phase'] == 'VOLUNTARY')
    inboxes = [start['inbox_drained'] for start in data(trace, 'decide_start') if start['agent_id'] == 1]
    assert bool(inboxes[0]) == enabled


def test_shared_budget_crosses_channels_and_phases_but_resets_by_round():
    topology = {'default': {'channels': {'dm': {}, 'all_groupchat': {}}, 'budget': {'per_agent_per_round': 2}},
                'phases': {'DECISION': {}}}
    clients = [PolicyClient(cheap=[{'type': 'dm', 'to': 1}, {'type': 'all_groupchat'}], decision=[{'type': 'dm', 'to': 1}]), PolicyClient()]
    trace, _ = run_episode(clients, topology, meetings=2)
    assert len(data(trace, 'dm_sent')) == 2 and len(data(trace, 'all_groupchat_sent')) == 2
    invalid = data(trace, 'invalid_tool_call')
    assert len(invalid) == 2
    assert all(call['phase'] == 'DECISION' and 'budget exhausted' in call['reason'] for call in invalid)
    assert trace.metrics['meetings_scheduled'] == 2


def test_explicit_denials_do_not_consume_budget_or_activate_blocked_neighbors():
    topology = {'default': {'graph': 'ring', 'budget': {'per_agent_per_round': 1}}}
    clients = [PolicyClient(cheap=[{'type': 'dm', 'to': 2}, {'type': 'dm', 'to': 3}]), *[PolicyClient() for _ in range(3)]]
    trace, _ = run_episode(clients, topology)
    assert [send['to_agent'] for send in data(trace, 'dm_sent')] == [3]
    assert clients[2].voluntary_calls == 0 and clients[3].voluntary_calls == 1


def test_participant_retry_uses_parent_phase_policy_and_shared_budget():
    topology = {'default': {'budget': {'per_agent_per_round': 1}}, 'phases': {'DECISION': {}}}
    clients = [PolicyClient(decision=[{'type': 'dm', 'to': 1}], retry=[{'type': 'dm', 'to': 1}], invalid_decision=True), PolicyClient()]
    trace, _ = run_episode(clients, topology, decision_retries=1)
    assert len(data(trace, 'dm_sent')) == 1
    assert 'budget exhausted' in data(trace, 'invalid_tool_call')[0]['reason']
    assert trace.metrics['meetings_scheduled'] == 1


def test_voluntary_retry_keeps_parent_policy_and_cannot_schedule():
    class VoluntaryRetry(PolicyClient):
        def retry_decide(self, attempt, max_attempts, conflict):
            return DecideResult([
                {'type': 'dm', 'to': 1, 'content': 'voluntary-retry'},
                {'type': 'schedule', 'meeting_id': 1, 'slot': 0},
            ], None, None, None, None, None)

    invalid_move = {'type': 'reschedule', 'item_id': 4, 'from_slot': 99, 'to_slot': 1, 'justification': 'invalid'}
    clients = [PolicyClient(cheap=[{'type': 'dm', 'to': 2}]), PolicyClient(), VoluntaryRetry(voluntary=[invalid_move])]
    trace, _ = run_episode(clients, {'default': {}, 'phases': {'VOLUNTARY': {}}}, decision_retries=1)
    retry_send = data(trace, 'dm_sent')[-1]
    assert retry_send['phase'] == 'VOLUNTARY' and retry_send['content'] == 'voluntary-retry'
    voluntary_cell = next(cell for cell in data(trace, 'cell_applied') if cell['phase'] == 'VOLUNTARY')
    assert voluntary_cell['actions'] == []
    assert trace.final_state['calendars'][2] == [None] * 4
    assert trace.metrics['meetings_scheduled'] == 1


@pytest.mark.parametrize('directed, sent', [(True, False), (False, True)])
def test_explicit_edge_reverse_dm_is_allowed_only_when_undirected(directed, sent):
    clients = [PolicyClient(), PolicyClient(cheap=[{'type': 'dm', 'to': 0}])]
    trace, _ = run_episode(clients, {'default': {'graph': 'edges', 'edges': [[0, 1]], 'directed': directed}})
    assert bool(data(trace, 'dm_sent')) == sent
    assert bool(clients[0].received) == sent


def test_authored_topology_remains_sparse_and_hash_stable():
    authored = {'default': {'graph': 'ring'}, 'phases': {'DECISION': {'channels': {}}}}
    before = deepcopy(authored)
    trace, _ = run_episode([PolicyClient(), PolicyClient()], authored)
    assert trace.config.communication['topology'] == before == authored
    assert config_hash({'communication': {'topology': authored}}) == config_hash({'communication': {'topology': before}})


@pytest.mark.parametrize('communication', [
    {'permissions': {'send': True}}, {'topology': {'phases': {'DECSION': {}}}},
    {'topology': {'default': {'graph': 'star', 'hub': 5}}},
    {'topology': {'default': {'edges': [[0, 3]]}}},
    {'topology': {'default': {'channels': {'bogus': {}}}}},
])
def test_bad_configuration_fails_without_client_construction_or_events(communication):
    with pytest.raises(ValueError):
        CalendarGame({'num_agents': 2, 'communication': communication})


def test_actual_instrumented_model_prompts_follow_policy_and_push_decision_messages():
    class Model:
        def __init__(self, seat):
            self.seat = seat
            self.prompts = []

        def streaming_with_retry(self, messages, **kwargs):
            self.prompts.append(messages[-1]['content'])
            if len(self.prompts) == 1:
                return {'text': '{"actions": []}'}
            actions = [{'type': 'schedule', 'meeting_id': 1, 'slot': 0}]
            if self.seat == 0:
                actions.append({'type': 'dm', 'to': 1, 'content': 'model-message'})
            import json
            return {'text': json.dumps({'actions': actions})}

    models = [Model(0), Model(1)]
    clients = [InstrumentedCalendarClient(LLMClient(model), agent_id=seat) for seat, model in enumerate(models)]
    trace, _ = run_episode(clients, {'default': {'channels': {}}, 'phases': {'DECISION': {'channels': {'dm': {}}}}})
    assert all('Communication is disabled' in model.prompts[0] for model in models)
    assert all('Communication tools enabled: dm' in model.prompts[1] for model in models)
    assert 'model-message' in models[1].prompts[1]
    assert data(trace, 'decide_start')[1]['prompt_sent'].endswith('model-message')
    assert trace.metrics['meetings_scheduled'] == 1


def test_mock_models_receive_graph_scoped_prompts_and_effective_phase_budgets():
    class Model:
        def __init__(self, seat):
            self.seat = seat
            self.prompts = []
            self.system_prompts = []

        def streaming_with_retry(self, messages, **kwargs):
            prompt = messages[-1]['content']
            self.prompts.append(prompt)
            self.system_prompts.append(messages[0]['content'])
            actions = []
            if '=== DECISION PHASE ===' in prompt:
                actions = [{'type': 'schedule', 'meeting_id': 1, 'slot': 0}]
                if self.seat == 0:
                    actions.append({'type': 'all_groupchat', 'content': 'decision-neighbors'})
            elif '=== VOLUNTARY RESCHEDULE PHASE ===' in prompt:
                actions = [{'type': 'all_groupchat', 'content': 'voluntary-neighbors'}]
            elif self.seat == 0 and len(self.prompts) == 1:
                actions = [{'type': 'all_groupchat', 'content': 'cheap-talk-neighbors'}]
            return {'text': json.dumps({'actions': actions})}

    authored = {
        'default': {'graph': 'ring', 'channels': {'all_groupchat': {}}, 'budget': {'per_agent_per_round': 5}},
        'phases': {
            'VOLUNTARY': {'graph': 'star', 'hub': 0, 'budget': {'per_agent_per_round': 3}},
            'DECISION': {'budget': {'per_agent_per_round': 2}},
        },
    }
    before = deepcopy(authored)
    models = [Model(seat) for seat in range(4)]
    clients = [InstrumentedCalendarClient(LLMClient(model), agent_id=seat) for seat, model in enumerate(models)]
    trace, _ = run_episode(clients, authored, dm_cap=999)
    assert [(send['phase'], send['to_agents']) for send in data(trace, 'all_groupchat_sent')] == [
        ('CHEAP_TALK', [1, 3]), ('VOLUNTARY', [0]), ('DECISION', [1, 3]),
    ]
    assert models[2].prompts == []
    assert any('Graph-neighbor groupchat' in prompt for prompt in models[1].prompts)
    assert 'voluntary-neighbors' in models[0].prompts[-1]
    assert 'decision-neighbors' in models[1].prompts[-1]
    for model in models:
        for prompt in model.system_prompts:
            assert 'CHEAP_TALK Messaging-tool budget per agent per meeting round: 5.' in prompt
            assert 'visible to every agent' not in prompt
            assert 'proposing to everyone' not in prompt
            assert '999' not in prompt
            assert 'runaway guard' not in prompt
        for prompt in model.prompts:
            budget = 2 if '=== DECISION PHASE ===' in prompt else 3 if '=== VOLUNTARY RESCHEDULE PHASE ===' in prompt else 5
            assert f'Messaging-tool budget per agent per meeting round: {budget}.' in prompt
            assert 'visible to graph neighbors excluding yourself, including neighboring non-participants' in prompt
            assert 'visible to every agent' not in prompt
            assert 'Usage is shared across channels and phases' in prompt
    for kind in ('turn_start', 'decide_start'):
        for start in data(trace, kind):
            assert 'visible to graph neighbors excluding yourself' in start['prompt_sent']
            assert 'visible to every agent' not in start['prompt_sent']
            assert 'Messaging-tool budget per agent per meeting round:' in start['prompt_sent']
    assert trace.config.dm_cap == 999
    assert trace.config.communication['topology'] == before == authored
    assert config_hash({'communication': {'topology': authored}}) == config_hash({'communication': {'topology': before}})
    assert trace.metrics['meetings_scheduled'] == 1
