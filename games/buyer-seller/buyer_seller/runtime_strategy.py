"""Agent-side policy reusing bargaining's scripted agents, prompts and parsers."""

import asyncio
import threading

from a2a_engine.llm.api import LLMClient

from .agents import BuyerAgent, SellerAgent


class DeadlineClient:
    def __init__(self, client):
        self.client, self.deadline = client, None

    def oneshot(self, messages):
        return self.client.oneshot(messages, deadline=self.deadline)


class BuyerSellerStrategy:
    def __init__(self, *, client=None):
        self.client = client
        self.bounded_client = DeadlineClient(client) if isinstance(client, LLMClient) else client
        self.model_lock = threading.Lock()
        self.agent = None
        self.role = None
        self.telemetry = {}

    async def calls(self, invocation):
        if invocation.kind == "register":
            from .game import _ScriptedBuyer, _ScriptedSeller
            info = invocation.observation
            self.role = info["role"]
            common = {key: info[key] for key in ("num_items", "max_rounds", "discount_factor")}
            if self.role == "seller":
                self.agent = (SellerAgent(self.bounded_client, cost=info["cost"], monotonic=info["monotonic"], **common)
                              if self.client else _ScriptedSeller(info["cost"], info["opening"], info["max_rounds"]))
            elif self.role == "buyer":
                self.agent = (BuyerAgent(self.bounded_client, value=info["value"], **common)
                              if self.client else _ScriptedBuyer(info["value"], info["patience"], info["max_rounds"]))
            else:
                raise ValueError("unknown role")
            return []
        if invocation.kind != "turn":
            return []
        if self.client:
            if not self.model_lock.acquire(blocking=False):
                raise RuntimeError("provider invocation is still active")
            if isinstance(self.bounded_client, DeadlineClient):
                self.bounded_client.deadline = invocation.deadline

            def act():
                try:
                    return asyncio.run(self.agent.act(invocation.observation, {}))
                finally:
                    self.model_lock.release()

            action = await asyncio.to_thread(act)
        else:
            action = await self.agent.act(invocation.observation, {})
        tool = "submit_offer" if self.role == "seller" else "submit_response"
        return [("env", tool, action)]
