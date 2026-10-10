"""Controlled real backend agents shared by loopback and Docker verification."""

import hashlib
import json
import random
from copy import deepcopy
from types import FunctionType

from a2a_engine.llm.retry import RateLimiter, RetryPolicy
from negotiation_game.backend.agents.heuristic import HeuristicAgent
from negotiation_game.backend.agents.llm import LLMAgentBase

CONFIG = {'environment_id': 'negotiation', 'num_rounds': 2, 'cheap_talk_turns': 2,
          'first_speaker': 1, 'mode': 'shifting', 'agent_shifting': [True, True],
          'rotate_projects': True, 'episode_uid': 'controlled', 'seed': 17, 'seed_mode': 'static',
          'resource_types': ['wood', 'stone'], 'resource_costs': {'wood': 1., 'stone': 1.},
          'resource_supply': {'wood': 6, 'stone': 6}, 'agent_budget': 5,
          'agent_projects': [[{'name': 'alpha', 'requirements': {'wood': 1}, 'reward': 7}],
                             [{'name': 'beta', 'requirements': {'stone': 1}, 'reward': 9}]]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SeededHeuristic(HeuristicAgent):
    def __init__(self, seat):
        super().__init__()
        # Freeze only the test oracle's randomness, not production scheduling.
        rng = random.Random(70 + seat)
        for name in ('cheap_talk', 'decide_allocation'):
            fn = getattr(HeuristicAgent, name)
            bound = FunctionType(fn.__code__, {**fn.__globals__, 'random': rng}, name,
                                 fn.__defaults__, fn.__closure__)
            setattr(self, name, bound.__get__(self))


class MockProvider(LLMAgentBase):
    def __init__(self, talk=True):
        super().__init__(model='controlled')
        self._retry_policy = RetryPolicy(max_attempts=2, backoff_base=0, backoff_max=0, request_cooldown=0)
        self._limiter = RateLimiter(0)
        self.messages = []
        self.index = 0
        self.talk = talk

    async def _call_api(self):
        self.messages.append(deepcopy(self._messages))
        if self._on_stream_start:
            await self._on_stream_start()
        index = self.index
        self.index += 1
        if not self.talk:
            index = 2 + index % 2
        if index >= 8:
            response = {'thinking': 'reflection thinking', 'speech': 'reflection speech'}
        elif index % 4 == 0:
            response = {'thinking': 'talk thinking', 'speech': 'invalid offer', 'action': {'wood': 999}}
        elif index % 4 == 1:
            response = {'thinking': 'talk thinking', 'speech': '{broken'}
        elif index % 4 == 2:
            response = {'thinking': 'decision thinking', 'action': {'wood': 999}}
        else:
            project = 'alpha' if self._agent_id == 'agent_b' else 'beta'
            response = {'thinking': 'decision thinking', 'action': {'projects': {project: 1}}}
        return {'text': json.dumps(response), 'model': 'controlled', 'duration_s': 0.,
                'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14, 'reasoning': 'reasoning'}


def make_controlled(mode, seat, talk=True):
    return MockProvider(talk) if mode == 'mock' else SeededHeuristic(seat)
