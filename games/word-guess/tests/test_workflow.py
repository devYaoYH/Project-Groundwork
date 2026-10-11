import asyncio
from copy import deepcopy

import pytest

from .baseline import original_loop
from word_guess.agents import GuesserAgent, HostAgent
from word_guess.game import WordGuessGame


class ControlledAgent:
    def __init__(self, role, lines, calls):
        self.role, self.lines, self.calls, self.index = role, lines, calls, 0

    async def act(self, observation, tools):
        self.calls.append((self.role, deepcopy(observation)))
        result = self.lines[min(self.index, len(self.lines) - 1)]
        self.index += 1
        return {"text": result}


def controlled(guesses, replies, turns):
    game = WordGuessGame({"secret_word": "dog", "max_turns": turns}, dry_run=True)
    calls = []
    game.guesser = ControlledAgent("guesser", guesses, calls)
    game.host = ControlledAgent("host", replies, calls)
    return game, calls


CASES = [
    ([" I GUESS: Dog"], [" Correct! "], 6),
    (["I guess: cat", "a question"], ["wrong", "no"], 2),
    (["is it a dog?"], ["correct!"], 3),
    (["I guess: cat", "I guess: dog"], ["wrong", "correct!"], 6),
    (["I guess: dog"], [""], 1),
    (["not a guess"], ["no"], 40),
]


@pytest.mark.parametrize("guesses,replies,turns", CASES)
def test_pre_refactor_exact_parity(guesses, replies, turns):
    old, old_calls = controlled(guesses, replies, turns)
    expected = asyncio.run(original_loop(old))
    new, calls = controlled(guesses, replies, turns)
    trace = new.run()
    assert ([(e.type, e.data) for e in trace.events], trace.final_state, trace.metrics) == expected
    assert calls == old_calls


def test_zero_turns_retains_no_calls_and_zero_division():
    old, old_calls = controlled(["unused"], ["unused"], 0)
    with pytest.raises(ZeroDivisionError):
        asyncio.run(original_loop(old))
    new, calls = controlled(["unused"], ["unused"], 0)
    with pytest.raises(ZeroDivisionError):
        new.run()
    assert calls == old_calls == []
    assert [(e.type, e.data) for e in new.events.all()] == [
        ("game_start", {"max_turns": 0, "word_pool_size": 6}), ("game_end", {"won": False, "turns_used": 0}),
    ]


class MockProvider:
    def __init__(self, responses):
        self.responses, self.messages = iter(responses), []

    def oneshot(self, messages):
        self.messages.append(deepcopy(messages))
        return next(self.responses)


@pytest.mark.parametrize("cls,kwargs,observation,expected", [
    (GuesserAgent, {}, {"history": [], "turns_remaining": 5}, "I guess: dog"),
    (HostAgent, {"secret_word": "dog"}, {"history": [], "last_guesser": "I guess: dog"}, "correct!"),
    (HostAgent, {"secret_word": "dog"}, {"history": [], "last_guesser": "hello"}, "no"),
])
def test_provider_parser_empty_and_first_line(cls, kwargs, observation, expected):
    provider = MockProvider(["  " if expected == "no" else expected + "\nignored"])
    agent = cls(provider, **kwargs)
    assert asyncio.run(agent.act(observation, {})) == {"text": expected}
    assert provider.messages == [agent.build_messages(observation, {})]
