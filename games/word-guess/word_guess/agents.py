"""Host and Guesser LLM agents for the word-guess environment."""

from collections.abc import Callable
from typing import Any

from a2a_engine import LLMAgent

HOST_SYSTEM = (
    "You are the Host of a yes/no word-guessing environment. The secret word is "
    "{secret_word!r}. The Guesser will ask yes/no questions or make a guess. "
    "Reply with exactly one short line:\n"
    "- 'yes' or 'no' to a yes/no question (truthfully).\n"
    "- 'correct!' if the Guesser's guessed word matches {secret_word!r} exactly (case-insensitive).\n"
    "- 'wrong' if the Guesser guesses an incorrect word.\n"
    "Never reveal the word otherwise."
)

GUESSER_SYSTEM = (
    "You are the Guesser. Your job is to identify a secret common English noun. "
    "Each turn, output ONE line: either a yes/no question, or 'I guess: <word>'. "
    "Use prior answers to narrow down the word. Be efficient."
)


class HostAgent(LLMAgent):
    """Host: knows the secret word and answers yes/no truthfully."""

    def __init__(self, client, secret_word: str) -> None:
        super().__init__(client, system_prompt=HOST_SYSTEM.format(secret_word=secret_word),
                         name="host")
        self.secret_word = secret_word

    def build_messages(
        self,
        observation: dict[str, Any],
        tools: dict[str, Callable],
    ) -> list[dict]:
        prompt = self.build_prompt(observation)
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

    def build_prompt(self, observation: dict[str, Any]) -> str:
        history = observation.get("history", [])
        lines = [f"{turn['speaker']}: {turn['text']}" for turn in history]
        lines.append("Guesser just said: " + observation["last_guesser"])
        lines.append("Reply as Host (one short line):")
        return "\n".join(lines)

    def parse_response(self, text: str, observation, tools) -> dict[str, Any]:
        return {"text": text.strip().splitlines()[0] if text.strip() else "no"}


class GuesserAgent(LLMAgent):
    """Guesser: asks yes/no questions or makes a guess."""

    def __init__(self, client) -> None:
        super().__init__(client, system_prompt=GUESSER_SYSTEM, name="guesser")

    def build_messages(
        self,
        observation: dict[str, Any],
        tools: dict[str, Callable],
    ) -> list[dict]:
        prompt = self.build_prompt(observation)
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

    def build_prompt(self, observation: dict[str, Any]) -> str:
        history = observation.get("history", [])
        if not history:
            return "Start the environment. Ask your first yes/no question."
        lines = [f"{turn['speaker']}: {turn['text']}" for turn in history]
        lines.append(
            f"Turns remaining: {observation['turns_remaining']}. "
            "Output ONE line: a yes/no question, or 'I guess: <word>'."
        )
        return "\n".join(lines)

    def parse_response(self, text: str, observation, tools) -> dict[str, Any]:
        line = text.strip().splitlines()[0] if text.strip() else ""
        return {"text": line}
