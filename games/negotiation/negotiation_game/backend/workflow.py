"""Negotiation's executable backend, including bounded attempts and lifecycle."""

import json
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from a2a_engine.workflow_runtime import AgentStep, GraphProgram, require_agent_step
from .defaults import MAX_DECISION_RETRIES
from .engine import GameMode


class GameState(TypedDict, total=False):
    round_number: int
    turn: int
    actor: str
    transcript: list
    alloc_a: dict | None
    alloc_b: dict | None
    proj_runs_a: dict | None
    proj_runs_b: dict | None
    project_update_a: str | None
    project_update_b: str | None
    pub_config_a: dict
    pub_config_b: dict
    round_num_a: int
    round_num_b: int
    message: str
    candidate: dict
    valid: bool
    error: str
    feedback: str
    attempt: int
    total_demanded: dict
    overdrawn: bool
    reward_a: float
    reward_b: float
    proj_details_a: dict | None
    proj_details_b: dict | None
    result: object
    summary: dict
    theoretical_sum: float | None
    theoretical_final: float | None
    reflections: dict


@dataclass
class GameContext:
    engine: object


def select_a(state, context):
    return context.engine.agent_a_impl


def select_b(state, context):
    return context.engine.agent_b_impl


def select_deciding_actor(state, context):
    return select_a(state, context) if state['actor'] == 'agent_a' else select_b(state, context)


def supports(actor, method):
    from ..remote import RemoteNegotiator
    return method in actor.supported if isinstance(actor, RemoteNegotiator) else hasattr(actor, method)


def no_observation(state, context):
    return {}


def for_seat(observe, label):
    def observe_seat(state, context):
        return observe({**state, 'actor': label}, context)
    return observe_seat


def actor_observation(state, context):
    label = state['actor']
    suffix = label[-1]
    engine = context.engine
    return {'agent_id': label, 'round_number': state.get('round_num_' + suffix, state['round_number']),
            'game_config_public': state.get('pub_config_' + suffix, engine._public_config(label)),
            'conversation': state.get('transcript', []),
            'own_memory': getattr(engine, label + '_state').memory,
            'turn_number': state.get('turn', 0),
            'project_update': state.get('project_update_' + suffix) if state.get('turn', 0) == 0 else None,
            'feedback': state.get('feedback', '')}


async def invoke(actor, operation, observation, state, context):
    require_agent_step(actor, operation)
    from ..remote import RemoteNegotiator
    if isinstance(actor, RemoteNegotiator):
        response = await actor.invoke(operation, observation)
    else:
        response = await local_operation(actor, operation, observation)
    if operation == 'initialize_session':
        prompt = response.get('system_prompt')
        if prompt:
            await context.engine._emit('system_prompt', {'agent': observation['agent_id'], 'prompt': prompt})
    elif operation == 'cheap_talk':
        return {'message': response['value']}
    elif operation == 'decide_allocation':
        return {'candidate': response['value']}
    elif operation == 'reflect':
        text = response['value']
        if text:
            reflections = {**state['reflections'], observation['agent_id']: text}
            await context.engine._emit('agent_reflection', {
                'agent': observation['agent_id'], 'reflection': text,
                'cumulative_reward': observation['own_cumulative_reward'],
                'theoretical_joint_max': observation['theoretical_joint_max']})
            return {'reflections': reflections}
    return {}


async def local_operation(actor, operation, info):
    if operation == 'initialize_session':
        if hasattr(actor, '_init_session'):
            actor._init_session(info['agent_id'], info['game_config_public'])
        return {'system_prompt': getattr(actor, '_system_prompt', None),
                'supported': [name for name in ('reset_context', '_append_user', 'notify_round_result', 'reflect')
                              if hasattr(actor, name)]}
    if operation == 'reset_context':
        if hasattr(actor, 'reset_context'):
            actor.reset_context()
    elif operation in {'error_feedback', 'project_update'}:
        if hasattr(actor, '_append_user'):
            actor._append_user(info['feedback'])
    elif operation == 'notify_round_result':
        if hasattr(actor, 'notify_round_result'):
            actor.notify_round_result(*(info[key] for key in ('round_number', 'own_allocation', 'own_reward',
                'opponent_allocation', 'opponent_reward', 'overdrawn', 'visible_opponent_reward', 'project_details')))
    elif operation == 'cheap_talk':
        return {'value': await actor.cheap_talk(info['agent_id'], info['round_number'], info['turn_number'],
                info['game_config_public'], info['conversation'], info['own_memory'],
                project_update=info['project_update'])}
    elif operation == 'decide_allocation':
        return {'value': await actor.decide_allocation(info['agent_id'], info['round_number'],
                info['game_config_public'], info['conversation'], info['own_memory'])}
    elif operation == 'reflect':
        return {'value': await actor.reflect(*(info[key] for key in ('agent_id', 'total_rounds',
            'own_cumulative_reward', 'opponent_cumulative_reward', 'visible_opponent_reward', 'theoretical_joint_max')))}
    return {}


async def choose_a(state: GameState):
    return {'actor': 'agent_a'}


async def choose_b(state: GameState):
    return {'actor': 'agent_b'}


async def next_round(state: GameState):
    return {}


def round_destination(state: GameState, runtime: Runtime[GameContext]):
    engine = runtime.context.engine
    if state['round_number'] > engine.config.num_rounds:
        return 'exhausted'
    return 'stopped' if engine._stopped else 'continue'


async def stopped(state: GameState, runtime: Runtime[GameContext]):
    await runtime.context.engine._emit('game_stopped', {'reason': 'stopped by user', 'after_round': state['round_number'] - 1})
    return {}


def reset_destination(state: GameState, runtime: Runtime[GameContext]):
    engine = runtime.context.engine
    shifted = engine._shifting_a if state['actor'] == 'agent_a' else engine._shifting_b
    return 'reset' if engine.config.mode == GameMode.SHIFTING and state['round_number'] > 1 and shifted else 'bypass'


async def record_reset(state: GameState, runtime: Runtime[GameContext]):
    await runtime.context.engine._emit('context_reset', {'round': state['round_number'], 'agent': state['actor']})
    return {}


async def prepare_round(state: GameState, runtime: Runtime[GameContext]):
    updates = await runtime.context.engine._prepare_round(state['round_number'])
    return {**updates, 'transcript': [], 'alloc_a': None, 'alloc_b': None,
            'proj_runs_a': None, 'proj_runs_b': None, 'turn': 0, 'feedback': ''}


def talk_enabled(state: GameState, runtime: Runtime[GameContext]):
    return 'enabled' if runtime.context.engine.config.enable_cheap_talk else 'disabled'


async def start_talk(state: GameState, runtime: Runtime[GameContext]):
    await runtime.context.engine._emit('phase_start', {'round': state['round_number'], 'phase': 'cheap_talk'})
    return {}


def talk_destination(state: GameState, runtime: Runtime[GameContext]):
    if state['alloc_a'] is not None and state['alloc_b'] is not None:
        return 'both_decided'
    turns = runtime.context.engine.config.cheap_talk_turns
    return 'continue' if state['turn'] < (turns if turns > 0 else 1000) else 'cap'


def unresolved(state: GameState):
    return 'invoke' if state['alloc_' + state['actor'][-1]] is None else 'bypass_decided'


def pass_destination(state: GameState, runtime: Runtime[GameContext]):
    destination = talk_destination(state, runtime)
    return unresolved(state) if destination == 'continue' else destination


async def prepare_talk(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    actor = select_deciding_actor(state, runtime.context)
    transcript = list(state['transcript'])
    other = 'b' if label == 'agent_a' else 'a'
    if state['alloc_' + other] is not None:
        notice = {'speaker': 'system', 'type': 'system', 'turn': state['turn'],
                  'message': f'Agent {other.upper()} has already made their final selection. Please submit your purchase as a JSON object.'}
        transcript.append(notice)
        await engine._emit('cheap_talk', {'round': state['round_number'], **notice})
    if engine._is_human(actor):
        await engine._emit('waiting_for_human', {'agent': label, 'round': state['round_number'],
            'turn': state['turn'], 'phase': 'cheap_talk', 'public_config': state['pub_config_' + label[-1]]})
    engine._set_stream_callback(actor, label, state['round_number'], 'cheap_talk', state['turn'])
    return {'transcript': transcript}


async def parse_talk(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    actor = select_deciding_actor(state, runtime.context)
    transcript = list(state['transcript'])
    await engine._emit_api_failure(actor, label, state['round_number'], 'cheap_talk', state['turn'])
    await engine._emit_thinking(actor, label, state['round_number'], transcript, state['turn'])
    await engine._emit_reasoning(actor, label, state['round_number'], transcript, state['turn'])
    error, feedback, updates = '', '', {}
    try:
        alloc, projects = engine._try_parse_purchase(state['message'])
        if alloc is not None:
            alloc = await engine._auto_fill_resources(alloc, projects, label)
            valid, error = engine._validate_allocation(alloc, label)
            if valid:
                updates = {'alloc_' + label[-1]: alloc, 'proj_runs_' + label[-1]: projects}
            else:
                feedback = f'Invalid purchase: {error}. Please fix and resubmit as JSON.'
    except ValueError:
        error = 'malformed JSON'
        feedback = 'Your response looked like JSON but could not be parsed. Please send either a plain text message or a valid JSON purchase.'
    return {**updates, 'transcript': transcript, 'error': error, 'feedback': feedback}


def needs_feedback(state: GameState):
    return 'feedback' if state['feedback'] else 'record'


async def record_talk(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    actor = select_deciding_actor(state, runtime.context)
    transcript = list(state['transcript'])
    alloc, projects = state['alloc_' + label[-1]], state['proj_runs_' + label[-1]]
    if state['error']:
        await engine._emit('validation_error', {'agent': label, 'error': state['error']})
    if alloc is not None:
        speech = getattr(actor, 'last_speech', '')
        if speech:
            entry = {'speaker': label, 'type': 'speech', 'turn': state['turn'], 'message': speech}
            transcript.append(entry)
            await engine._emit('cheap_talk', {'round': state['round_number'], **entry})
        transcript.append({'speaker': label, 'type': 'decision', 'turn': state['turn'], 'message': f'[DECISION] {json.dumps(alloc)}'})
        event = {'round': state['round_number'], 'agent': label, 'allocation': alloc}
        if projects is not None:
            event['project_runs'] = projects
        await engine._emit('early_decision', event)
    else:
        entry = {'speaker': label, 'type': 'speech', 'turn': state['turn'], 'message': state['message']}
        transcript.append(entry)
        await engine._emit('cheap_talk', {'round': state['round_number'], **entry})
    return {'transcript': transcript}


def after_talk(state: GameState):
    if state['alloc_a'] is not None and state['alloc_b'] is not None:
        return 'both_decided'
    return 'next_b' if state['actor'] == 'agent_a' else 'advance'


async def advance_turn(state: GameState):
    return {'turn': state['turn'] + 1}


async def start_decision(state: GameState, runtime: Runtime[GameContext]):
    engine = runtime.context.engine
    if not engine.config.enable_cheap_talk or state['alloc_a'] is None or state['alloc_b'] is None:
        await engine._emit('phase_start', {'round': state['round_number'], 'phase': 'decision'})
    return {'actor': 'agent_a', 'feedback': state['project_update_a'] or ''}


async def project_b(state: GameState):
    return {'actor': 'agent_b', 'feedback': state['project_update_b'] or ''}


def project_destination(state: GameState, runtime: Runtime[GameContext]):
    return 'update' if not runtime.context.engine.config.enable_cheap_talk and state['feedback'] else 'bypass'


async def prepare_decision(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    actor = select_deciding_actor(state, runtime.context)
    if engine._is_human(actor):
        await engine._emit('waiting_for_human', {'agent': label, 'round': state['round_number'],
            'phase': 'decision', 'public_config': state['pub_config_' + label[-1]]})
    engine._set_stream_callback(actor, label, state['round_number'], 'decision')
    return {'attempt': 0}


async def validate_decision(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    alloc = dict(state['candidate'])
    projects = None
    if 'projects' in alloc:
        raw = alloc.pop('projects')
        if isinstance(raw, dict):
            projects = {k: int(v) for k, v in raw.items() if int(v) > 0}
    alloc = await engine._auto_fill_resources(alloc, projects, label)
    valid, error = engine._validate_allocation(alloc, label)
    return {'valid': valid, 'error': error, 'attempt': state['attempt'] + 1,
            'alloc_' + label[-1]: alloc, 'proj_runs_' + label[-1]: projects,
            'feedback': f'Invalid decision: {error}. Please try again with a valid JSON purchase.'}


def validation_destination(state: GameState):
    return 'valid' if state['valid'] else 'invalid'


async def record_invalid(state: GameState, runtime: Runtime[GameContext]):
    await runtime.context.engine._emit('validation_error', {'agent': state['actor'], 'error': state['error'], 'attempt': state['attempt']})
    return {}


def attempt_destination(state: GameState):
    return 'retry' if state['attempt'] < MAX_DECISION_RETRIES else 'exhausted'


async def exhaust_decision(state: GameState, runtime: Runtime[GameContext]):
    label = state['actor']
    await runtime.context.engine._emit('validation_error', {'agent': label,
        'error': f'{label} failed to submit a valid decision after {MAX_DECISION_RETRIES} attempts'})
    return {'alloc_' + label[-1]: {}, 'proj_runs_' + label[-1]: None}


async def record_decision(state: GameState, runtime: Runtime[GameContext]):
    engine, label = runtime.context.engine, state['actor']
    actor = select_deciding_actor(state, runtime.context)
    transcript = list(state['transcript'])
    await engine._emit_api_failure(actor, label, state['round_number'], 'decision')
    await engine._emit_thinking(actor, label, state['round_number'], transcript)
    await engine._emit_reasoning(actor, label, state['round_number'], transcript)
    return {'transcript': transcript}


async def resolve_round(state: GameState, runtime: Runtime[GameContext]):
    return await runtime.context.engine._resolve_round(*(state[key] for key in
        ('round_number', 'alloc_a', 'alloc_b', 'proj_runs_a', 'proj_runs_b', 'transcript')))


def notification_observation(state, context):
    suffix = state['actor'][-1]
    other = 'b' if suffix == 'a' else 'a'
    return dict(round_number=state['round_num_' + suffix], own_allocation=state['alloc_' + suffix],
                own_reward=state['reward_' + suffix], opponent_allocation=state['alloc_' + other],
                opponent_reward=state['reward_' + other], overdrawn=state['overdrawn'],
                visible_opponent_reward=context.engine.config.visible_opponent_reward,
                project_details=state['proj_details_' + suffix])


async def complete_round(state: GameState, runtime: Runtime[GameContext]):
    result = await runtime.context.engine._complete_round(*(state[key] for key in
        ('round_number', 'alloc_a', 'alloc_b', 'transcript', 'total_demanded', 'overdrawn',
         'reward_a', 'reward_b', 'proj_details_a', 'proj_details_b')))
    return {'result': result, 'round_number': state['round_number'] + 1}


async def build_summary(state: GameState, runtime: Runtime[GameContext]):
    summary, total, final = runtime.context.engine._build_summary()
    return {'summary': summary, 'theoretical_sum': total, 'theoretical_final': final}


def reflection_destination(state: GameState, runtime: Runtime[GameContext]):
    engine = runtime.context.engine
    return 'reflect' if engine.config.enable_cheap_talk and supports(select_deciding_actor(state, runtime.context), 'reflect') else 'bypass'


def reflection_observation(state, context):
    engine, label = context.engine, state['actor']
    suffix, other = label[-1], 'b' if label == 'agent_a' else 'a'
    shifted = engine._shifting_a if suffix == 'a' else engine._shifting_b
    own = getattr(engine.results[-1], 'agent_' + suffix + '_reward') if shifted else getattr(engine, label + '_state').cumulative_reward
    opponent = getattr(engine.results[-1], 'agent_' + other + '_reward') if shifted else getattr(engine, 'agent_' + other + '_state').cumulative_reward
    return dict(agent_id=label, total_rounds=1 if shifted else len(engine.results),
                own_cumulative_reward=own, opponent_cumulative_reward=opponent,
                visible_opponent_reward=engine.config.visible_opponent_reward,
                theoretical_joint_max=state['theoretical_final'] if shifted else state['theoretical_sum'])


async def complete_game(state: GameState, runtime: Runtime[GameContext]):
    summary = dict(state['summary'])
    if state['reflections']:
        summary['reflections'] = state['reflections']
    await runtime.context.engine._emit('game_complete', summary)
    return {'summary': summary}


def graph():
    return StateGraph(GameState, context_schema=GameContext)


def domain(builder, *functions):
    for fn in functions:
        builder.add_node(fn.__name__, fn)


def chain(builder, *names):
    for source, target in zip(names, names[1:]):
        builder.add_edge(source, target)


def cheap_talk_program():
    builder = graph()
    domain(builder, start_talk, choose_b, prepare_talk, parse_talk, record_talk, advance_turn)
    builder.add_node('next_pass', choose_a)
    builder.add_node('talk_call', AgentStep(select_deciding_actor, 'cheap_talk', actor_observation, invoke))
    builder.add_node('error_feedback', AgentStep(select_deciding_actor, 'error_feedback', actor_observation, invoke))
    chain(builder, START, 'start_talk', 'next_pass')
    builder.add_conditional_edges('next_pass', pass_destination, {
        'both_decided': END, 'cap': END, 'invoke': 'prepare_talk', 'bypass_decided': 'choose_b'})
    builder.add_conditional_edges('choose_b', unresolved, {'invoke': 'prepare_talk', 'bypass_decided': 'advance_turn'})
    chain(builder, 'prepare_talk', 'talk_call', 'parse_talk')
    builder.add_conditional_edges('parse_talk', needs_feedback, {'feedback': 'error_feedback', 'record': 'record_talk'})
    chain(builder, 'error_feedback', 'record_talk')
    builder.add_conditional_edges('record_talk', after_talk, {'both_decided': END, 'next_b': 'choose_b', 'advance': 'advance_turn'})
    chain(builder, 'advance_turn', 'next_pass')
    return GraphProgram(builder)


def decision_program(chooser):
    builder = graph()
    domain(builder, prepare_decision, validate_decision, record_invalid, exhaust_decision, record_decision)
    builder.add_node('begin', chooser)
    builder.add_node('allocation_call', AgentStep(select_deciding_actor, 'decide_allocation', actor_observation, invoke))
    builder.add_node('error_feedback', AgentStep(select_deciding_actor, 'error_feedback', actor_observation, invoke))
    chain(builder, START, 'begin')
    builder.add_conditional_edges('begin', unresolved, {'invoke': 'prepare_decision', 'bypass_decided': END})
    chain(builder, 'prepare_decision', 'allocation_call', 'validate_decision')
    builder.add_conditional_edges('validate_decision', validation_destination, {'valid': 'record_decision', 'invalid': 'record_invalid'})
    chain(builder, 'record_invalid', 'error_feedback')
    builder.add_conditional_edges('error_feedback', attempt_destination, {'retry': 'allocation_call', 'exhausted': 'exhaust_decision'})
    chain(builder, 'exhaust_decision', 'record_decision', END)
    return GraphProgram(builder)


def round_program():
    builder = graph()
    domain(builder, prepare_round, start_decision, project_b, resolve_round, complete_round)
    builder.add_node('cheap_talk', cheap_talk_program().compiled)
    builder.add_node('decision_a', decision_program(choose_a).compiled)
    builder.add_node('decision_b', decision_program(choose_b).compiled)
    for suffix, selector in (('a', select_a), ('b', select_b)):
        builder.add_node('update_' + suffix, AgentStep(selector, 'project_update', actor_observation, invoke))
        builder.add_node('notify_' + suffix, AgentStep(selector, 'notify_round_result',
            for_seat(notification_observation, 'agent_' + suffix), invoke))
    chain(builder, START, 'prepare_round')
    builder.add_conditional_edges('prepare_round', talk_enabled, {'enabled': 'cheap_talk', 'disabled': 'start_decision'})
    chain(builder, 'cheap_talk', 'start_decision')
    builder.add_conditional_edges('start_decision', project_destination, {'update': 'update_a', 'bypass': 'project_b'})
    chain(builder, 'update_a', 'project_b')
    builder.add_conditional_edges('project_b', project_destination, {'update': 'update_b', 'bypass': 'decision_a'})
    chain(builder, 'update_b', 'decision_a', 'decision_b', 'resolve_round', 'notify_a', 'notify_b', 'complete_round', END)
    return GraphProgram(builder)


def reset_program():
    builder = graph()
    for suffix, selector, chooser in (('a', select_a, choose_a), ('b', select_b, choose_b)):
        builder.add_node('reset_guard_' + suffix, chooser)
        builder.add_node('reset_' + suffix, AgentStep(selector, 'reset_context', no_observation, invoke))
        builder.add_node('record_reset_' + suffix, record_reset)
    chain(builder, START, 'reset_guard_a')
    for suffix, target in (('a', 'reset_guard_b'), ('b', END)):
        builder.add_conditional_edges('reset_guard_' + suffix, reset_destination, {'reset': 'reset_' + suffix, 'bypass': target})
        chain(builder, 'reset_' + suffix, 'record_reset_' + suffix, target)
    return GraphProgram(builder)


def reflections_program():
    builder = graph()
    for suffix, selector, chooser in (('a', select_a, choose_a), ('b', select_b, choose_b)):
        builder.add_node('reflect_guard_' + suffix, chooser)
        builder.add_node('reflect_' + suffix, AgentStep(selector, 'reflect', reflection_observation, invoke))
    chain(builder, START, 'reflect_guard_a')
    for suffix, target in (('a', 'reflect_guard_b'), ('b', END)):
        builder.add_conditional_edges('reflect_guard_' + suffix, reflection_destination, {'reflect': 'reflect_' + suffix, 'bypass': target})
        chain(builder, 'reflect_' + suffix, target)
    return GraphProgram(builder)


def build_program():
    builder = graph()
    domain(builder, next_round, stopped, complete_game)
    builder.add_node('summarize', build_summary)
    builder.add_node('reset_contexts', reset_program().compiled)
    builder.add_node('round', round_program().compiled)
    builder.add_node('reflections', reflections_program().compiled)
    for suffix, selector in (('a', select_a), ('b', select_b)):
        builder.add_node('open_session_' + suffix, AgentStep(selector, 'initialize_session',
            for_seat(actor_observation, 'agent_' + suffix), invoke))
    chain(builder, START, 'open_session_a', 'open_session_b', 'next_round')
    builder.add_conditional_edges('next_round', round_destination, {'continue': 'reset_contexts', 'stopped': 'stopped', 'exhausted': 'summarize'})
    chain(builder, 'reset_contexts', 'round', 'next_round')
    chain(builder, 'stopped', 'summarize', 'reflections', 'complete_game')
    chain(builder, 'complete_game', END)
    return GraphProgram(builder)


def execution_limit(config):
    # Upper bound inherited by each nested graph's local loop. Two actors take
    # at most eight steps each per talk pass; decisions have three calls.
    turns = config.cheap_talk_turns if config.cheap_talk_turns > 0 else 1000
    return max(100, 100 + max(0, config.num_rounds) * (100 + 20 * turns if config.enable_cheap_talk else 100))
