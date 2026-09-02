"""
Pydantic schemas for game data structures.

These models define the canonical shapes for data persisted to Firestore
and local JSON storage. Used for validation, documentation, and type safety.
"""

from pydantic import BaseModel, Field

# Schema version for Firestore documents (single source of truth)
CURRENT_SCHEMA_VERSION = 9  # V9: maximize_joint and full_transparency flags


class AgentInfo(BaseModel):
    type: str = "random"
    model: str | None = None


class SynergyConditionSchema(BaseModel):
    """Scenario-level synergy: flat bonus per player when triggered.

    If both players buy ≥1 of `resource` AND combined total ≥ `threshold`,
    each player gets a flat +`bonus`.
    """
    resource: str       # which resource triggers synergy
    threshold: int      # total units (both players) needed
    bonus: int          # flat bonus per player when triggered


class ProjectSchema(BaseModel):
    """Schema for a single project assigned to an agent."""
    name: str                              # e.g. "project_a", "project_b"
    requirements: dict[str, int]           # resource: qty_per_run
    reward: int                            # base reward per run


class OracleStatsSchema(BaseModel):
    """Optimal reward statistics computed by the scenario solver."""
    v1: float           # Agent A max (solo optimization)
    v2: float           # Agent B max (solo optimization)
    combined: float     # C = V1 + V2
    collab_max: float   # M (joint optimization including synergy)
    mc_ratio: float     # M/C ratio
    collab_detail: str | None = None  # Optimal project allocations (e.g. "p5x2, p1x1")


class GameConfigSchema(BaseModel):
    """Schema for the game configuration stored alongside each trace."""
    mode: str
    num_rounds: int
    cheap_talk_turns: int
    resource_types: list[str]
    resource_supply: dict[str, int]
    resource_costs: dict[str, float]
    agent_budget: float
    max_resource_types_per_turn: int = 2
    agent_projects: list[list[ProjectSchema]]
    agent_shifting: list[bool]
    first_speaker: int
    agents: list[AgentInfo]
    goal: str = ""
    thinking: bool = False
    visible_utilities: bool = False
    visible_opponent_reward: bool = True
    enable_cheap_talk: bool = True
    share_projects: bool = False
    think_about_opponent: bool = False
    maximize_joint: bool = False
    full_transparency: bool = False
    named_projects: bool = False
    seed: int | None = None
    swapped: bool = False
    experiment_label: str = ""
    oracle_stats: OracleStatsSchema | None = None
    scenario_synergy: SynergyConditionSchema | None = None
    # Scenario pool for per-round project rotation
    scenario_pool_path: str | None = None
    target_mc_ratio: float | None = None
    rotate_projects: bool = False  # When True, sample new projects each round from pool
    # Experiment tracking metadata
    experiment_run_id: str | None = None
    experiment_name: str | None = None
    git_hash: str | None = None


class RoundStatsSchema(BaseModel):
    """Precomputed aggregate statistics for a round."""
    num_turns_used: int
    early_submit: bool
    agent_a_msg_count: int
    agent_b_msg_count: int
    agent_a_char_count: int
    agent_b_char_count: int
    total_char_count: int


class ProjectRunsSchema(BaseModel):
    """Project runs resolved for an agent in a single round."""
    runs: dict[str, int]         # project_name: num_runs
    base_reward: float
    synergy_bonus: float
    total_reward: float
    auto_allocated: bool = False  # True if greedy auto-allocation was used (no explicit project runs provided)


class RoundResultSchema(BaseModel):
    """Schema for a single round's result within the game summary."""
    model_config = {"use_enum_values": True}

    round_number: int
    agent_a_allocation: dict[str, int]
    agent_b_allocation: dict[str, int]
    total_demanded: dict[str, int]
    resource_supply: dict[str, int]
    overdrawn: bool
    agent_a_reward: float
    agent_b_reward: float
    agent_a_project_runs: ProjectRunsSchema | None = None
    agent_b_project_runs: ProjectRunsSchema | None = None
    # V1: transcript in rounds (legacy)
    cheap_talk_transcript: list[dict] | None = None
    # V2+: precomputed stats in rounds
    stats: RoundStatsSchema | None = None


class PerRoundScenarioSchema(BaseModel):
    """Per-round project/oracle data. Identical entries for non-rotating games."""
    candidate_id: str | None = None
    mc_bucket: float | None = None
    agent_projects: list[list[ProjectSchema]]
    oracle_stats: OracleStatsSchema | None = None


class GameResultSchema(BaseModel):
    """Schema for the game result/summary returned by the engine."""
    game_id: str
    mode: str
    num_rounds: int
    agent_a_cumulative_reward: float
    agent_b_cumulative_reward: float
    rounds: list[RoundResultSchema]
    agent_projects: list[list[ProjectSchema]]
    first_speaker: int
    seed: int | None = None
    swapped: bool = False
    stopped: bool = False
    api_failures: dict[str, int] | None = None
    per_round_scenarios: list[PerRoundScenarioSchema] | None = None
    reflections: dict[str, str] | None = None  # agent_id -> reflection text (post-game learnings)


class GameEventSchema(BaseModel):
    """Schema for a single event in the event log."""
    type: str
    data: dict


class FirestoreDocumentSchema(BaseModel):
    """Schema for the complete document written to Firestore.

    The created_at field uses Firestore SERVER_TIMESTAMP at write time,
    so it's excluded from validation (not present in the dict we construct).

    Schema versions:
    - V1-V4: Legacy (game_traces collection, value-function rewards)
    - V5: Project-based rewards (game_traces_v2 collection)
    """
    schema_version: int = CURRENT_SCHEMA_VERSION
    game_config: GameConfigSchema
    result: GameResultSchema
    # V1/V2: events (uncompressed)
    events: list[GameEventSchema] | None = None
    # V3+: events_compressed (gzip + base64)
    events_compressed: str | None = None
