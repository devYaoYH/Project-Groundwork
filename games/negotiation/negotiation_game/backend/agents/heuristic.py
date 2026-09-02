"""Smart heuristic agent that learns from rewards and coordinates via cheap talk."""

import random


class HeuristicAgent:
    """Smart heuristic agent that learns from rewards and coordinates via cheap talk."""

    def __init__(self, personality="balanced"):
        self.personality = personality

    async def cheap_talk(self, agent_id, round_number, turn_number,
                         game_config_public, conversation_so_far, own_memory, **kwargs):
        resources = game_config_public["resource_types"]

        if not own_memory:
            if turn_number == 0:
                return random.choice([
                    f"Hi! Let's coordinate. I'm thinking of focusing on {random.choice(resources)} this round.",
                    "Let's try to avoid overdrawing. Want to split resources?",
                    f"I'll go light on {random.choice(resources)} if you want to claim it.",
                ])
            else:
                other_msgs = [m["message"] for m in conversation_so_far if m["speaker"] != agent_id]
                if other_msgs:
                    mentioned = [res for res in resources if any(res in msg.lower() for msg in other_msgs)]
                    if mentioned:
                        avoid = mentioned[0]
                        alternatives = [res for res in resources if res != avoid]
                        return f"Okay, I'll steer clear of {avoid}. I'll focus on {random.choice(alternatives)}."
                return "Sounds good. Let's keep demand moderate."

        last = own_memory[-1]
        if last["overdrawn"]:
            if turn_number == 0:
                return random.choice([
                    "We overshot last round. I'll cut my quantities significantly.",
                    "Overdraw last time — I'm going to be much more conservative.",
                    f"Let me try a completely different mix. I'll avoid {random.choice(resources)} entirely.",
                ])
            return "Agreed, let's be careful. I'll keep my purchases low."

        if turn_number == 0:
            reward = last["my_reward"]
            if reward > 10:
                return "Good round for me. I'd like to try a similar approach."
            elif reward > 5:
                return f"Decent outcome. Maybe I'll adjust slightly — thinking about {random.choice(resources)}."
            else:
                return f"Low reward last time. I'm switching strategy — going for {random.choice(resources)} instead."

        return random.choice([
            "That works for me.",
            "Let's go with that plan.",
            "Okay, committing to my choice now.",
        ])

    async def decide_allocation(self, agent_id, round_number,
                                game_config_public, cheap_talk_transcript, own_memory):
        resources = game_config_public["resource_types"]
        costs = game_config_public["resource_costs"]
        supply = game_config_public["resource_supply"]
        budget = game_config_public["agent_budget"]
        max_types = game_config_public["max_resource_types_per_turn"]

        # Parse cheap talk for hints about what the other agent wants
        other_mentioned = set()
        my_mentioned = set()
        for msg in cheap_talk_transcript:
            for res in resources:
                if res in msg["message"].lower():
                    if msg["speaker"] != agent_id:
                        other_mentioned.add(res)
                    else:
                        my_mentioned.add(res)

        # Learn from memory
        best_alloc = None
        best_reward = -1
        overdraws = 0
        for m in (own_memory or []):
            if m["overdrawn"]:
                overdraws += 1
            elif m["my_reward"] > best_reward:
                best_reward = m["my_reward"]
                best_alloc = m["my_allocation"]

        # Avoid resources the other agent signaled interest in
        preferred = [res for res in resources if res not in other_mentioned]
        if not preferred:
            preferred = resources[:]

        # Strategy: exploit best known or explore
        if best_alloc and random.random() < 0.5 and overdraws < len(own_memory or []) * 0.4:
            alloc = dict(best_alloc)
            # Small variation
            for res in list(alloc.keys()):
                alloc[res] = max(0, alloc[res] + random.randint(-1, 1))
                if alloc[res] == 0:
                    del alloc[res]
        else:
            # Pick from preferred, be conservative
            n = random.randint(1, min(max_types, len(preferred)))
            chosen = random.sample(preferred, n)
            alloc = {}
            remaining = budget
            for res in chosen:
                max_qty = min(
                    int(remaining / costs[res]),
                    supply[res] // 2 + (1 if random.random() < 0.3 else 0)
                )
                if max_qty > 0:
                    qty = random.randint(1, max_qty)
                    alloc[res] = qty
                    remaining -= qty * costs[res]

        # Budget check
        total_cost = sum(costs.get(res, 1) * q for res, q in alloc.items())
        while total_cost > budget and alloc:
            res = random.choice(list(alloc.keys()))
            alloc[res] -= 1
            if alloc[res] <= 0:
                del alloc[res]
            total_cost = sum(costs.get(res, 1) * q for res, q in alloc.items())

        # Ensure non-empty: buy 1 of cheapest resource if empty
        if not alloc:
            cheapest = min(preferred, key=lambda r: costs[r])
            alloc[cheapest] = 1

        # Max types check
        while len(alloc) > max_types:
            res = min(alloc, key=lambda k: alloc[k])
            del alloc[res]

        return alloc
