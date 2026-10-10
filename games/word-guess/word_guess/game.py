"""WordGuessGame: a two-agent yes/no word-guessing environment."""

import asyncio
import random
import re
import uuid
from typing import Any

from pydantic import Field

from a2a_engine import (
    EventLog,
    EpisodeConfigBase,
    EpisodeTrace,
    register_environment,
)
from a2a_engine._context import current_conversation_id
from a2a_engine.llm.factory import make_llm_client
from a2a_engine.tracing_otel import get_tracer

from word_guess.agents import GuesserAgent, HostAgent
from word_guess.declaration import DECLARATION

DEFAULT_POOL = ["dog", "apple", "car", "piano", "river", "book"]


class WordGuessConfig(EpisodeConfigBase):
    """Config for the word-guess environment."""

    environment_id: str = "word_guess"
    num_agents: int = 2
    secret_word: str | None = None
    word_pool: list[str] = Field(default_factory=lambda: list(DEFAULT_POOL))
    max_turns: int = 20
    turn_timeout_s: float = Field(default=120, gt=0)


# ---------------------------------------------------------------------------
# Scripted dry-run agents (no LLM).
# ---------------------------------------------------------------------------


def _scripted_span(name: str):
    tracer = get_tracer()
    span_cm = tracer.start_as_current_span(f"invoke_agent {name}")
    return span_cm


class _ScriptedGuesser:
    """Fixed script for dry runs: ask, ask, then guess the secret word."""

    name = "guesser"

    def __init__(self, secret_word: str) -> None:
        self._lines = [
            "is it an animal?",
            "is it a dog?",
            f"I guess: {secret_word}",
        ]
        self._i = 0

    async def act(self, observation, tools):
        with _scripted_span(self.name) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            conv_id = current_conversation_id.get()
            if conv_id:
                span.set_attribute("gen_ai.conversation.id", conv_id)
            line = self._lines[min(self._i, len(self._lines) - 1)]
            self._i += 1
            return {"text": line}


class _ScriptedHost:
    """Truthful host for dry runs — case-insensitive word match."""

    name = "host"

    def __init__(self, secret_word: str) -> None:
        self.secret_word = secret_word

    async def act(self, observation, tools):
        with _scripted_span(self.name) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            conv_id = current_conversation_id.get()
            if conv_id:
                span.set_attribute("gen_ai.conversation.id", conv_id)
            return self._reply(observation)

    def _reply(self, observation):
        utter = observation["last_guesser"].lower()
        m = re.match(r"\s*i guess[:\s]+([a-zA-Z\-']+)", utter)
        if m:
            return {"text": "correct!" if m.group(1).lower() == self.secret_word.lower() else "wrong"}
        # Yes/no heuristics for the canned questions.
        if "animal" in utter:
            return {"text": "yes" if self.secret_word.lower() == "dog" else "no"}
        if self.secret_word.lower() in utter:
            return {"text": "yes"}
        return {"text": "no"}


# ---------------------------------------------------------------------------
# Environment.
# ---------------------------------------------------------------------------


def _is_correct(host_reply: str) -> bool:
    return host_reply.strip().lower().startswith("correct")


def _extract_guess(text: str) -> str | None:
    m = re.match(r"\s*i guess[:\s]+([a-zA-Z\-']+)", text.lower())
    return m.group(1) if m else None


class WordGuessGame:
    """Round-robin word-guessing environment."""

    def __init__(self, config: dict | WordGuessConfig, dry_run: bool = False, *, runtime_manager=None, runtime_context=None) -> None:
        self.config = config if isinstance(config, WordGuessConfig) else WordGuessConfig(**config)
        self.dry_run = dry_run
        rng = random.Random(self.config.seed)
        self.secret_word = self.config.secret_word or rng.choice(self.config.word_pool)
        self.events = EventLog.from_config(self.config)
        self.runtime = runtime_context or (runtime_manager.provision(self.config.model_dump(), scripted=dry_run) if runtime_manager else None)
        try:
            self._build_agents()
        except BaseException:
            if self.runtime:
                self.runtime.close()
            raise

    def _build_agents(self) -> None:
        agents = self.config.agents
        for seat, role in enumerate(("guesser", "host")):
            cfg = agents[seat].model_dump() if len(agents) > seat else {"model": "gpt-4o-mini"}
            if cfg.get("runtime", "in_process") != "in_process":
                if not self.runtime:
                    raise ValueError("word_guess remote seats require runtime context")
                from .remote import RemoteTextAgent
                agent = RemoteTextAgent(self.runtime, seat, timeout_s=getattr(self.config, "turn_timeout_s", 120))
            elif self.dry_run:
                agent = _ScriptedGuesser(self.secret_word) if seat == 0 else _ScriptedHost(self.secret_word)
            else:
                agent = GuesserAgent(make_llm_client(cfg)) if seat == 0 else HostAgent(make_llm_client(cfg), self.secret_word)
            setattr(self, role, agent)

    @classmethod
    def build_program(cls):
        from .workflow import build_program
        return build_program()

    @classmethod
    def runtime_capability(cls):
        from a2a_engine.workflow_runtime import RuntimeCapability
        from .remote import WORD_GUESS_TOOLS
        return RuntimeCapability(WORD_GUESS_TOOLS)

    def run(self) -> EpisodeTrace:
        return asyncio.run(self._run_async())

    async def _run_async(self) -> EpisodeTrace:
        from .workflow import GameContext
        from .remote import RemoteTextAgent
        remote_agents = [agent for agent in (self.guesser, self.host) if isinstance(agent, RemoteTextAgent)]
        try:
            for agent in remote_agents:
                await asyncio.to_thread(agent.register, self.secret_word)
            state = await self.build_program().ainvoke({}, context=GameContext(self),
                config={"recursion_limit": max(10, self.config.max_turns * 7 + 5)})
            return self._trace(state)
        finally:
            if self.runtime:
                try:
                    for agent in remote_agents:
                        await asyncio.to_thread(agent.episode_end)
                finally:
                    self.runtime.close()

    def _trace(self, state):
        won, turns_used = state["won"], state["turns_used"]
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "secret_word": self.secret_word,
                "guessed_word": state["guessed_word"],
                "won": won,
                "turns_used": turns_used,
            },
            metrics={
                "won": won,
                "turns_used": turns_used,
                "efficiency": turns_used / self.config.max_turns,
            },
        )


register_environment(
    "word_guess",
    WordGuessGame,
    declaration=DECLARATION,
    package="word-guess",
    # Dry runs use the scripted agents above, so no API keys are involved.
    dry_run_checks_keys=False,
)
