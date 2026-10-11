"""Controlled strategy, not a live provider, with a held first MCP submission."""

import asyncio
import os

from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_agent import service
from a2a_agent.mcp_client import execute
from buyer_seller.runtime_strategy import BuyerSellerStrategy
from buyer_seller_fixture import MockProvider, digest


async def watch(runtime, directory, callback_url):
    seen, audits = set(), []

    async def audit(request):
        return JSONResponse(audits)

    runtime.app.routes.append(Route("/audit", audit))

    class ControlledStrategy(BuyerSellerStrategy):
        def __init__(self):
            super().__init__(client=MockProvider() if os.environ["BARGAIN_MODE"] == "mock" else None)
            self.actions, self.observations = [], []
            self.registration = None

        async def calls(self, invocation):
            calls = await super().calls(invocation)
            if invocation.kind == "register":
                self.registration = invocation.observation
            elif invocation.kind == "turn":
                self.observations.append(invocation.observation)
                self.actions.append(calls[0][2])
                if invocation.seat == 0 and invocation.observation["round"] == 1:
                    outcomes = await execute(invocation, calls, network=runtime.network)
                    assert outcomes[0]["status"] == "accepted"
                    while not (directory / "release").exists():
                        await asyncio.sleep(.02)
                    return []
            elif invocation.kind == "episode_end":
                opposing_key = "value" if self.role == "seller" else "cost"
                opposing_obs = "your_value" if self.role == "seller" else "your_cost"
                private = "33.27" if self.role == "seller" else "7.51"
                assert opposing_key not in self.registration
                assert all(opposing_obs not in obs for obs in self.observations)
                messages = self.client.messages if self.client else []
                assert private not in str(messages)
                audits.append({"episode_id": invocation.episode_id, "seat": invocation.seat,
                               "actions": digest(self.actions), "observations": digest(self.observations),
                               "messages": digest(messages), "private": True})
            return calls

    while not runtime.stopping:
        existing = set(directory.glob("*.json"))
        seen.intersection_update(existing)
        for path in sorted(existing - seen):
            seen.add(path)
            descriptor = service.read_descriptor(path)
            assert descriptor["environment_id"] == "buyer_seller"
            await runtime.join(descriptor["join_url"], callback_url, descriptor["join_ticket"],
                               policy=ControlledStrategy())
        runtime._expire()
        await asyncio.sleep(.05)


if __name__ == "__main__":
    service.watch = watch
    service.main()
