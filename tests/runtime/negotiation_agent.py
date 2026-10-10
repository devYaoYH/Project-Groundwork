"""Controlled backend policies; hold one submitted candidate before completion."""

import asyncio
import os
from copy import deepcopy

from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_agent import service
from a2a_agent.mcp_client import execute
from negotiation_game.runtime_strategy import NegotiationStrategy
from negotiation_fixture import digest, make_controlled


async def watch(runtime, directory, callback_url):
    seen, audits = set(), []
    async def audit(request):
        return JSONResponse(audits)
    runtime.app.routes.append(Route('/audit', audit))

    class ControlledStrategy(NegotiationStrategy):
        def __init__(self, seat):
            super().__init__(agent=make_controlled(os.environ['NEGOTIATION_MODE'], seat))
            self.observations, self.values = [], []

        async def calls(self, invocation):
            calls = await super().calls(invocation)
            if invocation.kind == 'turn':
                self.observations.append((invocation.operation, deepcopy(invocation.observation)))
                self.values.append((invocation.operation, calls[0][2].get('value')))
                if invocation.seat == 1 and invocation.operation == 'cheap_talk' and len(self.observations) == 2:
                    outcomes = await execute(invocation, calls, network=runtime.network)
                    assert outcomes[0]['status'] == 'accepted'
                    while not (directory / 'release').exists():
                        await asyncio.sleep(.02)
                    return []
            if invocation.kind == 'episode_end':
                audits.append({'seat': invocation.seat, 'observations': digest(self.observations),
                               'values': digest(self.values), 'messages': digest(getattr(self.agent, 'messages', [])),
                               'history': digest(getattr(self.agent, '_messages', []))})
            return calls

    while not runtime.stopping:
        existing = set(directory.glob('*.json'))
        seen.intersection_update(existing)
        for path in sorted(existing - seen):
            seen.add(path)
            descriptor = service.read_descriptor(path)
            assert descriptor['environment_id'] == 'negotiation'
            await runtime.join(descriptor['join_url'], callback_url, descriptor['join_ticket'],
                               policy=ControlledStrategy(descriptor['seat']))
        runtime._expire()
        await asyncio.sleep(.05)


if __name__ == '__main__':
    service.watch = watch
    service.main()
