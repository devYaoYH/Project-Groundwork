"""Agent-side /2 multimodal policy preserving a seat's private turn history."""

import asyncio
import threading
from a2a_engine.llm.api import LLMClient
from .agents import TangramReferenceAgent, ScriptedAgent


class DeadlineClient:
    def __init__(self, client):
        self.client = client
        self.deadline = None

    def oneshot(self, messages):
        return self.client.oneshot(messages, deadline=self.deadline)


class TangramReferenceStrategy:
    supported_input_modalities = {"text", "image"}

    def __init__(self, *, client=None):
        self.client = client
        self.capabilities = (
            client.capabilities if client else ScriptedAgent.capabilities
        )
        self.bounded = (
            DeadlineClient(client) if isinstance(client, LLMClient) else client
        )
        self.agent = None
        self.lock = threading.Lock()
        self.telemetry = {}

    async def calls(self, invocation):
        if invocation.kind == "register":
            role = invocation.observation["role"]
            self.agent = (
                TangramReferenceAgent(self.bounded, role)
                if self.client
                else ScriptedAgent(role, None)
            )
            return []
        if invocation.kind != "turn":
            return []
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("provider invocation already active")
        if isinstance(self.bounded, DeadlineClient):
            self.bounded.deadline = invocation.deadline

        def run():
            try:
                return asyncio.run(self.agent.act(invocation.observation, {}))
            finally:
                self.lock.release()

        action = await asyncio.to_thread(run)
        return [("env", "submit_tangram", action)]
