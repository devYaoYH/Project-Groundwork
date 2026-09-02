"""Negotiation game plugin for a2a-engine.

Importing this package registers the game, its batch-level config resolver, and
its local-first default trace store.
"""

from a2a_engine import register_game

from negotiation_game.game import NegotiationConfig, NegotiationGame
from negotiation_game.resolve import resolve_config

register_game(
    "negotiation",
    NegotiationGame,
    resolve_config=resolve_config,
    # A single-game experiment must work with no account or cloud SDK. Firestore
    # remains an explicit optional sink for importing or mirroring legacy data.
    storage={"backend": "sqlite"},
    package="negotiation-game",
    # A negotiation dry run swaps in heuristic agents, so it needs no API keys.
    dry_run_checks_keys=False,
)

__all__ = ["NegotiationConfig", "NegotiationGame", "resolve_config"]
