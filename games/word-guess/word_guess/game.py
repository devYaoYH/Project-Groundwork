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

DEFAULT_POOL = ["dog", "apple", "car", "piano", "river", "book"]


class WordGuessConfig(EpisodeConfigBase):
    """Config for the word-guess environment."""

    environment_id: str = "word_guess"
    num_agents: int = 2
    secret_word: str | None = None
    word_pool: list[str] = Field(default_factory=lambda: list(DEFAULT_POOL))
    max_turns: int = 20


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

    def __init__(self, config: dict | WordGuessConfig, dry_run: bool = False) -> None:
        self.config = config if isinstance(config, WordGuessConfig) else WordGuessConfig(**config)
        self.dry_run = dry_run
        rng = random.Random(self.config.seed)
        self.secret_word = self.config.secret_word or rng.choice(self.config.word_pool)
        self.events = EventLog.from_config(self.config)
        self._build_agents()

    def _build_agents(self) -> None:
        if self.dry_run:
            self.guesser = _ScriptedGuesser(self.secret_word)
            self.host = _ScriptedHost(self.secret_word)
            return
        agents = self.config.agents
        guesser_cfg = agents[0].model_dump() if len(agents) >= 1 else {"model": "gpt-4o-mini"}
        host_cfg = agents[1].model_dump() if len(agents) >= 2 else {"model": "gpt-4o-mini"}
        self.guesser = GuesserAgent(make_llm_client(guesser_cfg))
        self.host = HostAgent(make_llm_client(host_cfg), self.secret_word)

    def run(self) -> EpisodeTrace:
        return asyncio.run(self._run_async())

    async def _run_async(self) -> EpisodeTrace:
        self.events.append(
            "game_start",
            data={"max_turns": self.config.max_turns, "word_pool_size": len(self.config.word_pool)},
        )
        history: list[dict[str, str]] = []
        guessed_word: str | None = None
        won = False
        turns_used = 0

        for turn in range(1, self.config.max_turns + 1):
            turns_used = turn
            obs_g = {"history": list(history), "turns_remaining": self.config.max_turns - turn + 1}
            g_action = await self.guesser.act(obs_g, tools={})
            g_text = g_action["text"]
            history.append({"speaker": "guesser", "text": g_text})
            self.events.append("message", data={"speaker": "guesser", "text": g_text})

            obs_h = {"history": list(history[:-1]), "last_guesser": g_text}
            h_action = await self.host.act(obs_h, tools={})
            h_text = h_action["text"]
            history.append({"speaker": "host", "text": h_text})
            self.events.append("message", data={"speaker": "host", "text": h_text})

            guess = _extract_guess(g_text)
            if guess:
                guessed_word = guess
                if _is_correct(h_text):
                    won = True
                    break

        self.events.append(
            "game_end",
            data={"won": won, "turns_used": turns_used},
        )

        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "secret_word": self.secret_word,
                "guessed_word": guessed_word,
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
    package="word-guess",
    # Dry runs use the scripted agents above, so no API keys are involved.
    dry_run_checks_keys=False,
)
