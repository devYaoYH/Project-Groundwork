"""Environment-agnostic Dataset interface over EpisodeTrace JSON dumps.

`EpisodeDataset` mirrors the structure of `NegotiationDataset` (from the
a2a-llm-judge analysis layer) but knows nothing about a specific environment. Use
``EpisodeDataset.from_dir(path)`` to load every trace under a results directory
and then ``to_episodes_df`` / ``to_messages_df`` / ``to_events_df`` for analysis.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd
from pydantic import BaseModel, ConfigDict

from a2a_engine.schemas import ParticipantBinding, EpisodeTrace


class GameMessage(BaseModel):
    """One natural-language message extracted from a trace's events."""

    model_config = ConfigDict(extra="allow")

    turn: int
    speaker: str
    text: str
    timestamp: datetime


class EpisodeRecord(BaseModel):
    """Thin pydantic wrapper around a EpisodeTrace with computed views."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trace: EpisodeTrace

    # ---- pass-through ids ----
    @property
    def episode_uid(self) -> str:
        return self.trace.episode_uid

    @property
    def environment_id(self) -> str:
        return self.trace.config.environment_id

    @property
    def experiment_name(self) -> str | None:
        return self.trace.config.experiment_name

    @property
    def episode_id(self) -> str | None:
        return self.trace.config.episode_id

    @property
    def duration_seconds(self) -> float | None:
        if self.trace.ended_at is None:
            return None
        return (self.trace.ended_at - self.trace.started_at).total_seconds()

    @property
    def agents(self) -> list[ParticipantBinding]:
        return list(self.trace.config.agents)

    @property
    def messages(self) -> list[GameMessage]:
        out: list[GameMessage] = []
        turn = 0
        for ev in self.trace.events:
            data = ev.data or {}
            speaker = data.get("speaker")
            text = data.get("text")
            if ev.type == "message" or (speaker and text is not None):
                if speaker is None or text is None:
                    continue
                out.append(GameMessage(turn=turn, speaker=str(speaker), text=str(text), timestamp=ev.timestamp))
                turn += 1
        return out

    @property
    def metrics(self) -> dict[str, Any]:
        return dict(self.trace.metrics)

    @property
    def final_state(self) -> dict[str, Any]:
        return dict(self.trace.final_state)


class EpisodeDataset:
    """A collection of EpisodeRecord with DataFrame builders."""

    def __init__(self, records: list[EpisodeRecord]) -> None:
        self.records = list(records)

    # ---- constructors ----
    @classmethod
    def from_dir(cls, path: str | Path) -> "EpisodeDataset":
        root = Path(path)
        records: list[EpisodeRecord] = []
        for jp in sorted(root.rglob("*.json")):
            try:
                trace = EpisodeTrace.model_validate_json(jp.read_text())
            except Exception:
                continue
            records.append(EpisodeRecord(trace=trace))
        return cls(records)

    @classmethod
    def from_traces(cls, episodes: list[EpisodeTrace]) -> "EpisodeDataset":
        return cls([EpisodeRecord(trace=t) for t in episodes])

    @classmethod
    def from_store(
        cls,
        store: Any,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> "EpisodeDataset":
        """Load episodes from any configured ``EpisodeStore``.

        This is the sink-agnostic entry point: the same analysis code works
        whether the experiment wrote to SQLite, S3, Firestore, or JSON on disk.
        ``filters`` are passed to the backend, so a SQL-backed store narrows the
        query instead of loading everything and filtering in Python.
        """
        from a2a_engine.storage import iter_episodes

        return cls([
            EpisodeRecord(trace=t)
            for t in iter_episodes(store, filters=filters, limit=limit)
        ])

    @classmethod
    def from_config(
        cls,
        storage: dict[str, Any] | None = None,
        *,
        results_dir: str | Path = "./results",
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> "EpisodeDataset":
        """Build a store from a ``storage:`` block, then load it.

        Lets an analysis script reuse the same block its experiment YAML used::

            spec = load_experiment("experiments/pilot.yaml")
            ds = EpisodeDataset.from_config(resolve_storage(spec))
        """
        from a2a_engine.storage import make_store

        store = make_store(storage, results_dir=results_dir)
        return cls.from_store(store, filters=filters, limit=limit)

    # ---- container ----
    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[EpisodeRecord]:
        return iter(self.records)

    def __getitem__(self, idx: int | slice):
        if isinstance(idx, slice):
            return EpisodeDataset(self.records[idx])
        return self.records[idx]

    # ---- filtering ----
    def filter(self, predicate: Callable[[EpisodeRecord], bool]) -> "EpisodeDataset":
        return EpisodeDataset([r for r in self.records if predicate(r)])

    def filter_by(self, **kwargs: Any) -> "EpisodeDataset":
        def ok(r: EpisodeRecord) -> bool:
            for k, v in kwargs.items():
                if getattr(r, k, None) != v:
                    return False
            return True
        return self.filter(ok)

    # ---- DataFrames ----
    def to_episodes_df(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for r in self.records:
            row: dict[str, Any] = {
                "episode_uid": r.episode_uid,
                "environment_id": r.environment_id,
                "experiment_name": r.experiment_name,
                "episode_id": r.episode_id,
                "num_agents": len(r.agents),
                "num_events": len(r.trace.events),
                "num_messages": len(r.messages),
                "duration_s": r.duration_seconds,
            }
            for k, v in r.metrics.items():
                row[f"metrics_{k}"] = v
            for k, v in r.final_state.items():
                row[f"final_{k}"] = v
            rows.append(row)
        return pd.DataFrame(rows)

    def to_messages_df(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for r in self.records:
            for m in r.messages:
                rows.append({
                    "episode_uid": r.episode_uid,
                    "turn": m.turn,
                    "speaker": m.speaker,
                    "text": m.text,
                    "char_count": len(m.text),
                    "timestamp": m.timestamp,
                })
        return pd.DataFrame(rows)

    def to_events_df(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for r in self.records:
            for i, ev in enumerate(r.trace.events):
                rows.append({
                    "episode_uid": r.episode_uid,
                    "event_idx": i,
                    "type": ev.type,
                    "timestamp": ev.timestamp,
                    "data": json.dumps(ev.data, default=str),
                })
        return pd.DataFrame(rows)
