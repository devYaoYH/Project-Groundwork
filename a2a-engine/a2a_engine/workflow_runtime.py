"""Executable invocation bindings; no alternate scheduler or workflow catalog."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.runtime import Runtime

_invoking = ContextVar("workflow_agent_invocation", default=None)


def require_agent_step(actor=None, operation=None):
    scope = _invoking.get()
    if not scope:
        raise RuntimeError("agent invocation requires an executable AgentStep")
    if actor is not None:
        if scope.actor is not actor or scope.operation != operation or scope.used:
            raise RuntimeError("agent invocation does not match executable AgentStep")
        scope.used = True


@dataclass
class InvocationScope:
    actor: object
    operation: str
    used: bool = False


class GuardedAgent:
    """Reject exercised hidden calls outside the selected executable handoff."""

    def __init__(self, agent):
        self.agent = agent

    def __getattr__(self, name):
        return getattr(self.agent, name)

    async def act(self, observation, tools):
        require_agent_step(self, "act")
        return await self.agent.act(observation, tools)


@dataclass(frozen=True)
class AgentStep:
    actor: Callable
    operation: str
    observation: Callable
    adapter: Callable

    async def __call__(self, state: dict, runtime: Runtime[Any]):
        actor = self.actor(state, runtime.context)
        observation = self.observation(state, runtime.context)
        token = _invoking.set(InvocationScope(actor, self.operation))
        try:
            return await self.adapter(actor, self.operation, observation, state, runtime.context)
        finally:
            _invoking.reset(token)


@dataclass(frozen=True)
class RuntimeCapability:
    tools: tuple

    def bind(self, manager):
        return {"runtime_manager": manager}


def runtime_capability(environment_id):
    from .registry import get_environment_spec
    capability = getattr(get_environment_spec(environment_id).cls, "runtime_capability", None)
    return capability() if capability else None


class GraphProgram:
    def __init__(self, builder, *, incomplete_reason=None):
        self.builder = builder
        self.compiled = builder.compile()
        self.incomplete_reason = incomplete_reason

    async def ainvoke(self, state, *, context, **kwargs):
        return await self.compiled.ainvoke(state, context=context, **kwargs)


@dataclass(frozen=True)
class BatchStep:
    program: GraphProgram
    items: Callable
    concurrency: Callable
    serial_key: Callable

    async def __call__(self, state: dict, runtime: Runtime[Any]):
        context = runtime.context
        items = self.items(state, context)
        limit = self.concurrency(state, context)
        semaphore = asyncio.Semaphore(limit) if limit else None
        locks = {}

        async def invoke(item):
            key = self.serial_key(item, context)
            lock = locks.setdefault(key, asyncio.Lock()) if key is not None else None

            async def execute():
                return await self.program.ainvoke({"call": item}, context=context)

            async def serialized():
                if lock is None:
                    return await execute()
                async with lock:
                    return await execute()

            if semaphore is None:
                return await serialized()
            async with semaphore:
                return await serialized()

        results = await asyncio.gather(*(invoke(item) for item in items), return_exceptions=True)
        return {"batch_results": results}
