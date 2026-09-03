"""
Multi-Agent Negotiation Environment Engine

Agents negotiate over scarce resources with project-based rewards.
Includes a cheap-talk phase before each purchasing decision.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable
from enum import Enum

log = logging.getLogger("negotiation")

from negotiation_game.backend.defaults import (
    MAX_DECISION_RETRIES,
    DEFAULT_RESOURCE_TYPES,
    DEFAULT_RESOURCE_SUPPLY,
    DEFAULT_RESOURCE_COSTS,
    DEFAULT_AGENT_BUDGET,
    DEFAULT_NUM_ROUNDS,
    DEFAULT_CHEAP_TALK_TURNS,
    DEFAULT_MAX_RESOURCE_TYPES_PER_TURN,
    DEFAULT_PROJECTS_A,
    DEFAULT_PROJECTS_B,
)
from negotiation_game.backend.scenario_pool import ScenarioPool


class GameMode(str, Enum):
    STABLE = "stable"        # All value functions stay fixed
    SHIFTING = "shifting"    # Agent(s) with shifting=True get randomized each round


@dataclass
class SynergyCondition:
    """Synergy bonus condition for a project."""
    resource: str
    threshold: int
    bonus: int


@dataclass
class Project:
    """A project that an agent can run by allocating resources."""
    name: str                          # e.g. "project_a", "project_b"
    requirements: dict[str, int]       # resource: qty_per_run
    reward: int                        # base reward per run


class ProjectRewardCalculator:
    """Computes rewards from resource purchases and project run assignments."""

    def __init__(self, projects: list[Project], resource_costs: dict[str, float]):
        self.projects = projects
        self.resource_costs = resource_costs

    def compute_cost_per_run(self, project: Project) -> float:
        return sum(self.resource_costs.get(r, 0) * qty for r, qty in project.requirements.items())

    def max_runs_from_resources(self, project: Project, available: dict[str, int]) -> int:
        """Max runs of a project given available resources."""
        max_r = float('inf')
        for res, qty_per_run in project.requirements.items():
            if qty_per_run <= 0:
                continue
            max_r = min(max_r, available.get(res, 0) // qty_per_run)
        return int(max_r) if max_r != float('inf') else 0

    def minimum_resources_for_runs(self, requested_runs: dict[str, int]) -> dict[str, int]:
        """Compute the minimum resource purchase needed to execute the requested project runs."""
        resources: dict[str, int] = {}
        for proj in self.projects:
            n = requested_runs.get(proj.name, 0)
            if n <= 0:
                continue
            for res, qty_per_run in proj.requirements.items():
                resources[res] = resources.get(res, 0) + qty_per_run * n
        return resources

    def greedy_assign(self, purchased: dict[str, int]) -> dict[str, int]:
        """Greedily assign purchased resources to projects in order."""
        remaining = dict(purchased)
        runs = {}
        for proj in self.projects:
            n = self.max_runs_from_resources(proj, remaining)
            if n > 0:
                runs[proj.name] = n
                for res, qty in proj.requirements.items():
                    remaining[res] = remaining.get(res, 0) - qty * n
        return runs

    def resolve_project_runs(self, purchased: dict[str, int],
                             requested_runs: dict[str, int] | None) -> tuple[dict[str, int], bool]:
        """Resolve final project runs from purchase + agent's requested runs.

        If requested_runs is provided, validate against purchased resources.
        If runs exceed what's feasible, clamp. Then greedily assign any
        remaining resources to projects that weren't fully specified.

        Returns:
            (project_runs, auto_allocated) where auto_allocated is True if
            greedy assignment was used (no explicit project runs provided).
        """
        if not requested_runs:
            return self.greedy_assign(purchased), True

        remaining = dict(purchased)
        final_runs = {}

        # First pass: honor explicit requests (clamped to feasible)
        for proj in self.projects:
            requested = requested_runs.get(proj.name, 0)
            if requested <= 0:
                continue
            feasible = self.max_runs_from_resources(proj, remaining)
            actual = min(requested, feasible)
            if actual > 0:
                final_runs[proj.name] = actual
                for res, qty in proj.requirements.items():
                    remaining[res] = remaining.get(res, 0) - qty * actual

        # Second pass: greedily assign remaining resources to all projects
        for proj in self.projects:
            n = self.max_runs_from_resources(proj, remaining)
            if n > 0:
                final_runs[proj.name] = final_runs.get(proj.name, 0) + n
                for res, qty in proj.requirements.items():
                    remaining[res] = remaining.get(res, 0) - qty * n

        return final_runs, False

    def compute_reward(self, project_runs: dict[str, int],
                       purchased: dict[str, int],
                       opponent_purchased: dict[str, int] | None = None,
                       scenario_synergy: dict | None = None) -> dict:
        """Compute reward from resolved project runs.

        Returns dict with: total_reward, base_reward, synergy_bonus, runs.
        Synergy is scenario-level: flat bonus if both players buy ≥1 of resource
        AND combined ≥ threshold.
        """
        base_reward = 0.0
        synergy_bonus = 0.0

        for proj in self.projects:
            runs = project_runs.get(proj.name, 0)
            if runs <= 0:
                continue
            base_reward += proj.reward * runs

        # Scenario-level synergy check
        if scenario_synergy and opponent_purchased:
            res = scenario_synergy["resource"]
            my_total = purchased.get(res, 0)
            opp_total = opponent_purchased.get(res, 0)
            if (my_total >= 1 and opp_total >= 1
                    and my_total + opp_total >= scenario_synergy["threshold"]):
                synergy_bonus = scenario_synergy["bonus"]

        return {
            "runs": project_runs,
            "base_reward": base_reward,
            "synergy_bonus": synergy_bonus,
            "total_reward": base_reward + synergy_bonus,
        }

    def compute_resource_demand(self, project_runs: dict[str, int]) -> dict[str, int]:
        """Compute total resource demand from project runs."""
        demand = {}
        for proj in self.projects:
            runs = project_runs.get(proj.name, 0)
            for res, qty in proj.requirements.items():
                demand[res] = demand.get(res, 0) + qty * runs
        return demand


@dataclass
class AgentState:
    agent_id: str
    budget: float
    project_calculator: ProjectRewardCalculator | None = None
    cumulative_reward: float = 0.0
    memory: list = field(default_factory=list)


@dataclass
class RoundResult:
    round_number: int
    agent_a_allocation: dict[str, int]
    agent_b_allocation: dict[str, int]
    total_demanded: dict[str, int]
    resource_supply: dict[str, int]
    overdrawn: bool
    agent_a_reward: float
    agent_b_reward: float
    agent_a_project_runs: dict | None = None
    agent_b_project_runs: dict | None = None
    cheap_talk_transcript: list = field(default_factory=list)


def _projects_from_dicts(project_dicts: list[dict]) -> list[Project]:
    """Convert list of project dicts to Project dataclass instances."""
    projects = []
    for pd in project_dicts:
        projects.append(Project(
            name=pd["name"],
            requirements=dict(pd["requirements"]),
            reward=pd["reward"],
        ))
    return projects


@dataclass
class GameConfig:
    episode_uid: str = ""
    resource_types: list[str] = field(default_factory=lambda: list(DEFAULT_RESOURCE_TYPES))
    resource_supply: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RESOURCE_SUPPLY))
    resource_costs: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RESOURCE_COSTS))
    agent_budget: float = DEFAULT_AGENT_BUDGET
    num_rounds: int = DEFAULT_NUM_ROUNDS
    cheap_talk_turns: int = DEFAULT_CHEAP_TALK_TURNS
    max_resource_types_per_turn: int = DEFAULT_MAX_RESOURCE_TYPES_PER_TURN
    mode: GameMode = GameMode.STABLE

    # Per-agent projects (list of 2 lists of project dicts)
    agent_projects: list[list[dict]] = field(
        default_factory=lambda: [list(DEFAULT_PROJECTS_A), list(DEFAULT_PROJECTS_B)]
    )

    # Oracle statistics from SA solver
    oracle_stats: dict | None = None

    # Scenario-level synergy: {resource, threshold, bonus}
    scenario_synergy: dict | None = None

    agent_shifting: list[bool] = field(default_factory=lambda: [False, False])

    # Who speaks first: 0 = agents[0], 1 = agents[1]
    first_speaker: int = 0

    # Optional goal override
    goal: str = ""

    # Whether LLM agents use structured thinking (True) or all-public speech (False)
    thinking: bool = True

    # Whether agents can see their own value function / projects
    visible_utilities: bool = False

    # Whether agents see the opponent's reward after each round (allocations always visible)
    visible_opponent_reward: bool = True

    # Whether cheap talk is enabled (False = no-talk baseline, agents go directly to decision)
    enable_cheap_talk: bool = True

    # Whether agents are instructed to share project details during cheap talk
    share_projects: bool = False

    # Whether agents are prompted to reason about opponent's goals and projects
    think_about_opponent: bool = False

    # Whether agents are instructed to maximize joint (combined) reward instead of individual reward
    maximize_joint: bool = False

    # Whether both agents' projects are fully disclosed in each other's system prompt
    full_transparency: bool = False

    # Whether projects use themed names instead of generic project_a/b/c
    named_projects: bool = False

    # Random seed for reproducibility (shifting values, heuristic agents)
    seed: int | None = None

    # Whether turn order was swapped (cell metadata)
    swapped: bool = False

    # Scenario pool for per-round project rotation
    scenario_pool_path: str | None = None
    target_mc_ratio: float | None = None
    rotate_projects: bool = False

    # Experiment tracking metadata
    episode_id: str | None = None
    experiment_name: str | None = None
    git_hash: str | None = None

    def __post_init__(self):
        if not self.episode_uid:
            self.episode_uid = str(uuid.uuid4())[:8]
        # Always show utilities (agents need to know their projects),
        # never show opponent rewards (allocations still visible)
        self.visible_utilities = True
        self.visible_opponent_reward = False


class GameEngine:
    """Runs a full negotiation environment between two agents."""

    def __init__(self, config: GameConfig, agent_a, agent_b):
        self.config = config
        fs = config.first_speaker
        self.agent_a_impl = agent_a
        self.agent_b_impl = agent_b

        self._shifting_a = config.agent_shifting[fs] if len(config.agent_shifting) > fs else False
        self._shifting_b = config.agent_shifting[1 - fs] if len(config.agent_shifting) > (1 - fs) else False

        self.results: list[RoundResult] = []

        a_proj_dicts = config.agent_projects[fs] if len(config.agent_projects) > fs else DEFAULT_PROJECTS_A
        b_proj_dicts = config.agent_projects[1 - fs] if len(config.agent_projects) > (1 - fs) else DEFAULT_PROJECTS_B
        a_projects = _projects_from_dicts(a_proj_dicts)
        b_projects = _projects_from_dicts(b_proj_dicts)

        self.agent_a_state = AgentState(
            agent_id="agent_a",
            budget=config.agent_budget,
            project_calculator=ProjectRewardCalculator(a_projects, config.resource_costs),
        )
        self.agent_b_state = AgentState(
            agent_id="agent_b",
            budget=config.agent_budget,
            project_calculator=ProjectRewardCalculator(b_projects, config.resource_costs),
        )

        self._event_callbacks: list[Callable] = []
        self._stopped = False
        self._api_failures = {"agent_a": 0, "agent_b": 0}
        self._scenario_pool = None
        self._theme = None
        self._per_round_scenarios: list[dict] = []
        self._used_candidate_ids: list[str] = []

        # Initialize scenario pool if configured
        if config.scenario_pool_path:
            self._scenario_pool = ScenarioPool(config.scenario_pool_path)
            self._theme = self._scenario_pool.pick_theme(seed=config.seed)
            res_cfg = self._scenario_pool.get_resource_config(self._theme)
            config.resource_types = res_cfg["resource_types"]
            config.resource_costs = res_cfg["resource_costs"]
            config.resource_supply = res_cfg["resource_supply"]
            config.agent_budget = res_cfg["agent_budget"]
            # Sample initial split
            self._apply_pool_split(config)

    def _apply_pool_split(self, config: GameConfig):
        """Sample a split from the pool and apply theme, updating config and calculators."""
        mc = config.target_mc_ratio or 0.8
        split = self._scenario_pool.sample_split(mc, exclude=self._used_candidate_ids or None, seed=config.seed)
        if split is None:
            log.warning("No scenario available for M/C=%.1f, keeping current projects", mc)
            return
        themed = self._scenario_pool.apply_theme(split, self._theme, named_projects=config.named_projects)
        config.agent_projects = themed["agent_projects"]
        if "oracle_stats" in split:
            config.oracle_stats = split["oracle_stats"]

        # Track used candidate_ids to avoid repeats
        candidate_id = split.get("candidate_id", "unknown")
        self._used_candidate_ids.append(candidate_id)
        self._per_round_scenarios.append({
            "candidate_id": candidate_id,
            "mc_bucket": split.get("mc_bucket"),
            "agent_projects": themed["agent_projects"],
            "oracle_stats": split.get("oracle_stats"),
        })

        # Rebuild project calculators
        fs = config.first_speaker
        a_proj_dicts = config.agent_projects[fs] if len(config.agent_projects) > fs else []
        b_proj_dicts = config.agent_projects[1 - fs] if len(config.agent_projects) > (1 - fs) else []
        self.agent_a_state.project_calculator = ProjectRewardCalculator(
            _projects_from_dicts(a_proj_dicts), config.resource_costs
        )
        self.agent_b_state.project_calculator = ProjectRewardCalculator(
            _projects_from_dicts(b_proj_dicts), config.resource_costs
        )

    def _update_projects_for_round(self, round_num: int):
        """If rotate_projects is enabled, sample new projects for this round."""
        if not self.config.rotate_projects or not self._scenario_pool:
            return
        if round_num == 1:
            return  # Already sampled in __init__

        self._apply_pool_split(self.config)

    def _is_human(self, agent_impl) -> bool:
        return hasattr(agent_impl, 'submit_input')

    def stop(self):
        self._stopped = True

    def on_event(self, callback: Callable):
        self._event_callbacks.append(callback)

    async def _emit(self, event_type: str, data: dict):
        for cb in self._event_callbacks:
            await cb(event_type, data)

    def _set_stream_callback(self, agent_impl, agent_id: str, round_number: int,
                                phase: str, turn: int = -1):
        """Set a streaming callback on an LLM agent to emit llm_streaming event on first token."""
        if not hasattr(agent_impl, '_on_stream_start'):
            return
        emitted = False

        async def on_stream_start():
            nonlocal emitted
            if not emitted:
                emitted = True
                await self._emit("llm_streaming", {
                    "agent": agent_id,
                    "round": round_number,
                    "phase": phase,
                    "turn": turn,
                })

        agent_impl._on_stream_start = on_stream_start

    def _get_api_meta(self, agent_impl) -> dict:
        meta = getattr(agent_impl, 'last_api_meta', None)
        if meta:
            return {k: v for k, v in meta.items() if v is not None}
        return {}

    async def _emit_thinking(self, agent_impl, agent_id: str, round_number: int,
                             transcript: list | None = None, turn: int = -1):
        thinking = getattr(agent_impl, 'last_thinking', "")
        api_meta = self._get_api_meta(agent_impl)
        if thinking:
            event_data = {
                "agent": agent_id,
                "round": round_number,
                "turn": turn,
                "content": thinking,
            }
            if api_meta:
                event_data["api_meta"] = api_meta
            await self._emit("thinking", event_data)
            if transcript is not None:
                entry = {
                    "speaker": agent_id,
                    "turn": turn,
                    "type": "thinking",
                    "message": thinking,
                }
                if api_meta:
                    entry["api_meta"] = api_meta
                transcript.append(entry)
            agent_impl.last_thinking = ""

    async def _emit_reasoning(self, agent_impl, agent_id: str, round_number: int,
                              transcript: list | None = None, turn: int = -1):
        """Emit reasoning summary from LLM API (o-series, Claude extended thinking, etc.)."""
        reasoning = getattr(agent_impl, 'last_reasoning', "")
        api_meta = self._get_api_meta(agent_impl)
        if reasoning:
            event_data = {
                "agent": agent_id,
                "round": round_number,
                "turn": turn,
                "content": reasoning,
            }
            if api_meta:
                event_data["api_meta"] = api_meta
            await self._emit("reasoning", event_data)
            if transcript is not None:
                entry = {
                    "speaker": agent_id,
                    "turn": turn,
                    "type": "reasoning",
                    "message": reasoning,
                }
                if api_meta:
                    entry["api_meta"] = api_meta
                transcript.append(entry)
            agent_impl.last_reasoning = ""

    async def _emit_api_failure(self, agent_impl, agent_id: str,
                               round_number: int, phase: str, turn: int = -1):
        api_meta = self._get_api_meta(agent_impl)
        if not api_meta or "error_type" not in api_meta:
            return
        self._api_failures[agent_id] = self._api_failures.get(agent_id, 0) + 1
        await self._emit("api_failure", {
            "agent": agent_id,
            "round": round_number,
            "phase": phase,
            "turn": turn if phase == "cheap_talk" else -1,
            "retry_count": api_meta.get("retry_count", 0),
            "error_type": api_meta.get("error_type", "unknown"),
            "error_message": api_meta.get("error_message", ""),
        })
        if api_meta.get("used_fallback"):
            await self._emit("heuristic_fallback", {
                "agent": agent_id,
                "round": round_number,
                "phase": phase,
                "turn": turn if phase == "cheap_talk" else -1,
            })

    def _public_config(self, agent_id: str = "") -> dict:
        is_shifting = (
            (agent_id == "agent_a" and self._shifting_a) or
            (agent_id == "agent_b" and self._shifting_b)
        )
        opponent_is_shifting = (
            (agent_id == "agent_a" and self._shifting_b) or
            (agent_id == "agent_b" and self._shifting_a)
        )
        cfg = {
            "resource_types": self.config.resource_types,
            "resource_supply": self.config.resource_supply,
            "resource_costs": self.config.resource_costs,
            "agent_budget": self.config.agent_budget,
            "max_resource_types_per_turn": self.config.max_resource_types_per_turn,
            "num_rounds": self.config.num_rounds,
            "cheap_talk_turns": self.config.cheap_talk_turns,
            "enable_cheap_talk": self.config.enable_cheap_talk,
        }
        if self.config.goal:
            cfg["goal"] = self.config.goal
        cfg["thinking"] = self.config.thinking
        if is_shifting:
            cfg["is_shifting"] = True
        if opponent_is_shifting:
            cfg["opponent_shifts"] = True

        cfg["use_projects"] = True
        cfg["share_projects"] = self.config.share_projects
        cfg["think_about_opponent"] = self.config.think_about_opponent
        cfg["maximize_joint"] = self.config.maximize_joint
        cfg["full_transparency"] = self.config.full_transparency
        cfg["named_projects"] = self.config.named_projects
        if self.config.scenario_synergy:
            cfg["scenario_synergy"] = self.config.scenario_synergy
        if self.config.visible_utilities and agent_id:
            own_state = self.agent_a_state if agent_id == "agent_a" else self.agent_b_state
            opp_state = self.agent_b_state if agent_id == "agent_a" else self.agent_a_state
            if own_state.project_calculator:
                cfg["own_projects"] = [
                    {
                        "name": p.name,
                        "requirements": p.requirements,
                        "reward": p.reward,
                    }
                    for p in own_state.project_calculator.projects
                ]
            if self.config.full_transparency and opp_state.project_calculator:
                cfg["opponent_projects"] = [
                    {
                        "name": p.name,
                        "requirements": p.requirements,
                        "reward": p.reward,
                    }
                    for p in opp_state.project_calculator.projects
                ]

        return cfg

    def _compute_round_stats(self, transcript: list[dict], cheap_talk_turns: int) -> dict:
        # Only count speech messages (exclude thinking and synthetic decision entries)
        speech_msgs = [e for e in transcript if e.get("type") == "speech"]
        a_msgs = [e for e in speech_msgs if e.get("speaker") == "agent_a"]
        b_msgs = [e for e in speech_msgs if e.get("speaker") == "agent_b"]

        # Use all public messages (including decisions) for turn counting
        public_msgs = [e for e in transcript if e.get("type") != "thinking"]
        max_turn = max((e.get("turn", 0) for e in public_msgs), default=0)
        num_turns_used = max_turn + 1
        return {
            "num_turns_used": num_turns_used,
            "early_submit": num_turns_used < cheap_talk_turns if cheap_talk_turns > 0 else False,
            "agent_a_msg_count": len(a_msgs),
            "agent_b_msg_count": len(b_msgs),
            "agent_a_char_count": sum(len(e.get("message", "")) for e in a_msgs),
            "agent_b_char_count": sum(len(e.get("message", "")) for e in b_msgs),
            "total_char_count": sum(len(e.get("message", "")) for e in speech_msgs),
        }

    def _validate_allocation(self, allocation: dict[str, int], agent_id: str) -> tuple[bool, str]:
        """Validate resource purchase (budget, max types, non-negative)."""
        costs = self.config.resource_costs
        budget = self.config.agent_budget

        nonzero = {r: q for r, q in allocation.items() if q > 0}
        if len(nonzero) > self.config.max_resource_types_per_turn:
            return False, f"{agent_id} selected more than {self.config.max_resource_types_per_turn} resource types"

        total_cost = sum(costs.get(r, 0) * q for r, q in allocation.items())
        if total_cost > budget + 0.001:
            return False, f"{agent_id} exceeded budget: cost={total_cost}, budget={budget}"

        for r, q in allocation.items():
            if q < 0:
                return False, f"{agent_id} has negative quantity for {r}"

        return True, ""

    def _try_parse_purchase(self, text: str) -> tuple[dict[str, int] | None, dict[str, int] | None]:
        """Try to parse a response as a JSON purchase.

        Returns (resource_allocation, project_runs) or (None, None) if not JSON.
        project_runs may be None even if allocation is valid (agent didn't specify).
        """
        stripped = text.strip()
        if not stripped.startswith("{"):
            return None, None
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(stripped)
            if not isinstance(parsed, dict):
                return None, None

            # Extract project runs if present under "projects" key
            project_runs = None
            if "projects" in parsed:
                proj_raw = parsed.pop("projects")
                if isinstance(proj_raw, dict):
                    project_runs = {k: int(v) for k, v in proj_raw.items() if int(v) > 0}

            # Separate resource keys from project-name keys
            valid_resources = set(self.config.resource_types)
            alloc = {}
            inferred_project_runs = {}
            for k, v in parsed.items():
                if not isinstance(v, (int, float)):
                    continue
                iv = int(v)
                if iv <= 0:
                    continue
                if k in valid_resources:
                    alloc[k] = iv
                else:
                    # Key is not a resource — treat as a project run
                    inferred_project_runs[k] = iv

            # If we found project-like keys and no explicit project_runs, use inferred
            if inferred_project_runs and project_runs is None:
                project_runs = inferred_project_runs
                log.warning(
                    "Agent submitted project names as top-level keys instead of under 'projects': %s "
                    "(expected resource keys: %s). Treating as project runs.",
                    list(inferred_project_runs.keys()), list(valid_resources),
                )

            return alloc, project_runs
        except (json.JSONDecodeError, ValueError, TypeError):
            raise ValueError(f"Malformed JSON: {stripped[:100]}")

    async def _auto_fill_resources(self, alloc: dict[str, int], project_runs: dict[str, int] | None,
                                    agent_id: str) -> dict[str, int]:
        """If alloc is empty but project_runs are specified, infer minimum resources."""
        if alloc or not project_runs:
            return alloc
        state = self.agent_a_state if agent_id == "agent_a" else self.agent_b_state
        if not state.project_calculator:
            return alloc
        filled = state.project_calculator.minimum_resources_for_runs(project_runs)
        await self._emit("decision_auto_filled", {
            "agent": agent_id,
            "reason": "Agent submitted only project runs without resource purchases; auto-filled minimum resources.",
            "inferred_allocation": filled,
            "project_runs": project_runs,
        })
        await self._emit("output_format_warning", {
            "agent": agent_id,
            "warning": "Agent output contained project names instead of resource purchases. "
                       "Expected format: {\"wood\": N, \"projects\": {\"project_a\": M}}. "
                       "Got project keys at top level without resource allocations.",
            "raw_project_runs": project_runs,
        })
        return filled

    async def _request_decision_with_retries(self, agent_impl, agent_id: str,
                                              round_number: int, pub_config: dict,
                                              transcript: list, memory: list) -> tuple[dict[str, int], dict[str, int] | None]:
        """Call decide_allocation with retries on malformed JSON or validation errors.
        Returns (resource_allocation, project_runs).
        """
        for attempt in range(MAX_DECISION_RETRIES):
            alloc = await agent_impl.decide_allocation(
                agent_id, round_number, pub_config, transcript, memory,
            )
            # For project mode, parse project_runs from the allocation if embedded
            project_runs = None
            if isinstance(alloc, dict) and "projects" in alloc:
                proj_raw = alloc.pop("projects")
                if isinstance(proj_raw, dict):
                    project_runs = {k: int(v) for k, v in proj_raw.items() if int(v) > 0}
            alloc = await self._auto_fill_resources(alloc, project_runs, agent_id)
            valid, err = self._validate_allocation(alloc, agent_id)
            if valid:
                return alloc, project_runs
            error_msg = f"Invalid decision: {err}. Please try again with a valid JSON purchase."
            await self._emit("validation_error", {"agent": agent_id, "error": err, "attempt": attempt + 1})
            if hasattr(agent_impl, '_append_user'):
                agent_impl._append_user(error_msg)
        await self._emit("validation_error", {
            "agent": agent_id,
            "error": f"{agent_id} failed to submit a valid decision after {MAX_DECISION_RETRIES} attempts",
        })
        return {}, None

    async def run_round(self, round_number: int) -> RoundResult:
        self._update_projects_for_round(round_number)

        # Build per-agent project update text for rotating games (rounds > 1)
        project_update_a = None
        project_update_b = None
        if self.config.rotate_projects and round_number > 1:
            for label, state in [
                ("agent_a", self.agent_a_state),
                ("agent_b", self.agent_b_state),
            ]:
                if state.project_calculator:
                    projects = state.project_calculator.projects
                    proj_text = "\n".join(
                        f"  - {p.name}: requires [{', '.join(f'{r}×{q}' for r, q in p.requirements.items())}], reward = {p.reward}/run"
                        for p in projects
                    )
                    opp_state = self.agent_b_state if label == "agent_a" else self.agent_a_state
                    if self.config.full_transparency and opp_state.project_calculator:
                        opp_proj_text = "\n".join(
                            f"  - {p.name}: requires [{', '.join(f'{r}×{q}' for r, q in p.requirements.items())}], reward = {p.reward}/run"
                            for p in opp_state.project_calculator.projects
                        )
                        opponent_section = (
                            f"\nThe other party's projects have also changed. Their projects this round:\n"
                            f"{opp_proj_text}\n"
                        )
                    else:
                        opponent_section = f"\nThe other party has their own projects with different requirements and rewards.\n"
                    update = (
                        f"--- NEW PROJECTS FOR THIS ROUND ---\n"
                        f"Your projects have changed. These are your ONLY available projects this round:\n"
                        f"{proj_text}\n"
                        f"{opponent_section}"
                        f"All resource availability, costs, and your budget remain the same."
                    )
                    if label == "agent_a":
                        project_update_a = update
                    else:
                        project_update_b = update
                    await self._emit("project_instructions", {
                        "round": round_number,
                        "agent": label,
                        "prompt": update,
                    })
            await self._emit("projects_rotated", {"round": round_number})

        pub_config_a = self._public_config("agent_a")
        pub_config_b = self._public_config("agent_b")
        # Shifting agents always see round 1 (they think it's a one-shot environment)
        round_num_a = 1 if self._shifting_a else round_number
        round_num_b = 1 if self._shifting_b else round_number
        transcript = []
        alloc_a = None
        alloc_b = None
        proj_runs_a = None
        proj_runs_b = None

        # --- No-talk baseline: skip cheap talk ---
        if not self.config.enable_cheap_talk:
            await self._emit("phase_start", {"round": round_number, "phase": "decision"})
            # In no-talk mode, inject project updates directly before decision
            if project_update_a and hasattr(self.agent_a_impl, '_append_user'):
                self.agent_a_impl._append_user(project_update_a)
            if project_update_b and hasattr(self.agent_b_impl, '_append_user'):
                self.agent_b_impl._append_user(project_update_b)
        else:
            await self._emit("phase_start", {"round": round_number, "phase": "cheap_talk"})

            max_turns = self.config.cheap_talk_turns if self.config.cheap_talk_turns > 0 else 1000
            turn = 0
            while turn < max_turns:
                if alloc_a is not None and alloc_b is not None:
                    break

                # --- Agent A's turn ---
                if alloc_a is None:
                    if alloc_b is not None:
                        notice = {"speaker": "system", "type": "system", "turn": turn,
                                  "message": "Agent B has already made their final selection. Please submit your purchase as a JSON object."}
                        transcript.append(notice)
                        await self._emit("cheap_talk", {"round": round_number, **notice})

                    if self._is_human(self.agent_a_impl):
                        await self._emit("waiting_for_human", {
                            "agent": "agent_a",
                            "round": round_number,
                            "turn": turn,
                            "phase": "cheap_talk",
                            "public_config": pub_config_a,
                        })

                    self._set_stream_callback(self.agent_a_impl, "agent_a", round_number, "cheap_talk", turn)
                    msg_a = await self.agent_a_impl.cheap_talk(
                        "agent_a", round_num_a, turn, pub_config_a,
                        transcript, self.agent_a_state.memory,
                        project_update=project_update_a if turn == 0 else None,
                    )

                    api_meta = self._get_api_meta(self.agent_a_impl)
                    if api_meta and "error_type" in api_meta:
                        await self._emit_api_failure(self.agent_a_impl, "agent_a", round_number, "cheap_talk", turn)

                    await self._emit_thinking(self.agent_a_impl, "agent_a", round_number, transcript, turn=turn)
                    await self._emit_reasoning(self.agent_a_impl, "agent_a", round_number, transcript, turn=turn)

                    try:
                        parsed_alloc, parsed_proj = self._try_parse_purchase(msg_a)
                        if parsed_alloc is not None:
                            parsed_alloc = await self._auto_fill_resources(parsed_alloc, parsed_proj, "agent_a")
                            valid, err = self._validate_allocation(parsed_alloc, "agent_a")
                            if valid:
                                alloc_a = parsed_alloc
                                proj_runs_a = parsed_proj
                                # Emit the speech that accompanied the early decision
                                early_speech = getattr(self.agent_a_impl, 'last_speech', '')
                                if early_speech:
                                    speech_entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": early_speech}
                                    transcript.append(speech_entry)
                                    await self._emit("cheap_talk", {"round": round_number, **speech_entry})
                                entry = {"speaker": "agent_a", "type": "decision", "turn": turn,
                                         "message": f"[DECISION] {json.dumps(alloc_a)}"}
                                transcript.append(entry)
                                event_data = {"round": round_number, "agent": "agent_a", "allocation": alloc_a}
                                if proj_runs_a is not None:
                                    event_data["project_runs"] = proj_runs_a
                                await self._emit("early_decision", event_data)
                            else:
                                error_msg = f"Invalid purchase: {err}. Please fix and resubmit as JSON."
                                if hasattr(self.agent_a_impl, '_append_user'):
                                    self.agent_a_impl._append_user(error_msg)
                                await self._emit("validation_error", {"agent": "agent_a", "error": err})
                                entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                                transcript.append(entry)
                                await self._emit("cheap_talk", {"round": round_number, **entry})
                        else:
                            entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                            transcript.append(entry)
                            await self._emit("cheap_talk", {"round": round_number, **entry})
                    except ValueError:
                        error_msg = f"Your response looked like JSON but could not be parsed. Please send either a plain text message or a valid JSON purchase."
                        if hasattr(self.agent_a_impl, '_append_user'):
                            self.agent_a_impl._append_user(error_msg)
                        await self._emit("validation_error", {"agent": "agent_a", "error": "malformed JSON"})
                        entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                        transcript.append(entry)
                        await self._emit("cheap_talk", {"round": round_number, **entry})

                if alloc_a is not None and alloc_b is not None:
                    break

                # --- Agent B's turn ---
                if alloc_b is None:
                    if alloc_a is not None:
                        notice = {"speaker": "system", "type": "system", "turn": turn,
                                  "message": "Agent A has already made their final selection. Please submit your purchase as a JSON object."}
                        transcript.append(notice)
                        await self._emit("cheap_talk", {"round": round_number, **notice})

                    if self._is_human(self.agent_b_impl):
                        await self._emit("waiting_for_human", {
                            "agent": "agent_b",
                            "round": round_number,
                            "turn": turn,
                            "phase": "cheap_talk",
                            "public_config": pub_config_b,
                        })

                    self._set_stream_callback(self.agent_b_impl, "agent_b", round_number, "cheap_talk", turn)
                    msg_b = await self.agent_b_impl.cheap_talk(
                        "agent_b", round_num_b, turn, pub_config_b,
                        transcript, self.agent_b_state.memory,
                        project_update=project_update_b if turn == 0 else None,
                    )

                    api_meta = self._get_api_meta(self.agent_b_impl)
                    if api_meta and "error_type" in api_meta:
                        await self._emit_api_failure(self.agent_b_impl, "agent_b", round_number, "cheap_talk", turn)

                    await self._emit_thinking(self.agent_b_impl, "agent_b", round_number, transcript, turn=turn)
                    await self._emit_reasoning(self.agent_b_impl, "agent_b", round_number, transcript, turn=turn)

                    try:
                        parsed_alloc, parsed_proj = self._try_parse_purchase(msg_b)
                        if parsed_alloc is not None:
                            parsed_alloc = await self._auto_fill_resources(parsed_alloc, parsed_proj, "agent_b")
                            valid, err = self._validate_allocation(parsed_alloc, "agent_b")
                            if valid:
                                alloc_b = parsed_alloc
                                proj_runs_b = parsed_proj
                                early_speech = getattr(self.agent_b_impl, 'last_speech', '')
                                if early_speech:
                                    speech_entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": early_speech}
                                    transcript.append(speech_entry)
                                    await self._emit("cheap_talk", {"round": round_number, **speech_entry})
                                entry = {"speaker": "agent_b", "type": "decision", "turn": turn,
                                         "message": f"[DECISION] {json.dumps(alloc_b)}"}
                                transcript.append(entry)
                                event_data = {"round": round_number, "agent": "agent_b", "allocation": alloc_b}
                                if proj_runs_b is not None:
                                    event_data["project_runs"] = proj_runs_b
                                await self._emit("early_decision", event_data)
                            else:
                                error_msg = f"Invalid purchase: {err}. Please fix and resubmit as JSON."
                                if hasattr(self.agent_b_impl, '_append_user'):
                                    self.agent_b_impl._append_user(error_msg)
                                await self._emit("validation_error", {"agent": "agent_b", "error": err})
                                entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                                transcript.append(entry)
                                await self._emit("cheap_talk", {"round": round_number, **entry})
                        else:
                            entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                            transcript.append(entry)
                            await self._emit("cheap_talk", {"round": round_number, **entry})
                    except ValueError:
                        error_msg = f"Your response looked like JSON but could not be parsed. Please send either a plain text message or a valid JSON purchase."
                        if hasattr(self.agent_b_impl, '_append_user'):
                            self.agent_b_impl._append_user(error_msg)
                        await self._emit("validation_error", {"agent": "agent_b", "error": "malformed JSON"})
                        entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                        transcript.append(entry)
                        await self._emit("cheap_talk", {"round": round_number, **entry})

                turn += 1

        # --- Decision phase ---
        if (alloc_a is None or alloc_b is None) and self.config.enable_cheap_talk:
            await self._emit("phase_start", {"round": round_number, "phase": "decision"})

        if alloc_a is None:
            if self._is_human(self.agent_a_impl):
                await self._emit("waiting_for_human", {
                    "agent": "agent_a",
                    "round": round_number,
                    "phase": "decision",
                    "public_config": pub_config_a,
                })
            self._set_stream_callback(self.agent_a_impl, "agent_a", round_number, "decision")
            alloc_a, proj_runs_a = await self._request_decision_with_retries(
                self.agent_a_impl, "agent_a", round_num_a,
                pub_config_a, transcript, self.agent_a_state.memory,
            )
            api_meta = self._get_api_meta(self.agent_a_impl)
            if api_meta and "error_type" in api_meta:
                await self._emit_api_failure(self.agent_a_impl, "agent_a", round_number, "decision")
            await self._emit_thinking(self.agent_a_impl, "agent_a", round_number, transcript)
            await self._emit_reasoning(self.agent_a_impl, "agent_a", round_number, transcript)

        if alloc_b is None:
            if self._is_human(self.agent_b_impl):
                await self._emit("waiting_for_human", {
                    "agent": "agent_b",
                    "round": round_number,
                    "phase": "decision",
                    "public_config": pub_config_b,
                })
            self._set_stream_callback(self.agent_b_impl, "agent_b", round_number, "decision")
            alloc_b, proj_runs_b = await self._request_decision_with_retries(
                self.agent_b_impl, "agent_b", round_num_b,
                pub_config_b, transcript, self.agent_b_state.memory,
            )
            api_meta = self._get_api_meta(self.agent_b_impl)
            if api_meta and "error_type" in api_meta:
                await self._emit_api_failure(self.agent_b_impl, "agent_b", round_number, "decision")
            await self._emit_thinking(self.agent_b_impl, "agent_b", round_number, transcript)
            await self._emit_reasoning(self.agent_b_impl, "agent_b", round_number, transcript)

        # --- Resolve round ---
        total_demanded = {}
        for r in self.config.resource_types:
            total_demanded[r] = alloc_a.get(r, 0) + alloc_b.get(r, 0)

        overdrawn = any(
            total_demanded[r] > self.config.resource_supply[r]
            for r in self.config.resource_types
        )

        proj_details_a = None
        proj_details_b = None

        if overdrawn:
            reward_a = 0.0
            reward_b = 0.0
        else:
            # Project-based rewards
            calc_a = self.agent_a_state.project_calculator
            calc_b = self.agent_b_state.project_calculator

            # Resolve project runs
            resolved_a, auto_allocated_a = calc_a.resolve_project_runs(alloc_a, proj_runs_a)
            resolved_b, auto_allocated_b = calc_b.resolve_project_runs(alloc_b, proj_runs_b)

            # Emit auto-allocation events for observability
            if auto_allocated_a:
                await self._emit("project_auto_allocation", {
                    "round": round_number,
                    "agent": "agent_a",
                    "purchased": alloc_a,
                    "resolved_runs": resolved_a,
                    "reason": "Agent submitted resource purchases without explicit project allocations; resources auto-assigned greedily to projects.",
                })
            if auto_allocated_b:
                await self._emit("project_auto_allocation", {
                    "round": round_number,
                    "agent": "agent_b",
                    "purchased": alloc_b,
                    "resolved_runs": resolved_b,
                    "reason": "Agent submitted resource purchases without explicit project allocations; resources auto-assigned greedily to projects.",
                })

            # Compute rewards with scenario-level synergy
            details_a = calc_a.compute_reward(resolved_a, alloc_a, alloc_b, self.config.scenario_synergy)
            details_b = calc_b.compute_reward(resolved_b, alloc_b, alloc_a, self.config.scenario_synergy)

            # Add auto-allocation flag to project details for schema
            details_a["auto_allocated"] = auto_allocated_a
            details_b["auto_allocated"] = auto_allocated_b

            reward_a = details_a["total_reward"]
            reward_b = details_b["total_reward"]
            proj_details_a = details_a
            proj_details_b = details_b

        self.agent_a_state.cumulative_reward += reward_a
        self.agent_b_state.cumulative_reward += reward_b

        # Update memories
        self.agent_a_state.memory.append({
            "round": round_number,
            "my_allocation": alloc_a,
            "my_reward": reward_a,
            "overdrawn": overdrawn,
            "cheap_talk": transcript,
        })
        self.agent_b_state.memory.append({
            "round": round_number,
            "my_allocation": alloc_b,
            "my_reward": reward_b,
            "overdrawn": overdrawn,
            "cheap_talk": transcript,
        })

        # Notify agents of round results
        if hasattr(self.agent_a_impl, 'notify_round_result'):
            self.agent_a_impl.notify_round_result(
                round_num_a, alloc_a, reward_a, alloc_b, reward_b,
                overdrawn, self.config.visible_opponent_reward,
                proj_details_a,
            )
        if hasattr(self.agent_b_impl, 'notify_round_result'):
            self.agent_b_impl.notify_round_result(
                round_num_b, alloc_b, reward_b, alloc_a, reward_a,
                overdrawn, self.config.visible_opponent_reward,
                proj_details_b,
            )

        result = RoundResult(
            round_number=round_number,
            agent_a_allocation=alloc_a,
            agent_b_allocation=alloc_b,
            total_demanded=total_demanded,
            resource_supply=dict(self.config.resource_supply),
            overdrawn=overdrawn,
            agent_a_reward=reward_a,
            agent_b_reward=reward_b,
            agent_a_project_runs=proj_details_a,
            agent_b_project_runs=proj_details_b,
            cheap_talk_transcript=transcript,
        )

        round_complete_data = {
            "round_number": round_number,
            "agent_a_allocation": alloc_a,
            "agent_b_allocation": alloc_b,
            "total_demanded": total_demanded,
            "resource_supply": dict(self.config.resource_supply),
            "overdrawn": overdrawn,
            "agent_a_reward": reward_a,
            "agent_b_reward": reward_b,
        }
        if proj_details_a:
            round_complete_data["agent_a_project_runs"] = proj_details_a
        if proj_details_b:
            round_complete_data["agent_b_project_runs"] = proj_details_b

        await self._emit("round_complete", round_complete_data)
        self.results.append(result)
        return result

    async def run_game(self) -> dict:
        game_start_config = {
            "mode": self.config.mode.value,
            "num_rounds": self.config.num_rounds,
            "resource_types": self.config.resource_types,
            "resource_supply": self.config.resource_supply,
            "resource_costs": self.config.resource_costs,
            "agent_budget": self.config.agent_budget,
            "cheap_talk_turns": self.config.cheap_talk_turns,
            "first_speaker": self.config.first_speaker,
            "seed": self.config.seed,
            "swapped": self.config.swapped,
            "goal": self.config.goal,
            "thinking": self.config.thinking,
            "visible_utilities": self.config.visible_utilities,
            "visible_opponent_reward": self.config.visible_opponent_reward,
            "enable_cheap_talk": self.config.enable_cheap_talk,
            "share_projects": self.config.share_projects,
            "think_about_opponent": self.config.think_about_opponent,
            "maximize_joint": self.config.maximize_joint,
            "full_transparency": self.config.full_transparency,
        }
        game_start_config["agent_projects"] = self.config.agent_projects
        if self.config.scenario_synergy:
            game_start_config["scenario_synergy"] = self.config.scenario_synergy
        if self.config.oracle_stats:
            game_start_config["oracle_stats"] = self.config.oracle_stats

        await self._emit("game_start", {
            "episode_uid": self.config.episode_uid,
            "config": game_start_config,
        })

        # Pre-initialize LLM agents
        for label, impl in [("agent_a", self.agent_a_impl), ("agent_b", self.agent_b_impl)]:
            if hasattr(impl, '_init_session'):
                impl._init_session(label, self._public_config(label))
            prompt = getattr(impl, '_system_prompt', None)
            if prompt:
                await self._emit("system_prompt", {"agent": label, "prompt": prompt})

        for round_num in range(1, self.config.num_rounds + 1):
            if self._stopped:
                await self._emit("game_stopped", {"reason": "stopped by user", "after_round": round_num - 1})
                break

            if self.config.mode == GameMode.SHIFTING and round_num > 1:
                if self._shifting_a:
                    if hasattr(self.agent_a_impl, 'reset_context'):
                        self.agent_a_impl.reset_context()
                    await self._emit("context_reset", {"round": round_num, "agent": "agent_a"})
                if self._shifting_b:
                    if hasattr(self.agent_b_impl, 'reset_context'):
                        self.agent_b_impl.reset_context()
                    await self._emit("context_reset", {"round": round_num, "agent": "agent_b"})

            await self.run_round(round_num)

        # Build result
        rounds = []
        for r in self.results:
            stats = self._compute_round_stats(r.cheap_talk_transcript, self.config.cheap_talk_turns)
            round_data = {
                "round_number": r.round_number,
                "agent_a_allocation": r.agent_a_allocation,
                "agent_b_allocation": r.agent_b_allocation,
                "total_demanded": r.total_demanded,
                "resource_supply": r.resource_supply,
                "overdrawn": r.overdrawn,
                "agent_a_reward": r.agent_a_reward,
                "agent_b_reward": r.agent_b_reward,
                "stats": stats,
            }
            if r.agent_a_project_runs:
                round_data["agent_a_project_runs"] = r.agent_a_project_runs
            if r.agent_b_project_runs:
                round_data["agent_b_project_runs"] = r.agent_b_project_runs
            rounds.append(round_data)

        summary = {
            "episode_uid": self.config.episode_uid,
            "mode": self.config.mode.value,
            "num_rounds": len(self.results),
            "agent_a_cumulative_reward": self.agent_a_state.cumulative_reward,
            "agent_b_cumulative_reward": self.agent_b_state.cumulative_reward,
            "rounds": rounds,
            "first_speaker": self.config.first_speaker,
            "seed": self.config.seed,
            "swapped": self.config.swapped,
            "stopped": self._stopped,
            "api_failures": self._api_failures,
        }

        summary["agent_projects"] = self.config.agent_projects

        # Build per_round_scenarios: for rotating games, already populated per round;
        # for non-rotating games, repeat the same scenario for each round
        if self._per_round_scenarios:
            summary["per_round_scenarios"] = self._per_round_scenarios
        else:
            static_scenario = {
                "agent_projects": self.config.agent_projects,
            }
            if self.config.oracle_stats:
                static_scenario["oracle_stats"] = self.config.oracle_stats
            summary["per_round_scenarios"] = [static_scenario] * len(self.results)

        # Calculate theoretical joint maximum from oracle stats
        # For stable agents: sum across all rounds
        # For shifted agents: only the final round (they don't remember previous rounds)
        theoretical_joint_max_sum = None
        theoretical_joint_max_final = None

        if self._per_round_scenarios:
            joint_max_sum = 0.0
            for scenario in self._per_round_scenarios:
                oracle = scenario.get("oracle_stats")
                if oracle and "collab_max" in oracle:
                    joint_max_sum += oracle["collab_max"]
            if joint_max_sum > 0:
                theoretical_joint_max_sum = joint_max_sum
                # Final round oracle stats
                final_oracle = self._per_round_scenarios[-1].get("oracle_stats")
                if final_oracle and "collab_max" in final_oracle:
                    theoretical_joint_max_final = final_oracle["collab_max"]
        elif self.config.oracle_stats:
            oracle = self.config.oracle_stats
            collab_max = oracle["collab_max"] if isinstance(oracle, dict) else oracle.collab_max
            theoretical_joint_max_sum = collab_max * len(self.results)
            theoretical_joint_max_final = collab_max

        # Request post-environment reflections from LLM agents
        # Skip reflections when cheap talk is disabled (no-talk baseline)
        reflections = {}
        if self.config.enable_cheap_talk:
            for label, impl in [("agent_a", self.agent_a_impl), ("agent_b", self.agent_b_impl)]:
                if hasattr(impl, 'reflect'):
                    is_shifted = self._shifting_a if label == "agent_a" else self._shifting_b

                    if is_shifted:
                        # Shifted agents only have context from the last round
                        last_result = self.results[-1]
                        own_reward = last_result.agent_a_reward if label == "agent_a" else last_result.agent_b_reward
                        opp_reward = last_result.agent_b_reward if label == "agent_a" else last_result.agent_a_reward
                    else:
                        own_reward = self.agent_a_state.cumulative_reward if label == "agent_a" else self.agent_b_state.cumulative_reward
                        opp_reward = self.agent_b_state.cumulative_reward if label == "agent_a" else self.agent_a_state.cumulative_reward

                    # Use final round oracle for shifted agents, sum for stable agents
                    theoretical_joint_max = theoretical_joint_max_final if is_shifted else theoretical_joint_max_sum

                    reflection_text = await impl.reflect(
                        label, len(self.results) if not is_shifted else 1,
                        own_reward, opp_reward,
                        self.config.visible_opponent_reward,
                        theoretical_joint_max,
                    )

                    if reflection_text:
                        reflections[label] = reflection_text
                        await self._emit("agent_reflection", {
                            "agent": label,
                            "reflection": reflection_text,
                            "cumulative_reward": own_reward,
                            "theoretical_joint_max": theoretical_joint_max,
                        })

        # Add reflections to summary if any were generated
        if reflections:
            summary["reflections"] = reflections

        await self._emit("game_complete", summary)
        return summary
