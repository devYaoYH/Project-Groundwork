"""Pure authorization; games own budget usage, delivery and events."""

from __future__ import annotations

from dataclasses import dataclass

from .topology import CHANNELS, Topology, canonical_channel


@dataclass(frozen=True)
class RoutingContext:
    seat_ids: tuple[int, ...]
    meeting_id: int | None
    meeting_participants: tuple[int, ...]


@dataclass(frozen=True)
class RoutingDecision:
    allowed: bool
    recipients: tuple[int, ...] = ()
    reason: str | None = None
    charge_attempt: bool = False


class CommRouter:
    def __init__(self, topology: Topology) -> None:
        self.topology = topology

    def authorize(
        self, sender: int, channel: str, to: object = None, *, phase: str, round: int,
        context: RoutingContext, attempted_this_round: int,
    ) -> RoutingDecision:
        policy = self.topology.effective(phase=phase, round=round)
        channel = canonical_channel(channel)
        if channel not in CHANNELS:
            return RoutingDecision(False)
        if sender not in context.seat_ids or sender not in policy.seats:
            return RoutingDecision(False, reason="sender is not a seat")
        cap = policy.per_agent_per_round
        exhausted = cap >= 0 and attempted_this_round >= cap
        if policy.legacy and exhausted:
            return RoutingDecision(False, reason=f"per-agent cheap-talk messaging-tool budget exhausted (dm_cap={cap})")

        charge = policy.legacy
        if channel not in policy.channels:
            source = "communication_protocol" if policy.legacy else f"topology in {phase}"
            return RoutingDecision(False, reason=f"{channel} tool is disabled by {source}", charge_attempt=charge)
        if channel == "dm":
            try:
                recipient = int(to)
            except (TypeError, ValueError, OverflowError):
                return RoutingDecision(False, reason="dm tool missing integer 'to'", charge_attempt=charge)
            if recipient not in context.seat_ids:
                return RoutingDecision(False, reason="dm recipient is out of range", charge_attempt=charge)
            if not policy.legacy and recipient not in policy.neighbors(sender):
                return RoutingDecision(False, reason="dm recipient is not allowed by topology", charge_attempt=False)
            recipients = (recipient,)
        else:
            candidates = context.meeting_participants if channel == "participant_groupchat" else context.seat_ids
            neighbors = policy.neighbors(sender)
            recipients = tuple(seat for seat in candidates if seat != sender and (policy.legacy or seat in neighbors))

        if not policy.legacy and exhausted:
            return RoutingDecision(False, reason=f"per-agent messaging budget exhausted (per_agent_per_round={cap})")
        return RoutingDecision(True, recipients, charge_attempt=True)
