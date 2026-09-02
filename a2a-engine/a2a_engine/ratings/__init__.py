"""Game-agnostic rating utilities."""

from a2a_engine.ratings.openskill import OpenSkillRater
from a2a_engine.ratings.pipeline import (
    RatingAdapter,
    RatingMaterialization,
    rebuild_rating_snapshot,
)
from a2a_engine.ratings.schemas import (
    MetricSpec,
    PlayerRatingState,
    RatingEvent,
    RatingParticipant,
    RatingSnapshot,
    SkillRating,
)
from a2a_engine.ratings.matchmaking import Matchmaker, MatchmakingPlan
from a2a_engine.ratings.store import (
    ConcurrentRatingUpdate,
    DynamoDBRatingStore,
    InMemoryRatingStore,
    RatingEventAlreadyProcessed,
)

__all__ = [
    "ConcurrentRatingUpdate",
    "DynamoDBRatingStore",
    "InMemoryRatingStore",
    "Matchmaker",
    "MatchmakingPlan",
    "MetricSpec",
    "OpenSkillRater",
    "PlayerRatingState",
    "RatingEventAlreadyProcessed",
    "RatingAdapter",
    "RatingEvent",
    "RatingMaterialization",
    "RatingParticipant",
    "RatingSnapshot",
    "SkillRating",
    "rebuild_rating_snapshot",
]
