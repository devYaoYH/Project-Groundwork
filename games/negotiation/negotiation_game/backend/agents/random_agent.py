"""Random baseline agent for testing."""

import random


class RandomAgent:
    """Simple random baseline agent for testing."""

    async def cheap_talk(self, agent_id, round_number, turn_number,
                         game_config_public, conversation_so_far, own_memory, **kwargs) -> str:
        messages = [
            "I'm thinking about going for wood this round.",
            "Gold seems valuable, I might buy some.",
            "Let's try to avoid overdrawing resources.",
            "I'll keep my purchases modest this time.",
            "What are you planning to buy?",
            "Maybe we should coordinate to avoid conflicts.",
        ]
        return random.choice(messages)

    async def decide_allocation(self, agent_id, round_number,
                                game_config_public, cheap_talk_transcript,
                                own_memory) -> dict[str, int]:
        resources = game_config_public["resource_types"]
        costs = game_config_public["resource_costs"]
        budget = game_config_public["agent_budget"]
        max_types = game_config_public["max_resource_types_per_turn"]

        # Pick 1-2 random resource types
        n_types = random.randint(1, max_types)
        chosen = random.sample(resources, n_types)

        allocation = {}
        remaining_budget = budget
        for r in chosen:
            max_affordable = int(remaining_budget / costs[r])
            if max_affordable > 0:
                qty = random.randint(1, min(max_affordable, 5))
                allocation[r] = qty
                remaining_budget -= qty * costs[r]

        return allocation
