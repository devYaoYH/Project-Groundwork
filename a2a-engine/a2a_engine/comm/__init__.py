"""Transport-independent communication policy."""

from .router import CommRouter, RoutingContext, RoutingDecision
from .topology import CHANNELS, EffectiveTopology, Topology, canonical_channel, legacy_channels

__all__ = [
    "CHANNELS", "CommRouter", "EffectiveTopology", "RoutingContext", "RoutingDecision",
    "Topology", "canonical_channel", "legacy_channels",
]
