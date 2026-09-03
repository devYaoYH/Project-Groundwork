"""
Agent implementations for the Negotiation Environment.
"""

from negotiation_game.backend.agents.random_agent import RandomAgent
from negotiation_game.backend.agents.heuristic import HeuristicAgent
from negotiation_game.backend.agents.llm import LLMAgent, LLMAgentStdlib
from negotiation_game.backend.agents.human import HumanAgent
from negotiation_game.backend.agents.factory import make_agent


class AgentInterface:
    """
    Abstract agent interface. Implementations call out to LLM APIs
    or other decision-making systems.
    """

    async def cheap_talk(
        self,
        agent_id: str,
        round_number: int,
        turn_number: int,
        game_config_public: dict,
        conversation_so_far: list[dict],
        own_memory: list[dict],
        **kwargs,
    ) -> str:
        """Generate a cheap-talk message. Return a string."""
        raise NotImplementedError

    async def decide_allocation(
        self,
        agent_id: str,
        round_number: int,
        game_config_public: dict,
        cheap_talk_transcript: list[dict],
        own_memory: list[dict],
    ) -> dict[str, int]:
        """
        Decide resource allocation. Return e.g. {"wood": 3, "gold": 2}.
        Must choose at most max_resource_types_per_turn types.
        Total cost must be within budget.
        """
        raise NotImplementedError


__all__ = [
    "AgentInterface",
    "RandomAgent",
    "HeuristicAgent",
    "LLMAgent",
    "LLMAgentStdlib",
    "HumanAgent",
    "make_agent",
]
