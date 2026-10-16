"""Agent service owns provider execution, deadline, bounded memory and accruing private history."""

import asyncio
import threading
from datetime import datetime, timezone
from a2a_engine.llm.api import LLMClient
from .agents import HurricaneAgent, ScriptedManager


class DeadlineClient:
    def __init__(self, client):
        self.client, self.deadline = client, None

    def oneshot(self, messages):
        return self.client.oneshot(messages, deadline=self.deadline)


def submission(action):
    """The environment's action dict becomes one submit_turn call."""
    if not isinstance(action, dict) or action.get("parse_failed"):
        return {"parse_failed": True}
    actions = action.get("actions") if "actions" in action else [action]
    if not isinstance(actions, list):
        return {"parse_failed": True}
    return {"actions": [a for a in actions if isinstance(a, dict)]}


class HurricaneStrategy:
    supported_input_modalities = frozenset({"text"})

    def __init__(self, *, client=None):
        self.client = client
        self.bounded_client = DeadlineClient(client) if isinstance(client, LLMClient) else client
        self.model_lock = threading.Lock()
        self.agent = None
        self.telemetry = {}

    async def calls(self, invocation):
        if invocation.kind == "register":
            if invocation.observation["role"] != "manager":
                raise ValueError("unknown role")
            name = (
                invocation.observation.get("participant_id")
                or f"participant_{invocation.observation['participant'] - 1}"
            )
            self.agent = (
                HurricaneAgent(self.bounded_client, name,
                               history_window=invocation.observation.get("history_window"))
                if self.client else ScriptedManager(name)
            )
            return []
        if invocation.kind != "turn":
            return []
        if self.client:
            if not self.model_lock.acquire(blocking=False):
                raise RuntimeError("provider invocation is still active")
            if isinstance(self.bounded_client, DeadlineClient):
                self.bounded_client.deadline = invocation.deadline
            cancelled = threading.Event()
            deadline = getattr(invocation, "deadline", None)
            self.agent.history_allowed = lambda: (
                not cancelled.is_set()
                and (deadline is None or datetime.now(timezone.utc) < deadline)
            )

            def act():
                try:
                    return asyncio.run(self.agent.act(invocation.observation, {}))
                finally:
                    self.model_lock.release()

            try:
                action = await asyncio.to_thread(act)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        else:
            action = await self.agent.act(invocation.observation, {})
        return [("env", "submit_turn", submission(action))]
