"""Executable invocation bindings; no alternate scheduler or workflow catalog."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.runtime import Runtime

_invoking = ContextVar("workflow_agent_invocation", default=None)


def require_agent_step():
    if not _invoking.get():
        raise RuntimeError("agent invocation requires an executable AgentStep")


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
        require_agent_step()
        scope = _invoking.get()
        if scope.actor is not self or scope.operation != "act" or scope.used:
            raise RuntimeError("agent invocation does not match executable AgentStep")
        scope.used = True
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
    def __init__(self, builder):
        self.builder = builder
        self.compiled = builder.compile()

    async def ainvoke(self, state, *, context, **kwargs):
        return await self.compiled.ainvoke(state, context=context, **kwargs)
