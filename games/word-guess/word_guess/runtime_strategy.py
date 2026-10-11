"""Agent-side Word Guess policy reusing the existing prompts and parsers."""

import asyncio
import threading

from a2a_engine.llm.api import LLMClient

from .agents import GuesserAgent, HostAgent


class DeadlineClient:
    def __init__(self, client):
        self.client, self.deadline = client, None

    def oneshot(self, messages):
        return self.client.oneshot(messages, deadline=self.deadline)


class WordGuessStrategy:
    def __init__(self, *, client=None, guess_lines=None):
        self.client = client
        self.bounded_client = DeadlineClient(client) if isinstance(client, LLMClient) else client
        self.guess_lines = guess_lines or ["is it an animal?", "is it a dog?", "I guess: dog"]
        self.model_lock = threading.Lock()
        self.agent = None
        self.index = 0
        self.telemetry = {}

    async def calls(self, invocation):
        if invocation.kind == "register":
            from .game import _ScriptedHost
            role = invocation.observation["role"]
            if role == "host":
                secret = invocation.observation["secret_word"]
                self.agent = HostAgent(self.bounded_client, secret) if self.client else _ScriptedHost(secret)
            elif role == "guesser":
                self.agent = GuesserAgent(self.bounded_client) if self.client else None
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
        elif self.agent:
            action = await self.agent.act(invocation.observation, {})
        else:
            action = {"text": self.guess_lines[min(self.index, len(self.guess_lines) - 1)]}
            self.index += 1
        return [("env", "submit_action", action)]
