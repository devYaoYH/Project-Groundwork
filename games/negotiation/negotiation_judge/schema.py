"""Pydantic models for judge input/output validation."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --- Enums ---

class Effectiveness(str, Enum):
    positive = "positive"
    negative = "negative"
    neutral = "neutral"


class Intent(str, Enum):
    cooperative = "cooperative"
    self_interested = "self-interested"
    ambiguous = "ambiguous"


class RoundOutcome(str, Enum):
    optimal = "optimal"
    suboptimal = "suboptimal"
    overdrawn = "overdrawn"


# --- Judge Input ---

class CheapTalkTurn(BaseModel):
    speaker: str
    turn: int
    thinking: Optional[str] = None
    speech: Optional[str] = None
    # True when this turn contains a decision (turn=-1 forced final, or early decision
    # with speech suppressed). Used by the prompt formatter to label as "turn_decision".
    is_decision: bool = False


class JudgeRoundContext(BaseModel):
    """Everything the judge needs to evaluate one round."""
    game_id: str
    round_number: int
    model_a: str
    model_b: str
    mode: str  # "stable" or "shifting"
    mc_ratio: Optional[float] = None
    oracle_optimum: Optional[float] = None
    optimal_allocation: Optional[str] = None  # collab_detail string
    round_outcome: RoundOutcome
    joint_efficiency: float
    reward_a: float
    reward_b: float
    cheap_talk: list[CheapTalkTurn]
    allocation_a: dict[str, int]
    allocation_b: dict[str, int]
    auto_allocated_a: bool = False
    auto_allocated_b: bool = False


# --- Judge Output (Phase 1: free discovery) ---

class PatternEvidence(BaseModel):
    speaker: str
    type: str = "speech"  # "thinking", "speech", or "allocation" — default speech if omitted
    quote: str


class DiscoveredPattern(BaseModel):
    name: str
    description: str
    evidence: list[PatternEvidence]
    effectiveness: Effectiveness
    intent: Intent
    speech_allocation_coherent: bool
    coherence_note: Optional[str] = None


class RoundJudgment(BaseModel):
    """Output of a single round judgment (Phase 1)."""
    game_id: str
    round_number: int
    model_a: str
    model_b: str
    mode: str
    mc_ratio: Optional[float] = None
    round_outcome: RoundOutcome
    joint_efficiency: float
    patterns: list[DiscoveredPattern]
    round_attribution: str
    # New: how prior rounds influenced this one (repair, learning, regression, etc.)
    prior_round_influence: Optional[str] = None


# --- Game-level judge input (new: one call per game) ---

class RoundData(BaseModel):
    """Per-round data for game-level judging."""
    round_number: int
    round_outcome: RoundOutcome
    joint_efficiency: float
    reward_a: float
    reward_b: float
    cheap_talk: list[CheapTalkTurn]
    allocation_a: dict[str, int]
    allocation_b: dict[str, int]
    auto_allocated_a: bool = False
    auto_allocated_b: bool = False


class JudgeGameContext(BaseModel):
    """Everything the judge needs to evaluate one game (all rounds at once)."""
    game_id: str
    model_a: str
    model_b: str
    mode: str  # "stable" or "shifting"
    # In shifting mode, exactly one of agent_a / agent_b has its context reset
    # each round; the other retains full history. None in stable mode.
    shifting_agent: Optional[str] = None  # "agent_a" | "agent_b" | None
    mc_ratio: Optional[float] = None
    oracle_optimum: Optional[float] = None  # game-level oracle (for non-rotating)
    optimal_allocation: Optional[str] = None
    rounds: list[RoundData]


class GameJudgment(BaseModel):
    """Output of a whole-game judgment (Phase 1)."""
    game_id: str
    model_a: str
    model_b: str
    mode: str
    mc_ratio: Optional[float] = None
    rounds: list[RoundJudgment]
    game_attribution: str  # overall narrative across all rounds


# --- Firestore storage record ---

class JudgmentRecord(BaseModel):
    """Full document written to the judge_results Firestore collection.

    Document ID: {game_id}__{judge_model}__{prompt_version}
    (slashes in judge_model are replaced with underscores)

    Embeds the full GameJudgment plus storage metadata so the schema of
    every persisted document is explicitly typed.
    """
    # GameJudgment fields (inlined so the doc is self-contained)
    game_id: str
    model_a: str
    model_b: str
    mode: str
    mc_ratio: Optional[float] = None
    rounds: list["RoundJudgment"]
    game_attribution: str

    # Storage metadata
    judge_model: str
    prompt_version: str
    judged_at: str  # ISO-8601 UTC datetime string
    # Compressed judge input transcript (gzip + base64). Stores exactly what
    # was sent to the LLM so the UI can display it for inspection.
    input_transcript_gz: Optional[str] = None


# --- Taxonomy (Phase 2: consolidation) ---

class CanonicalPattern(BaseModel):
    id: str
    label: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    # True if this pattern's valence flips depending on context
    # (e.g., "Persistent Advocacy" is positive when defending the optimal plan
    # but negative when stubbornly defending a bad one). The pattern is still
    # placed in whichever bucket it leans toward most often.
    context_dependent: bool = False


class Taxonomy(BaseModel):
    """Canonical taxonomy split into failure modes and positive patterns.

    Soft target: ~10 categories per list. No forcing — fewer is fine if the
    data doesn't support more. Neutral is dropped at the canonical level
    (individual pattern instances can still be tagged neutral in round judgments).
    """
    version: int = 1
    negative_patterns: list[CanonicalPattern] = Field(
        default_factory=list, max_length=10,
        description="Canonical negative coordination patterns (up to 10)"
    )
    positive_patterns: list[CanonicalPattern] = Field(
        default_factory=list, max_length=10,
        description="Canonical positive coordination patterns (up to 10)"
    )

    @property
    def canonical_patterns(self) -> list[CanonicalPattern]:
        """Flat list of all canonical patterns (both buckets combined)."""
        return self.negative_patterns + self.positive_patterns

    def valence_of(self, canonical_id: str) -> str | None:
        """Return 'negative', 'positive', or None for a given canonical_id."""
        if any(p.id == canonical_id for p in self.negative_patterns):
            return "negative"
        if any(p.id == canonical_id for p in self.positive_patterns):
            return "positive"
        return None

    def is_context_dependent(self, canonical_id: str) -> bool:
        for p in self.canonical_patterns:
            if p.id == canonical_id:
                return p.context_dependent
        return False


