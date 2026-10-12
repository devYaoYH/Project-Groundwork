"""Attempt-scoped admission, registration and best-effort lifecycle teardown."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Mapping, TYPE_CHECKING

from .contract import PROTOCOL_VERSION_V2, TurnInvocation, TurnInvocationV2
from .dispatch import DeliveryFailure, TurnDispatcher, TurnDispatcherV2

if TYPE_CHECKING:
    from .server import ManagedEpisode


@dataclass(frozen=True)
class SeatHandle:
    seat: int
    runtime: str
    join_url: str | None


@dataclass(frozen=True)
class EpisodeHandle:
    environment_id: str
    episode_id: str
    attempt_id: str
    join_deadline: datetime | None
    seats: tuple[SeatHandle, ...]

    def to_json(self) -> str:
        payload = asdict(self)
        payload["join_deadline"] = self.join_deadline.isoformat() if self.join_deadline else None
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class Registration:
    observation: dict
    prompt: str | None = None


@dataclass(frozen=True)
class RosterSeat:
    seat: int
    runtime: str
    registration: Registration
    protocol_version: str | None
    agent_info: dict


@dataclass(frozen=True)
class Roster:
    seats: tuple[RosterSeat, ...]


class SeatsUnavailable(TimeoutError):
    def __init__(self, seats):
        self.seats = tuple(seats)
        super().__init__(f"seat_unavailable: {self.seats}")


def _seat_specs(config):
    specs = config.get("agents", [])
    return [specs[i] if i < len(specs) else {} for i in range(config.get("num_agents", len(specs) or 2))]


def open_episode(manager, config, *, scripted=False) -> EpisodeSession:
    if manager is None or all(s.get("runtime", "in_process") == "in_process" for s in _seat_specs(config)):
        return EpisodeSession.local(config)
    return EpisodeSession(config, manager.provision(config, scripted=scripted))


class EpisodeSession:
    handle: EpisodeHandle
    managed: ManagedEpisode | None

    def __init__(self, config, managed=None):
        self.managed = managed
        self.closed = False
        episode = managed.episode if managed else None
        root = f"{managed.io.base_url}/episodes/{episode.episode_id}" if managed else None
        self.handle = EpisodeHandle(
            config.get("environment_id", "calendar"),
            episode.episode_id if episode else uuid.uuid4().hex,
            episode.attempt_id if episode else uuid.uuid4().hex,
            datetime.fromtimestamp(min(s.join_deadline for s in episode.seats.values()), timezone.utc) if episode and episode.seats else None,
            tuple(SeatHandle(i, s.get("runtime", "in_process"),
                             f"{root}/join" if episode and i in episode.seats else None)
                  for i, s in enumerate(_seat_specs(config))),
        )

    @classmethod
    def local(cls, config):
        return cls(config)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.managed:
                self.managed.close()

    def _dispatch(self, seat, kind, registration, timeout_s, clock):
        runtime = self.managed
        episode = runtime.episode
        v2 = seat.protocol_version == PROTOCOL_VERSION_V2
        root = f"{runtime.io.base_url}/episodes/{episode.episode_id}"
        fields = dict(episode_id=episode.episode_id, seat=seat.seat, turn_id=uuid.uuid4().hex,
                      kind=kind, deadline=datetime.now(timezone.utc) + timedelta(seconds=timeout_s),
                      observation=registration.observation, prompt=registration.prompt,
                      mcp={name: f"{root}/{name}/mcp" for name in (("env", "comm", "lifecycle") if v2 else ("env", "comm"))})
        if v2:
            invocation = TurnInvocationV2(**fields, environment_id=episode.environment_id,
                                          attempt_id=episode.attempt_id, operation=kind)
            dispatcher = TurnDispatcherV2(runtime.io, runtime.environment.registry.lifecycle,
                                          network=runtime.environment.network, clock=clock)
        else:
            invocation = TurnInvocation(**fields)
            dispatcher = TurnDispatcher(runtime.io, clock=clock)
        dispatcher.dispatch(seat, invocation, deadline=clock() + timeout_s)

    @contextmanager
    def started(self, registrations: Mapping[int, Registration], *, protocols: frozenset[str],
                register_timeout_s: float, clock=time.monotonic) -> Iterator[Roster]:
        if set(registrations) != {s.seat for s in self.handle.seats}:
            raise ValueError("registrations must include every roster seat")
        if not math.isfinite(register_timeout_s) or register_timeout_s <= 0:
            raise ValueError("register_timeout_s must be finite and positive")
        if self.closed:
            raise SeatsUnavailable(registrations)
        registered = []
        cancelled = False
        episode = self.managed.episode if self.managed else None
        try:
            if episode:
                missing = []
                for seat_id in episode.seats:
                    try:
                        episode.wait_ready(seat_id)
                    except TimeoutError:
                        missing.append(seat_id)
                if missing:
                    raise SeatsUnavailable(missing)
                unsupported = [s.seat for s in episode.seats.values() if s.protocol_version not in protocols]
                if unsupported:
                    raise SeatsUnavailable(unsupported)
                for seat_id, registration in registrations.items():
                    if seat_id not in episode.seats:
                        continue
                    seat = episode.seats[seat_id]
                    try:
                        self._dispatch(seat, "register", registration, register_timeout_s, clock)
                    except DeliveryFailure:
                        raise SeatsUnavailable([seat_id]) from None
                    with episode.lock:
                        seat.registered = True
                    registered.append(seat)
            handles = {s.seat: s for s in self.handle.seats}
            yield Roster(tuple(RosterSeat(i, handles[i].runtime, reg,
                                         episode.seats[i].protocol_version if episode and i in episode.seats else None,
                                         dict(episode.seats[i].agent_info) if episode and i in episode.seats else {})
                               for i, reg in registrations.items()))
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                for seat in registered:
                    try:
                        self._dispatch(seat, "episode_end", Registration({}), register_timeout_s, clock)
                    except DeliveryFailure:
                        pass
