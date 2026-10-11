"""Agent-owned backend history, parsers, provider retries and optional behavior."""

import asyncio
import threading

from .backend.agents import HeuristicAgent, make_agent
from .backend.workflow import local_operation
from .remote import ApiMetadata, CandidateOutput, OPERATIONS


class NegotiationStrategy:
    def __init__(self, *, agent=None, config=None):
        self.agent = agent if agent is not None else (make_agent(config) if config else HeuristicAgent())
        self.model_lock = threading.Lock()
        self.telemetry = {}

    async def calls(self, invocation):
        operation = invocation.operation
        if operation in {'register', 'episode_end'}:
            return []
        if operation not in OPERATIONS:
            raise ValueError('unsupported Negotiation operation')
        if not self.model_lock.acquire(blocking=False):
            raise RuntimeError('backend invocation still active')

        # A synchronous provider may leave an executor thread alive on cancellation.
        # Own the whole backend call in a worker so its history cannot overlap a new turn.
        def execute():
            try:
                async def call():
                    started = False
                    async def stream_start():
                        nonlocal started
                        started = True
                    if hasattr(self.agent, '_on_stream_start'):
                        self.agent._on_stream_start = stream_start
                    if operation == 'notify_round_result':
                        self.agent.last_thinking = self.agent.last_reasoning = ''
                    result = await local_operation(self.agent, operation, invocation.observation)
                    payload = dict(result)
                    if operation in {'cheap_talk', 'decide_allocation'}:
                        payload.update(thinking=getattr(self.agent, 'last_thinking', ''),
                            speech=getattr(self.agent, 'last_speech', ''), reasoning=getattr(self.agent, 'last_reasoning', ''),
                            stream_started=started)
                        meta = {k: v for k, v in getattr(self.agent, 'last_api_meta', {}).items()
                                if k in ApiMetadata.model_fields and v is not None}
                        if 'error_message' in meta:
                            meta['error_message'] = 'provider request failed'
                        payload['api_meta'] = meta
                        # The environment consumes these transient fields after talk,
                        # and only after the final regular validation attempt.
                        if operation == 'cheap_talk':
                            self.agent.last_thinking = self.agent.last_reasoning = ''
                    return CandidateOutput(**payload).model_dump(exclude_none=True)
                return asyncio.run(call())
            finally:
                self.model_lock.release()

        result = await asyncio.to_thread(execute)
        return [('env', 'submit_candidate', result)]
