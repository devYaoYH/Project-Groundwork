"""Remote seat policy retaining image and dialogue history between rounds."""

import asyncio
import threading
from a2a_engine.llm.api import LLMClient
from .agents import MapDirectorAgent, ScriptedMapAgent, system_prompts


class DeadlineClient:
    def __init__(self, client):
        self.client = client
        self.deadline = None

    @property
    def capabilities(self):
        return self.client.capabilities

    def oneshot(self, messages):
        return self.client.oneshot(messages, deadline=self.deadline)


class MapDirectorStrategy:
    supported_input_modalities = {"text", "image"}

    def __init__(self, *, client=None):
        self.client = client
        self.capabilities = (
            client.capabilities if client else ScriptedMapAgent.capabilities
        )
        self.bounded_client = (
            DeadlineClient(client) if isinstance(client, LLMClient) else client
        )
        self.model_lock = threading.Lock()
        self.agent = None
        self.telemetry = {}

    async def calls(self, invocation):
        if invocation.kind == "register":
            role = invocation.observation["role"]
            version = invocation.observation.get("task_version", "v1")
            self.agent = (
                MapDirectorAgent(self.bounded_client, role=role, system_prompt=system_prompts(version)[role])
                if self.client
                else ScriptedMapAgent(role, task_version=version)
            )
            return []
        if invocation.kind != "turn":
            return []
        if self.client:
            if not self.model_lock.acquire(blocking=False):
                raise RuntimeError("provider invocation remains active")
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
        return [("env", "submit_action", action)]
