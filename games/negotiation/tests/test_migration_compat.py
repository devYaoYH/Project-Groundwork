"""Regression tests for vendored negotiation compatibility paths."""

from __future__ import annotations

import asyncio
from pathlib import Path

from negotiation_analysis.data_loader import _trace_to_game
from negotiation_game.backend.agents.llm import LLMAgentBase
from negotiation_game.backend.storage import compress_events
from negotiation_judge.prompts import get_prompt_version


def test_reflection_uses_the_vendored_prompt_module_after_initialization():
    agent = LLMAgentBase(model="gpt-4o-mini")
    agent._initialized = True
    agent._thinking_enabled = False

    async def reply():
        return "reflection"

    agent._call_api_with_retries = reply
    assert asyncio.run(agent.reflect("agent_a", 2, 3.0, 4.0)) == "reflection"
    assert agent._messages[-2]["role"] == "user"
    assert agent._messages[-1] == {"role": "assistant", "content": "reflection"}


def test_legacy_v3_analysis_decompresses_events_from_the_vendored_module():
    trace = {
        "schema_version": 3,
        "episode_uid": "legacy-v3",
        "game_config": {"experiment_label": "stable_competitive_hidden_thinking"},
        "result": {"rounds": [{"round_number": 1}]},
        "events_compressed": compress_events([
            {"type": "cheap_talk", "data": {"round": 1, "turn": 1, "speaker": "agent_a", "message": "hello"}},
        ]),
    }

    environment = _trace_to_game(trace)

    assert environment["events"][0]["data"]["message"] == "hello"
    assert environment["rounds"][0]["cheap_talk_transcript"][0]["message"] == "hello"


def test_negotiation_judge_has_an_offline_prompt_version():
    version = get_prompt_version()
    assert len(version) == 12
    assert version.isalnum()


def test_webapp_server_imports_in_the_vendored_layout():
    from negotiation_game.backend import server

    assert server._STATIC_DIR == Path(__file__).resolve().parents[1] / "webapp" / "static"
    assert server._STATIC_DIR.is_dir()
