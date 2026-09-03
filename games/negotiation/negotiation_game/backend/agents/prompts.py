"""
Prompt templates for LLM-backed negotiation agents.

All system prompts, response format instructions, and phase prompts
are defined here for easy adjustment and review.
"""

import json

# ---------------------------------------------------------------------------
# Goal descriptions
# ---------------------------------------------------------------------------

GOAL_DEFAULT = "maximize your cumulative reward across all rounds"
GOAL_JOINT = "maximize the JOINT (combined) reward of both parties across all rounds — cooperate to achieve the best possible outcome for everyone"

# ---------------------------------------------------------------------------
# System prompt: environment rules + situation (project-based rewards)
# ---------------------------------------------------------------------------

SYSTEM_RULES_PROJECTS = """\
## Environment Rules
- Resources available: {resource_types}
- Resource supply per round: {resource_supply}
- Resource costs: {resource_costs}
- Your budget per round: ${agent_budget}
- You may purchase at most {max_types} different resource types per round.
- Resources do NOT carry over between rounds — each round starts fresh with a new budget and new supply.
- If total demand for ANY resource exceeds supply, the round is ANNULLED and both parties get 0 reward.

## Projects
You have 3 projects. Each project requires specific resources per run and yields a reward per run.
The other party has their own projects with different requirements and rewards.
Your reward = sum of (runs × reward) for each project you can complete with your purchased resources.
IMPORTANT: Unspent money has NO value — only completed project runs count towards your score.
NOTE: If you purchase resources but lack the full set required to complete at least one run of any project, your reward is 0 (this is NOT an annulment — the round proceeds normally, you simply completed no projects).
{projects_info}
{opponent_projects_info}{synergy_info}

## Decision Format
Submit a JSON object with your resource purchases AND project allocations:
{decision_example}
The "projects" field specifies how many times to run each project. If omitted, the engine assigns resources to projects automatically by prioritizing projects in the order presented to you.

## Environment Flow
{game_flow_info}
At ANY point during the conversation, you may finalize your purchase.
Once you submit a purchase, your decision is locked for that round — you cannot change it.
If the other party has already submitted their purchase for this round, you will be asked for your final decision.

## Your Situation
- Your goal: {goal}
- {opponent_info}"""

# ---------------------------------------------------------------------------
# Response format instructions (appended to system prompt)
# ---------------------------------------------------------------------------

RESPONSE_FORMAT_THINKING_PROJECTS = """

## Response Format
You MUST always respond with a JSON object containing exactly three fields:
{{
  "thinking": "your private reasoning (hidden from the other party)",
  "speech": "your message to the other party (visible to them)",
  "action": null
}}

- Set "action" to null while you want to keep chatting.
- Set "action" to a purchase object with project allocation to finalize.

Example — chatting:
{{"thinking": "They might want gold. I should consider my options carefully.", "speech": "I'm thinking about gold. What about you?", "action": null}}

Example — purchasing:
{{"thinking": "I'll buy wood for my projects.", "speech": "Good luck!", "action": {{"wood": 5, "stone": 2, "projects": {{"{proj_name_0}": 1, "{proj_name_1}": 1}}}}}}

IMPORTANT: Respond with ONLY the JSON object. No text before or after it.

I will guide you through each phase with instructions."""

RESPONSE_FORMAT_NO_THINKING_PROJECTS = """

## Response Format
Everything you say is passed VERBATIM to the other party — you have NO private channel.
The other party sees your FULL response. Think carefully about what you reveal.

To send a message, respond with plain natural language text — NOT JSON, NOT a wrapper object.
Just write your message directly as plain text.

To finalize a purchase, respond with ONLY a JSON object with your resource purchases and project allocations:
{{"wood": 3, "gold": 1, "projects": {{"{proj_name_0}": 1, "{proj_name_1}": 1}}}}

I will guide you through each phase with instructions."""

# Anthropic-specific suffix injected into the system prompt for the Messages API
ANTHROPIC_JSON_SUFFIX = (
    "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object. "
    "No other text before or after. The JSON must have exactly three fields: "
    "'thinking' (string), 'speech' (string), and 'action' (null or object)."
)

# ---------------------------------------------------------------------------
# Helper: build the full system prompt
# ---------------------------------------------------------------------------


def _project_names(projects: list[dict]) -> list[str]:
    """Extract project names, falling back to project_a/project_b."""
    if projects and len(projects) >= 2:
        return [p["name"] for p in projects[:2]]
    return ["project_a", "project_b"]


def _decision_example(project_names: list[str]) -> str:
    """Build a decision JSON example using actual project names."""
    p0, p1 = project_names[0], project_names[1]
    return f'{{"wood": 3, "gold": 1, "projects": {{"{p0}": 1, "{p1}": 2}}}}'


def _format_projects(projects: list[dict]) -> str:
    lines = []
    for p in projects:
        req_str = ", ".join(f"{r}×{q}" for r, q in p["requirements"].items())
        lines.append(f"  - {p['name']}: requires [{req_str}], reward = {p['reward']}/run")
    return "\n".join(lines)


def _format_synergy(scenario_synergy: dict | None) -> str:
    if not scenario_synergy:
        return ""
    res = scenario_synergy["resource"]
    thresh = scenario_synergy["threshold"]
    bonus = scenario_synergy["bonus"]
    return (
        f"Synergy Bonus: if combined {res} ≥ {thresh} "
        f"(each player must buy ≥1), each player gets +{bonus} flat bonus."
    )


def build_system_prompt(game_config_public: dict, agent_id: str, thinking_enabled: bool) -> str:
    """Assemble the complete system prompt from environment config."""
    goal_text = GOAL_JOINT if game_config_public.get("maximize_joint") else GOAL_DEFAULT
    is_shifting = game_config_public.get("is_shifting", False)

    # Opponent info
    if is_shifting:
        opponent_info = "This is a standalone one-round negotiation."
    elif game_config_public.get("opponent_shifts"):
        opponent_info = "You are playing against a DIFFERENT opponent each round."
    else:
        opponent_info = "The other party is also purchasing from the same shared pool."

    if game_config_public.get("share_projects"):
        opponent_info += (
            "\n\nIMPORTANT — As a FIRST step in each round's cheap talk, share your "
            "project details (names, resource requirements, rewards) with the other party "
            "and ask them to share theirs."
        )

    if game_config_public.get("think_about_opponent"):
        opponent_info += (
            "\n\nIMPORTANT — During each round's cheap talk, actively reason about "
            "the other party's goals and which projects they might be trying to run. "
            "Consider what resources they need, how their interests align or conflict "
            "with yours, and how you can use this understanding to inform your strategy."
        )

    projects = game_config_public.get("own_projects", [])
    projects_info = _format_projects(projects)
    opponent_projects = game_config_public.get("opponent_projects", [])
    if opponent_projects:
        opponent_projects_info = "\n### Other Party's Projects\n" + _format_projects(opponent_projects) + "\n"
    else:
        opponent_projects_info = ""
    synergy_info = _format_synergy(game_config_public.get("scenario_synergy"))
    pnames = _project_names(projects)
    decision_example = _decision_example(pnames)

    max_turns = game_config_public["cheap_talk_turns"]
    if max_turns == 0:
        game_flow_info = "Each round you may exchange as many messages as necessary with the other party (cheap talk)."
    else:
        game_flow_info = (
            f"Each round you may exchange up to {max_turns} messages with the other party (cheap talk).\n"
            f"If you haven't submitted a purchase after {max_turns} exchanges, you will be asked for your final decision."
        )

    if thinking_enabled:
        response_fmt = RESPONSE_FORMAT_THINKING_PROJECTS
    else:
        response_fmt = RESPONSE_FORMAT_NO_THINKING_PROJECTS

    template = SYSTEM_RULES_PROJECTS + response_fmt

    # Shifting agents see only 1 round; hide "resources don't carry over" since it's irrelevant
    if is_shifting:
        template = template.replace(
            "- Resources do NOT carry over between rounds — each round starts fresh with a new budget and new supply.\n",
            "",
        )

    return template.format(
        agent_id=agent_id,
        num_rounds=1 if is_shifting else game_config_public["num_rounds"],
        resource_types=", ".join(game_config_public["resource_types"]),
        resource_supply=json.dumps(game_config_public["resource_supply"]),
        resource_costs=json.dumps(game_config_public["resource_costs"]),
        agent_budget=game_config_public["agent_budget"],
        max_types=game_config_public["max_resource_types_per_turn"],
        max_turns=game_config_public["cheap_talk_turns"],
        game_flow_info=game_flow_info,
        goal=goal_text,
        opponent_info=opponent_info,
        projects_info=projects_info,
        opponent_projects_info=opponent_projects_info,
        synergy_info=synergy_info,
        decision_example=decision_example,
        proj_name_0=pnames[0],
        proj_name_1=pnames[1],
    )


# ---------------------------------------------------------------------------
# Cheap-talk phase prompts
# ---------------------------------------------------------------------------


def cheap_talk_prompt(
    agent_id: str,
    turn_number: int,
    game_config_public: dict,
    conversation_so_far: list[dict],
    thinking_enabled: bool,
    round_number: int = 1,
    project_update: str | None = None,
) -> str:
    """Return the user-message prompt for a cheap-talk turn.

    Args:
        project_update: If set, prepended to turn-0 prompt with new project details
                        (used in rotating-projects games for rounds > 1).
    """
    opponent_msgs = [m for m in conversation_so_far if m["speaker"] not in (agent_id, "system") and m.get("type") == "speech"]
    system_notices = [m for m in conversation_so_far if m["speaker"] == "system"]

    if thinking_enabled:
        prompt = _cheap_talk_thinking(turn_number, game_config_public, opponent_msgs, system_notices, round_number)
    else:
        prompt = _cheap_talk_no_thinking(turn_number, game_config_public, opponent_msgs, system_notices, round_number)

    if project_update and turn_number == 0:
        prompt = f"{project_update}\n\n{prompt}"

    if turn_number == 0 and game_config_public.get("think_about_opponent"):
        prompt += (
            "\n\nReminder: Actively reason about the other party's goals and which "
            "projects they might be trying to run. Consider what resources they need "
            "and how their interests align or conflict with yours."
        )

    return prompt


def _format_context_suffix(opponent_msgs: list[dict], system_notices: list[dict]) -> str:
    """Build the trailing context lines showing opponent's last speech and any system notices."""
    parts = []
    if opponent_msgs:
        parts.append(f"The other party said: \"{opponent_msgs[-1]['message']}\"")
    for notice in system_notices:
        parts.append(f"[System: {notice['message']}]")
    return "\n\n" + "\n".join(parts) if parts else ""


def _cheap_talk_thinking(
    turn_number: int,
    config: dict,
    opponent_msgs: list[dict],
    system_notices: list[dict],
    round_number: int = 1,
) -> str:
    is_shifting = config.get("is_shifting", False)
    if turn_number == 0:
        num_rounds = config.get("num_rounds", 1)
        turns_info = "as much as required" if config['cheap_talk_turns'] == 0 else f"{config['cheap_talk_turns']} exchanges this round"
        if is_shifting:
            header = "--- CHEAP TALK PHASE ---"
        else:
            header = f"--— CHEAP TALK PHASE (Round {round_number}/{num_rounds}) ---"
        prompt = (
            f"{header}\n"
            f"Exchange messages with the other party ({turns_info}).\n"
            f"Respond with JSON. Set \"action\" to null to keep chatting, or to a purchase object to finalize."
        )
        prompt += _format_context_suffix(opponent_msgs, system_notices)
    else:
        suffix = _format_context_suffix(opponent_msgs, system_notices)
        if suffix:
            prompt = suffix.lstrip("\n") + "\n\nRespond with JSON."
        else:
            prompt = "Continue the conversation. Respond with JSON."
    return prompt


def _cheap_talk_no_thinking(
    turn_number: int,
    config: dict,
    opponent_msgs: list[dict],
    system_notices: list[dict],
    round_number: int = 1,
) -> str:
    is_shifting = config.get("is_shifting", False)
    if turn_number == 0:
        num_rounds = config.get("num_rounds", 1)
        turns_info = "as much as required" if config['cheap_talk_turns'] == 0 else f"{config['cheap_talk_turns']} exchanges this round"
        if is_shifting:
            header = "--- CHEAP TALK PHASE ---"
        else:
            header = f"--— CHEAP TALK PHASE (Round {round_number}/{num_rounds}) ---"
        prompt = (
            f"{header}\n"
            f"Exchange messages with the other party ({turns_info}).\n"
            f"Send your opening message. Or end the round early by responding with ONLY a JSON purchase."
        )
        prompt += _format_context_suffix(opponent_msgs, system_notices)
    else:
        suffix = _format_context_suffix(opponent_msgs, system_notices)
        if suffix:
            prompt = suffix.lstrip("\n") + "\n\nRespond with your message, or submit a JSON purchase to finalize."
        else:
            prompt = "Continue the conversation, or submit a JSON purchase to finalize."
    return prompt


# ---------------------------------------------------------------------------
# Decision phase prompts
# ---------------------------------------------------------------------------


def decision_prompt(
    game_config_public: dict,
    thinking_enabled: bool,
    agent_id: str | None = None,
    conversation_so_far: list[dict] | None = None,
) -> str:
    """Return the user-message prompt for the decision phase."""
    max_types = game_config_public["max_resource_types_per_turn"]
    budget = game_config_public["agent_budget"]
    use_projects = game_config_public.get("use_projects", False)
    projects = game_config_public.get("own_projects", [])
    pnames = _project_names(projects)
    example = _decision_example(pnames)
    context_prefix = ""
    if agent_id is not None and conversation_so_far:
        opponent_msgs = [
            m for m in conversation_so_far
            if m["speaker"] not in (agent_id, "system") and m.get("type") == "speech"
        ]
        system_notices = [m for m in conversation_so_far if m["speaker"] == "system"]
        context_suffix = _format_context_suffix(opponent_msgs, system_notices).lstrip("\n")
        if context_suffix:
            context_prefix = f"{context_suffix}\n\n"

    if thinking_enabled:
        if use_projects:
            return (
                f"{context_prefix}"
                f"--- DECISION PHASE ---\n"
                f"Now submit your resource purchases and project allocations.\n"
                f"Choose at most {max_types} resource types. "
                f"Total cost must not exceed ${budget}.\n"
                f"Respond with JSON. You MUST set \"action\" to your purchase object "
                f"(include \"projects\" to specify runs)."
            )
        return (
            f"{context_prefix}"
            f"--- DECISION PHASE ---\n"
            f"Now submit your resource purchases.\n"
            f"Choose at most {max_types} resource types. "
            f"Total cost must not exceed ${budget}.\n"
            f"Respond with JSON. You MUST set \"action\" to your purchase object."
        )
    else:
        if use_projects:
            return (
                f"{context_prefix}"
                f"--- DECISION PHASE ---\n"
                f"Now submit your resource purchases and project allocations.\n"
                f"Choose at most {max_types} resource types. "
                f"Total cost must not exceed ${budget}.\n"
                f"Respond with ONLY a JSON object, e.g. "
                f"{example}"
            )
        return (
            f"{context_prefix}"
            f"--- DECISION PHASE ---\n"
            f"Now submit your resource purchases.\n"
            f"Choose at most {max_types} resource types. "
            f"Total cost must not exceed ${budget}.\n"
            f"Respond with ONLY a JSON object, e.g. {{\"wood\": 3, \"gold\": 1}}"
        )


# ---------------------------------------------------------------------------
# Round result notifications
# ---------------------------------------------------------------------------


def round_result_message(
    own_allocation: dict,
    own_reward: float,
    opponent_allocation: dict,
    opponent_reward: float,
    overdrawn: bool,
    visible_opponent_reward: bool = True,
    project_details: dict | None = None,
) -> str:
    """Build the user-message notification sent after a round completes."""
    if overdrawn:
        return (
            f"--- Round result: ANNULLED (total demand exceeded supply). "
            f"Both parties receive 0 reward. "
            f"Your bid: {json.dumps(own_allocation)}. Opponent bid: {json.dumps(opponent_allocation)}. ---"
        )

    if project_details:
        runs_str = ", ".join(f"{k} x{v}" for k, v in project_details["runs"].items() if v > 0)
        if runs_str:
            own_detail = (
                f"You purchased {json.dumps(own_allocation)}, "
                f"ran projects [{runs_str}], "
                f"and earned reward = {own_reward}"
            )
        else:
            own_detail = (
                f"You purchased {json.dumps(own_allocation)}, "
                f"but could not complete any project runs (insufficient resources for any project), "
                f"reward = 0"
            )
        if project_details.get("synergy_bonus", 0) > 0:
            own_detail += f" (base: {project_details['base_reward']}, synergy: +{project_details['synergy_bonus']})"
    else:
        own_detail = f"You purchased {json.dumps(own_allocation)} and earned reward = {own_reward}"

    if visible_opponent_reward:
        return (
            f"--- Round result: {own_detail}. "
            f"Opponent purchased {json.dumps(opponent_allocation)} and earned reward = {opponent_reward}. ---"
        )
    else:
        return (
            f"--- Round result: {own_detail}. "
            f"Opponent purchased {json.dumps(opponent_allocation)}. ---"
        )


# ---------------------------------------------------------------------------
# Post-environment reflection prompt
# ---------------------------------------------------------------------------


def reflection_prompt(
    agent_id: str,
    total_rounds: int,
    own_cumulative_reward: float,
    opponent_cumulative_reward: float,
    visible_opponent_reward: bool = True,
    theoretical_joint_max: float | None = None,
) -> str:
    """Build the post-environment reflection prompt sent after all rounds complete.

    This leverages cached tokens from the full environment conversation to efficiently
    extract learnings that can improve performance in future games.
    """
    if visible_opponent_reward:
        joint_actual = own_cumulative_reward + opponent_cumulative_reward
        outcome = (
            f"Your cumulative reward: {own_cumulative_reward}. "
            f"Opponent's cumulative reward: {opponent_cumulative_reward}. "
            f"Joint total: {joint_actual}."
        )
        if theoretical_joint_max is not None:
            expected_individual = theoretical_joint_max / 2.0
            individual_efficiency = (own_cumulative_reward / expected_individual * 100) if expected_individual > 0 else 0
            joint_efficiency = (joint_actual / theoretical_joint_max * 100) if theoretical_joint_max > 0 else 0
            outcome += (
                f"\nExpected individual reward (optimal collaboration): {expected_individual:.1f}. "
                f"Your efficiency: {individual_efficiency:.1f}%. Joint efficiency: {joint_efficiency:.1f}%."
            )
    else:
        outcome = f"Your cumulative reward: {own_cumulative_reward}."
        if theoretical_joint_max is not None:
            expected_individual = theoretical_joint_max / 2.0
            outcome += f"\nExpected individual reward (optimal collaboration): {expected_individual:.1f}."

    return (
        f"--- GAME COMPLETE ({total_rounds} rounds) ---\n"
        f"{outcome}\n\n"
        f"Reflect on the environment and summarize key learnings that could help you "
        f"achieve better outcomes in future games. Consider:\n"
        f"- What strategies worked well or poorly?\n"
        f"- How effective was your communication and negotiation approach?\n"
        f"- What would you do differently next time?\n"
        f"- Any patterns you noticed in resource allocation or opponent behavior?\n"
        f"- How close did you get to the theoretical optimum?\n\n"
        f"Provide a concise reflection (2-4 sentences) focusing on actionable insights."
    )
