"""
Human player agent.

Awaits input from the browser via HTTP POST. The server resolves the
pending Future when input arrives from the human client.
"""

import asyncio
import json


def _format_projects(projects: list[dict]) -> str:
    if not projects:
        return "  No projects are configured."
    lines = []
    for project in projects:
        requirements = ", ".join(
            f"{resource} x{quantity}"
            for resource, quantity in project.get("requirements", {}).items()
        )
        lines.append(
            f"  - {project.get('name', 'project')}: requires [{requirements}], "
            f"reward = {project.get('reward', '?')}/run"
        )
    return "\n".join(lines)


def _format_resource_table(resources: list[str], costs: dict, supply: dict) -> str:
    lines = []
    for resource in resources:
        cost = costs.get(resource, "?")
        available = supply.get(resource, "?")
        lines.append(f"  - {resource}: costs ${cost}, shared supply {available}")
    return "\n".join(lines)


def _format_synergy(scenario_synergy: dict | None) -> str:
    if not scenario_synergy:
        return ""
    resource = scenario_synergy["resource"]
    threshold = scenario_synergy["threshold"]
    bonus = scenario_synergy["bonus"]
    return (
        "\n**Synergy bonus:** If both players buy at least 1 "
        f"{resource} and combined {resource} purchases reach {threshold}, "
        f"each player earns a flat +{bonus} bonus."
    )


def _format_optional_guidance(game_config_public: dict) -> str:
    guidance = []
    if game_config_public.get("share_projects"):
        guidance.append(
            "- At the start of cheap talk, share your project names, requirements, "
            "and rewards, then ask the other player to share theirs."
        )
    if game_config_public.get("think_about_opponent"):
        guidance.append(
            "- During negotiation, reason about what the other player may need and "
            "how that affects resource conflict or coordination."
        )
    if game_config_public.get("maximize_joint"):
        guidance.append(
            "- Your assigned objective is to maximize joint reward, not only your own score."
        )
    return "\n".join(guidance)


def _coerce_positive_int(value) -> int | None:
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return None
    return quantity if quantity > 0 else None


class HumanAgent:
    def __init__(self):
        self._pending: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._system_prompt = None

    def _init_session(self, agent_id, game_config_public):
        """Called by engine at game start to set up the system prompt."""
        resources = game_config_public.get("resource_types", ["wood", "stone", "gold"])
        costs = game_config_public.get("resource_costs", {})
        supply = game_config_public.get("resource_supply", {})
        budget = game_config_public.get("agent_budget", 15)
        max_types = game_config_public.get("max_resource_types_per_turn", 2)
        num_rounds = game_config_public.get("num_rounds", 3)
        own_projects = game_config_public.get("own_projects", [])
        opponent_projects = game_config_public.get("opponent_projects", [])
        opponent_section = ""
        if opponent_projects:
            opponent_section = (
                "\n**Other player's projects:**\n"
                f"{_format_projects(opponent_projects)}\n"
            )
        optional_guidance = _format_optional_guidance(game_config_public)
        optional_section = f"\n**Round guidance:**\n{optional_guidance}\n" if optional_guidance else ""
        cheap_talk_enabled = game_config_public.get("enable_cheap_talk", True)
        if cheap_talk_enabled:
            talk_mechanics = (
                "- Each round has a cheap talk phase where you can negotiate with your opponent\n"
                "- After negotiation, both players simultaneously select which resources to purchase"
            )
            talk_tools = "- During cheap talk: send messages or make an early decision using the order sliders\n"
        else:
            talk_mechanics = (
                "- There is no communication in this game: each round, both players "
                "simultaneously select which resources to purchase"
            )
            talk_tools = ""

        self._system_prompt = f"""You are playing a {num_rounds}-round resource allocation negotiation game.

**Your objective:** Maximize your cumulative reward across all rounds.

**Game mechanics:**
{talk_mechanics}
- Your budget: ${budget} per round
- Max resource types per purchase: {max_types}
- If total demand for any resource exceeds supply, both players score 0 for that round (overdraw)

**Resources available:**
{_format_resource_table(resources, costs, supply)}

**Your projects:**
{_format_projects(own_projects)}
{opponent_section}{_format_synergy(game_config_public.get("scenario_synergy"))}

**Your tools:**
{talk_tools}- During decision phase: select project runs and purchase quantities
- Budget and resource type limits are enforced automatically — invalid orders cannot be submitted
- Project selections are submitted with your resource purchase, for example:
  {{"wood": 3, "gold": 1, "projects": {{"{own_projects[0]['name'] if own_projects else 'project_a'}": 1}}}}
{optional_section}

**Strategy tips:**
- Your project list is private unless you choose to reveal it or transparency is enabled
- Coordinate to avoid overdraw, but remember: the opponent may have different incentives
- Consider making early decisions to signal commitment or gain strategic advantage
"""

    def submit_input(self, text: str):
        """Called from the server thread when a WS message arrives."""
        if self._pending and not self._pending.done() and self._loop:
            self._loop.call_soon_threadsafe(self._pending.set_result, text)

    async def _wait_for_input(self) -> str:
        self._loop = asyncio.get_running_loop()
        self._pending = self._loop.create_future()
        return await self._pending

    async def cheap_talk(self, agent_id, round_number, turn_number,
                         game_config_public, conversation_so_far, own_memory, **kwargs) -> str:
        return await self._wait_for_input()

    async def decide_allocation(self, agent_id, round_number,
                                game_config_public, cheap_talk_transcript,
                                own_memory) -> dict:
        text = await self._wait_for_input()
        parsed = json.loads(text)
        allocation = {}
        for key, value in parsed.items():
            if key == "projects" and isinstance(value, dict):
                project_runs = {}
                for project, runs in value.items():
                    quantity = _coerce_positive_int(runs)
                    if quantity is not None:
                        project_runs[project] = quantity
                if project_runs:
                    allocation[key] = project_runs
                continue
            quantity = _coerce_positive_int(value)
            if quantity is not None:
                allocation[key] = quantity
        return allocation

    def notify_round_result(self, round_number, own_allocation, own_reward,
                            opponent_allocation, opponent_reward, overdrawn,
                            visible_opponent_reward=True, project_details=None):
        pass  # Human sees results in the UI
