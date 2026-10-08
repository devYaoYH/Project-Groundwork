"""Validated graphs and default-plus-phase policy, without runtime state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

CHANNELS = ("dm", "participant_groupchat", "all_groupchat")
_TOOL_ALIASES = {
    "groupchat": "all_groupchat", "group_chat": "all_groupchat", "group": "all_groupchat",
    "all_agent_groupchat": "all_groupchat", "all_agent_chat": "all_groupchat",
    "participant_chat": "participant_groupchat", "meeting_groupchat": "participant_groupchat",
    "meeting_chat": "participant_groupchat",
}
_PROTOCOL_ALIASES = {
    **{key: {value} for key, value in _TOOL_ALIASES.items()},
    "direct": {"dm"}, "direct_message": {"dm"}, "private": {"dm"},
    "dm_and_groupchat": {"dm", "all_groupchat"},
    "dm_and_all_groupchat": {"dm", "all_groupchat"},
    "dm_and_participant_groupchat": {"dm", "participant_groupchat"},
    "both": {"dm", "all_groupchat"}, "mixed": {"dm", "all_groupchat"},
    "all": set(CHANNELS),
}


def canonical_channel(value: object) -> str:
    channel = str(value or "").lower()
    return _TOOL_ALIASES.get(channel, channel)


def legacy_channels(protocol: str | dict[str, bool]) -> set[str]:
    raw = protocol or "dm"
    if isinstance(raw, dict):
        channels: set[str] = set()
        for key, enabled in raw.items():
            if enabled:
                key = str(key).lower()
                channels.update(_PROTOCOL_ALIASES.get(key, {key}))
    else:
        key = str(raw).lower()
        channels = set(_PROTOCOL_ALIASES.get(key, {key}))
    if not channels or not channels.issubset(CHANNELS):
        raise ValueError("communication_protocol must enable one or more of: dm, participant_groupchat, all_groupchat")
    return channels


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Channel(_StrictModel):
    enabled: StrictBool = True
    scope: Literal["graph"] = "graph"


class _Budget(_StrictModel):
    per_agent_per_round: StrictInt = Field(default=-1, ge=-1)


class _Policy(_StrictModel):
    graph: Literal["complete", "ring", "star", "edges"] = "complete"
    hub: StrictInt | None = None
    edges: list[tuple[StrictInt, StrictInt]] = Field(default_factory=list)
    directed: StrictBool = False
    channels: dict[str, _Channel] = Field(default_factory=lambda: {"dm": _Channel()})
    budget: _Budget = Field(default_factory=_Budget)


class _TopologyConfig(_StrictModel):
    default: _Policy = Field(default_factory=_Policy)
    phases: dict[str, _Policy] = Field(default_factory=dict)


@dataclass(frozen=True)
class EffectiveTopology:
    graph: str
    seats: tuple[int, ...]
    channels: tuple[str, ...]
    per_agent_per_round: int
    hub: int | None = None
    edges: tuple[tuple[int, int], ...] = ()
    directed: bool = False
    legacy: bool = False

    def neighbors(self, sender: int) -> tuple[int, ...]:
        if self.graph == "complete":
            return tuple(seat for seat in self.seats if seat != sender)
        if self.graph == "ring":
            index = self.seats.index(sender)
            adjacent = {self.seats[(index - 1) % len(self.seats)], self.seats[(index + 1) % len(self.seats)]}
            return tuple(seat for seat in self.seats if seat != sender and seat in adjacent)
        if self.graph == "star":
            return tuple(seat for seat in self.seats if seat != sender and (sender == self.hub or seat == self.hub))
        adjacent = {target for source, target in self.edges if source == sender}
        if not self.directed:
            adjacent.update(source for source, target in self.edges if target == sender)
        return tuple(seat for seat in self.seats if seat != sender and seat in adjacent)

    @property
    def protocol(self) -> str:
        return "+".join(self.channels) or "none"

    def as_dict(self) -> dict:
        graph = {"graph": self.graph}
        if self.graph == "star":
            graph["hub"] = self.hub
        elif self.graph == "edges":
            graph.update(edges=[list(edge) for edge in self.edges], directed=self.directed)
        return {
            **graph,
            "channels": {channel: {"enabled": channel in self.channels, "scope": "graph"} for channel in CHANNELS},
            "budget": {"per_agent_per_round": self.per_agent_per_round},
            "legacy_routing": self.legacy,
        }


class Topology:
    def __init__(
        self, config: dict | None, *, seats: list[int] | tuple[int, ...], phases: set[str],
        default_send_phases: set[str], communication_protocol: str | dict[str, bool] = "dm",
        dm_cap: int = 1_000_000,
    ) -> None:
        if any(type(seat) is not int for seat in seats) or len(set(seats)) != len(seats):
            raise ValueError("topology seats must be unique integers")
        self.seats = tuple(sorted(seats))
        self.phases = frozenset(phases)
        if not default_send_phases.issubset(phases):
            raise ValueError("default send phases must be declared phases")
        self.legacy = config is None
        if self.legacy:
            channels = legacy_channels(communication_protocol)
            default = EffectiveTopology("complete", self.seats, tuple(c for c in CHANNELS if c in channels), dm_cap, legacy=True)
            self._effective = {phase: default if phase in default_send_phases else replace(default, channels=()) for phase in phases}
            return

        parsed = _TopologyConfig.model_validate(config)
        unknown = set(parsed.phases) - phases
        if unknown:
            raise ValueError(f"unknown topology phases: {sorted(unknown)}")
        self._validate_fields(parsed.default)
        default_fields = parsed.default.model_dump()
        default = self._resolve(default_fields)
        self._effective = {}
        for phase in phases:
            override = parsed.phases.get(phase)
            if override is None:
                self._effective[phase] = default if phase in default_send_phases else replace(default, channels=())
                continue
            self._validate_fields(override)
            fields = deepcopy(default_fields)
            for key, value in override.model_dump(exclude_unset=True).items():
                if key == "budget" or (key == "channels" and value):
                    previous = fields.get(key, {})
                    if key == "channels":
                        value = {**previous, **{name: {**previous.get(name, {}), **rule} for name, rule in value.items()}}
                    else:
                        value = {**previous, **value}
                fields[key] = value
            self._effective[phase] = self._resolve(fields)

    def _validate_fields(self, policy: _Policy) -> None:
        unknown = set(policy.channels) - set(CHANNELS)
        if unknown:
            raise ValueError(f"unknown topology channels: {sorted(unknown)}")
        if policy.hub is not None and policy.hub not in self.seats:
            raise ValueError("topology hub must be a seat")
        for source, target in policy.edges:
            if source not in self.seats or target not in self.seats:
                raise ValueError("topology edge endpoints must be seats")
            if source == target:
                raise ValueError("explicit topology edges cannot be self edges")

    def _resolve(self, fields: dict) -> EffectiveTopology:
        policy = _Policy.model_validate(fields)
        self._validate_fields(policy)
        if policy.graph == "star" and policy.hub is None:
            raise ValueError("star topology requires a hub")
        edges = policy.edges if policy.graph == "edges" else []
        if not policy.directed:
            edges = [tuple(sorted(edge)) for edge in edges]
        return EffectiveTopology(
            policy.graph, self.seats, tuple(c for c in CHANNELS if c in policy.channels and policy.channels[c].enabled),
            policy.budget.per_agent_per_round,
            hub=policy.hub if policy.graph == "star" else None,
            edges=tuple(sorted(set(edges))), directed=policy.directed if policy.graph == "edges" else False,
        )

    @classmethod
    def from_communication(cls, communication: dict | None, **kwargs) -> Topology:
        if communication is None:
            communication = {}
        if not isinstance(communication, dict):
            raise ValueError("communication must be an object")
        if communication.get("permissions"):
            raise ValueError("communication.permissions is not yet supported")
        if "topology" in communication and not isinstance(communication["topology"], dict):
            raise ValueError("communication.topology must be an object")
        return cls(communication.get("topology"), **kwargs)

    def effective(self, *, phase: str, round: int) -> EffectiveTopology:
        if phase not in self._effective:
            raise ValueError(f"unknown topology phase: {phase}")
        return self._effective[phase]
