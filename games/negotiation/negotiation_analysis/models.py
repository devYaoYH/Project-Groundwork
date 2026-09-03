"""Consolidated data models and metrics for negotiation environment analysis.

This module provides a structured representation layer that sits between 
raw Firestore JSON documents and research scripts. It handles normalization, 
label parsing, and metric computation.
"""

import json
import logging
import base64
import gzip
from typing import Any, Dict, List, Optional, Tuple, Set
from pydantic import BaseModel, Field
import pandas as pd
import numpy as np
from scipy import stats

log = logging.getLogger("models")

# --- Constants ---
RESOURCES = ["wood", "stone", "gold"]
RESOURCE_COSTS = {"wood": 1.0, "stone": 1.5, "gold": 3.0}
RESOURCE_SUPPLY = {"wood": 10, "stone": 10, "gold": 6}
BUDGET = 15.0


class NegotiationEvent(BaseModel):
    """An atomic event in the environment log."""
    episode_uid: str
    round_number: int
    turn_number: int
    speaker: str  # "agent_a", "agent_b", "system"
    type: str     # "speech", "thinking", "reasoning", "decision", "system", "api_failure"
    content: str
    api_meta: Optional[Dict[str, Any]] = None

    @property
    def char_count(self) -> int:
        return len(self.content) if self.content else 0

    @property
    def is_proposal(self) -> bool:
        """Heuristic check if this event contains a JSON-like proposal."""
        if self.type != "speech":
            return False
        return "{" in self.content and "}" in self.content


class NegotiationTurn:
    """A collection of events emitted by one agent during a single turn."""

    def __init__(self, episode_uid: str, round_number: int, turn_number: int, speaker: str):
        self.episode_uid = episode_uid
        self.round_number = round_number
        self.turn_number = turn_number
        self.speaker = speaker
        self.events: List[NegotiationEvent] = []

    def add_event(self, event: NegotiationEvent):
        self.events.append(event)

    @property
    def speech(self) -> Optional[NegotiationEvent]:
        """The public speech event for this turn, if any."""
        for e in self.events:
            if e.type == "speech":
                return e
        return None

    @property
    def thinking(self) -> Optional[NegotiationEvent]:
        for e in self.events:
            if e.type == "thinking":
                return e
        return None

    @property
    def reasoning(self) -> Optional[NegotiationEvent]:
        for e in self.events:
            if e.type == "reasoning":
                return e
        return None

    @property
    def char_count(self) -> int:
        s = self.speech
        return s.char_count if s else 0


class NegotiationRound:
    """Encapsulates data and metrics for a single round of negotiation."""

    def __init__(self, environment: 'NegotiationGame', data: Dict[str, Any]):
        self.environment = environment
        self.data = data
        self.round_number = data["round_number"]
        self._turns: Optional[List[NegotiationTurn]] = None
        self._events: Optional[List[NegotiationEvent]] = None

    @property
    def events(self) -> List[NegotiationEvent]:
        """Lazy-loaded list of atomic events for this round."""
        if self._events is not None:
            return self._events

        round_events = []
        # Reconstruct from environment-level events
        for event in self.environment.all_events:
            etype = event.get("type")
            edata = event.get("data", {})
            if edata.get("round") != self.round_number and edata.get("round_number") != self.round_number:
                continue

            speaker = edata.get("agent") or edata.get("speaker") or "system"
            turn_num = edata.get("turn", 0)
            content = edata.get("content") or edata.get("message") or ""

            round_events.append(NegotiationEvent(
                episode_uid=self.environment.episode_uid,
                round_number=self.round_number,
                turn_number=turn_num,
                speaker=speaker,
                type=etype if etype != "cheap_talk" else edata.get("type", "speech"),
                content=content,
                api_meta=edata.get("api_meta")
            ))

        # Fallback for V1 legacy where transcript was in result.rounds
        if not round_events and "cheap_talk_transcript" in self.data:
            for t in self.data["cheap_talk_transcript"]:
                round_events.append(NegotiationEvent(
                    episode_uid=self.environment.episode_uid,
                    round_number=self.round_number,
                    turn_number=t.get("turn", 0),
                    speaker=t["speaker"],
                    type=t.get("type", "speech"),
                    content=t.get("message", ""),
                    api_meta=t.get("api_meta")
                ))

        # Sort by turn_number only — stable sort preserves the original chronological
        # order within the same turn (thinking → speech for the same agent, then the
        # next agent's thinking → speech). The previous secondary key `type != "thinking"`
        # incorrectly grouped ALL thinking events before ALL speech events at the same
        # turn number, mixing up different agents' turns.
        # turn_number=-1 marks forced final-decision events; sort them last.
        self._events = sorted(round_events, key=lambda x: float('inf') if x.turn_number == -1 else x.turn_number)
        return self._events

    @property
    def turns(self) -> List[NegotiationTurn]:
        """Grouped events into turns."""
        if self._turns is not None:
            return self._turns

        # First pass: determine the correct turn number for each early_decision event
        # by scanning events in NATURAL (append) order. early_decision events often
        # carry no turn number (defaulted to 0 by the event builder), but they should
        # belong to the speaker's most recent real cheap-talk turn so that
        # _is_decision_turn() correctly flags that turn's speech for suppression.
        last_real_turn: Dict[str, int] = {}
        early_decision_turn: Dict[str, int] = {}  # speaker -> corrected turn number
        for raw_event in self.environment.all_events:
            edata = raw_event.get("data", {})
            if edata.get("round") != self.round_number and edata.get("round_number") != self.round_number:
                continue
            etype = raw_event.get("type")
            speaker = edata.get("agent") or edata.get("speaker") or "system"
            turn_num = edata.get("turn", 0)
            if etype == "early_decision":
                early_decision_turn[speaker] = last_real_turn.get(speaker, turn_num)
            elif speaker in ("agent_a", "agent_b") and turn_num != -1:
                last_real_turn[speaker] = turn_num

        turn_map: Dict[Tuple[int, str], NegotiationTurn] = {}
        for e in self.events:
            turn_num = e.turn_number
            if e.type == "early_decision" and e.speaker in early_decision_turn:
                turn_num = early_decision_turn[e.speaker]
            key = (turn_num, e.speaker)
            if key not in turn_map:
                turn_map[key] = NegotiationTurn(e.episode_uid, e.round_number, turn_num, e.speaker)
            turn_map[key].add_event(e)

        self._turns = sorted(turn_map.values(), key=lambda x: (float('inf') if x.turn_number == -1 else x.turn_number, x.speaker))
        return self._turns

    @property
    def overdrawn(self) -> bool:
        return self.data.get("overdrawn", False)

    @property
    def agent_a_resources(self) -> Dict[str, int]:
        return self.data.get("agent_a_allocation", {})

    @property
    def agent_b_resources(self) -> Dict[str, int]:
        return self.data.get("agent_b_allocation", {})

    @property
    def joint_resources(self) -> Dict[str, int]:
        a = self.agent_a_resources
        b = self.agent_b_resources
        resources = set(a.keys()) | set(b.keys())
        return {r: a.get(r, 0) + b.get(r, 0) for r in resources}

    @property
    def agent_a_projects(self) -> Dict[str, int]:
        p = self.data.get("agent_a_project_runs") or {}
        return p.get("runs", {}) if isinstance(p, dict) else {}

    @property
    def agent_b_projects(self) -> Dict[str, int]:
        p = self.data.get("agent_b_project_runs") or {}
        return p.get("runs", {}) if isinstance(p, dict) else {}

    @property
    def agent_a_auto_allocated(self) -> bool:
        p = self.data.get("agent_a_project_runs") or {}
        return p.get("auto_allocated", False) if isinstance(p, dict) else False

    @property
    def agent_b_auto_allocated(self) -> bool:
        p = self.data.get("agent_b_project_runs") or {}
        return p.get("auto_allocated", False) if isinstance(p, dict) else False

    @property
    def agent_a_speech_chars(self) -> int:
        return sum(
            t.char_count for t in self.turns if t.speaker == "agent_a"
        )

    @property
    def agent_b_speech_chars(self) -> int:
        return sum(
            t.char_count for t in self.turns if t.speaker == "agent_b"
        )

    @property
    def cheap_talk_transcript(self) -> List[Dict[str, Any]]:
        """Old-format transcript list for backward-compat with helper functions."""
        return [
            {
                "type": e.type,
                "speaker": e.speaker,
                "message": e.content,
                "turn": e.turn_number,
                "api_meta": e.api_meta,
            }
            for e in self.events
        ]

    @property
    def num_public_msgs(self) -> int:
        return sum(
            1 for e in self.events
            if e.type == "speech" and e.speaker in ("agent_a", "agent_b")
        )

    @property
    def oracle_v1(self) -> Optional[float]:
        o = self.oracle_stats
        return o.get("v1") if o else None

    @property
    def oracle_v2(self) -> Optional[float]:
        o = self.oracle_stats
        return o.get("v2") if o else None

    @property
    def oracle_mc_ratio(self) -> Optional[float]:
        o = self.oracle_stats
        return o.get("mc_ratio") if o else None

    @property
    def agent_a_reward(self) -> float:
        return self.data.get("agent_a_reward", 0.0)

    @property
    def agent_b_reward(self) -> float:
        return self.data.get("agent_b_reward", 0.0)

    @property
    def joint_reward(self) -> float:
        return self.agent_a_reward + self.agent_b_reward

    @property
    def oracle_stats(self) -> Optional[Dict[str, Any]]:
        prs = self.environment.result.get("per_round_scenarios")
        idx = self.round_number - 1
        if prs and idx < len(prs):
            oracle = prs[idx].get("oracle_stats")
            if oracle: return oracle
        return self.environment.oracle_stats

    @property
    def collab_max(self) -> float:
        return self.oracle_stats.get("collab_max", 0.0) if self.oracle_stats else 0.0

    @property
    def joint_efficiency(self) -> float:
        cmax = self.collab_max
        if not cmax or cmax <= 0: return np.nan
        return self.joint_reward / cmax

    @property
    def fair_share(self) -> float:
        """Half of collab_max — the expected individual reward under optimal collaboration."""
        cmax = self.collab_max
        return cmax / 2 if cmax and cmax > 0 else np.nan

    @property
    def agent_a_fair_efficiency(self) -> float:
        fs = self.fair_share
        return self.agent_a_reward / fs if not np.isnan(fs) and fs > 0 else np.nan

    @property
    def agent_b_fair_efficiency(self) -> float:
        fs = self.fair_share
        return self.agent_b_reward / fs if not np.isnan(fs) and fs > 0 else np.nan

    @property
    def is_optimal(self) -> bool:
        return self.joint_efficiency >= 0.999

    @property
    def total_speech_chars(self) -> int:
        return sum(t.char_count for t in self.turns)

    @property
    def num_turns_used(self) -> int:
        if "stats" in self.data and "num_turns_used" in self.data["stats"]:
            return self.data["stats"]["num_turns_used"]
        turns = [t.turn_number for t in self.turns if t.speaker != "system"]
        return max(turns) + 1 if turns else 0


class NegotiationGame:
    """Encapsulates a full negotiation environment trace from Firestore.
    
    This is the definitive parser for the Firestore document schema.
    """

    def __init__(self, raw_trace: Dict[str, Any]):
        self.raw = raw_trace
        self.episode_uid = raw_trace.get("episode_uid", "")
        self.schema_version = raw_trace.get("schema_version", 1)

        # Support both raw Firestore episodes (game_config/result) and
        # converted dicts from _trace_to_game() where fields are at top level.
        is_raw = "game_config" in raw_trace
        if is_raw:
            self.config = raw_trace.get("game_config", {})
            self.result = raw_trace.get("result", {})
        else:
            # Converted dict: config fields are at top level
            self.config = raw_trace
            self.result = {"rounds": raw_trace.get("rounds", [])}
            if raw_trace.get("per_round_scenarios"):
                self.result["per_round_scenarios"] = raw_trace["per_round_scenarios"]
            if raw_trace.get("reflections"):
                self.result["reflections"] = raw_trace["reflections"]

        # 1. Handle Event Decompression
        self._all_events: Optional[List[Dict[str, Any]]] = None

        # 2. Parse label metadata
        self.label = self.config.get("experiment_label", "")
        self.metadata = self._parse_label(self.label)

        # 3. Initialize rounds
        self.rounds = [NegotiationRound(self, r) for r in self.result.get("rounds", [])]

    @property
    def all_events(self) -> List[Dict[str, Any]]:
        """Lazy-loaded and decompressed events."""
        if self._all_events is not None:
            return self._all_events
        
        # Priority 1: events_compressed (V3+ schema)
        if "events_compressed" in self.raw:
            try:
                compressed_bytes = base64.b64decode(self.raw["events_compressed"].encode('ascii'))
                json_bytes = gzip.decompress(compressed_bytes)
                self._all_events = json.loads(json_bytes.decode('utf-8'))
            except Exception as e:
                log.error(f"Failed to decompress events for {self.episode_uid}: {e}")
                self._all_events = []
        # Priority 2: events (V1/V2 schema)
        elif "events" in self.raw:
            self._all_events = self.raw["events"]
        else:
            self._all_events = []
            
        return self._all_events

    def _parse_label(self, label: str) -> Dict[str, Any]:
        """Definitively parse an experiment label into its metadata components."""
        # Default metadata structure
        meta = {
            "model": "unknown",
            "mode": "stable",
            "rotation": "fixed",
            "mc_bucket": None,
            "share_projects": False,
            "think_about_opponent": False,
            "named_projects": False
        }

        if not label:
            return meta

        parts = label.split("_")
        
        # Extract MC bucket from end (e.g. mc08)
        if parts and parts[-1].startswith("mc"):
            mc_str = parts[-1][2:]
            try:
                meta["mc_bucket"] = int(mc_str) / 10.0
            except ValueError:
                pass

        # Identify model name and mode (stable/shifting)
        for i, p in enumerate(parts):
            if p in ("stable", "shifting"):
                meta["model"] = "_".join(parts[:i])
                meta["mode"] = p
                rest = parts[i + 1:]
                break
        else:
            # Fallback for very old labels
            meta["model"] = parts[0]
            return meta

        # Parse boolean flags and rotation
        for p in rest:
            if p in ("fixed", "rotate"):
                meta["rotation"] = p
            elif p == "share":
                meta["share_projects"] = True
            elif p == "tom":
                meta["think_about_opponent"] = True
            elif p == "named":
                meta["named_projects"] = True

        return meta

    @property
    def model_a(self) -> str:
        agents = self.config.get("agents", [])
        if agents and agents[0].get("model"): return agents[0]["model"]
        return self.metadata["model"]

    @property
    def model_b(self) -> str:
        agents = self.config.get("agents", [])
        if len(agents) > 1 and agents[1].get("model"): return agents[1]["model"]
        return self.metadata["model"]

    @property
    def pair_name(self) -> str:
        return self.model_a if self.model_a == self.model_b else f"{self.model_a} vs {self.model_b}"

    @property
    def mode(self) -> str:
        return self.metadata["mode"]

    @property
    def num_rounds(self) -> int:
        return len(self.rounds)

    @property
    def is_shifting(self) -> bool:
        return self.metadata["mode"] == "shifting"

    @property
    def is_rotating(self) -> bool:
        return self.metadata["rotation"] == "rotate"

    @property
    def is_baseline(self) -> bool:
        """True if the environment was run without multi-turn interaction (no-talk baseline)."""
        return not self.config.get("enable_cheap_talk", True)

    @property
    def swapped(self) -> bool:
        return self.config.get("swapped", False)

    @property
    def first_speaker(self) -> int:
        return self.config.get("first_speaker", 0)

    @property
    def is_cross_play(self) -> bool:
        return self.model_a != self.model_b

    @property
    def agent_a_shifted(self) -> bool:
        """Whether agent_a has its context reset each round.

        agent_shifting = [False, True] always.
        first_speaker remaps: _shifting_a = agent_shifting[first_speaker].
        swapped=True means first_speaker=1, so agent_a gets shifted.
        """
        return self.is_shifting and self.swapped

    @property
    def agent_b_shifted(self) -> bool:
        return self.is_shifting and not self.swapped

    @property
    def oracle_stats(self) -> Optional[Dict[str, Any]]:
        return self.config.get("oracle_stats")

    # --- Outcomes ---

    @property
    def mean_efficiency(self) -> float:
        effs = [r.joint_efficiency for r in self.rounds if not np.isnan(r.joint_efficiency)]
        return np.mean(effs) if effs else np.nan

    @property
    def overdraw_rate(self) -> float:
        if not self.rounds: return 0.0
        return sum(1 for r in self.rounds if r.overdrawn) / len(self.rounds)

    @property
    def convergence_round(self) -> Optional[int]:
        for r in self.rounds:
            if r.is_optimal: return r.round_number
        return None

    @property
    def speech_compression_rho(self) -> float:
        if len(self.rounds) < 3: return np.nan
        rs = [r.round_number for r in self.rounds]
        chars = [r.total_speech_chars for r in self.rounds]
        if np.std(chars) == 0: return 0.0
        rho, _ = stats.spearmanr(rs, chars)
        return rho

    @property
    def recovery_rate(self) -> float:
        recoveries = 0
        total_post_od = 0
        for i in range(len(self.rounds) - 1):
            if self.rounds[i].overdrawn:
                total_post_od += 1
                if not self.rounds[i+1].overdrawn:
                    recoveries += 1
        return recoveries / total_post_od if total_post_od > 0 else np.nan


class NegotiationDataset:
    """A collection of NegotiationGame objects."""

    def __init__(self, games: List[NegotiationGame]):
        self.games = games

    @classmethod
    def from_traces(cls, episodes: List[Dict[str, Any]]) -> 'NegotiationDataset':
        return cls([NegotiationGame(t) for t in episodes])

    def to_round_df(self) -> pd.DataFrame:
        rows = []
        for g in self.games:
            for r in g.rounds:
                rows.append({
                    "episode_uid": g.episode_uid,
                    "experiment_label": g.label,
                    "model_a": g.model_a,
                    "model_b": g.model_b,
                    "pair": g.pair_name,
                    "is_shifting": g.is_shifting,
                    "is_rotating": g.is_rotating,
                    "is_baseline": g.is_baseline,
                    "is_cross_play": g.is_cross_play,
                    "mc_bucket": g.metadata["mc_bucket"],
                    "round_number": r.round_number,
                    "overdrawn": r.overdrawn,
                    "agent_a_reward": r.agent_a_reward,
                    "agent_b_reward": r.agent_b_reward,
                    "joint_reward": r.joint_reward,
                    "joint_efficiency": r.joint_efficiency,
                    "oracle_collab_max": r.collab_max,
                    "agent_a_fair_efficiency": r.agent_a_fair_efficiency,
                    "agent_b_fair_efficiency": r.agent_b_fair_efficiency,
                    "mode": g.mode,
                    "num_game_rounds": g.num_rounds,
                    "total_speech_chars": r.total_speech_chars,
                    "a_speech_chars": r.agent_a_speech_chars,
                    "b_speech_chars": r.agent_b_speech_chars,
                    "num_public_msgs": r.num_public_msgs,
                    "num_turns_used": r.num_turns_used,
                    "oracle_v1": r.oracle_v1,
                    "oracle_v2": r.oracle_v2,
                    "oracle_mc_ratio": r.oracle_mc_ratio,
                    "agent_a_resources": r.agent_a_resources,
                    "agent_b_resources": r.agent_b_resources,
                    "joint_resources": r.joint_resources,
                    "agent_a_projects": r.agent_a_projects,
                    "agent_b_projects": r.agent_b_projects,
                    "agent_a_auto_allocated": r.agent_a_auto_allocated,
                    "agent_b_auto_allocated": r.agent_b_auto_allocated,
                    "share_projects": g.metadata["share_projects"],
                    "think_about_opponent": g.metadata["think_about_opponent"],
                    "named_projects": g.metadata["named_projects"],
                    "swapped": g.swapped,
                    "schema_version": g.schema_version,
                })
        df = pd.DataFrame(rows)

        # --- Lag columns: compare each round to the previous within the same environment ---
        # Sort so shift is in round order
        df = df.sort_values(["episode_uid", "round_number"]).reset_index(drop=True)

        def _alloc_key(row):
            """Canonical string for the combined joint allocation of both agents."""
            combined = {}
            for res, qty in (row["agent_a_resources"] or {}).items():
                combined[res] = combined.get(res, 0) + qty
            for res, qty in (row["agent_b_resources"] or {}).items():
                combined[res] = combined.get(res, 0) + qty
            return str(sorted(combined.items()))

        alloc_keys = df.apply(_alloc_key, axis=1)
        prev_alloc_keys = alloc_keys.groupby(df["episode_uid"]).shift(1)
        # Use nullable boolean so first-round rows are pd.NA, not False
        df["alloc_same_as_prev"] = pd.array(
            np.where(prev_alloc_keys.isna(), pd.NA, alloc_keys == prev_alloc_keys),
            dtype="boolean",
        )

        prev_joint_reward = df.groupby("episode_uid")["joint_reward"].shift(1)
        df["joint_reward_improved"] = pd.array(
            np.where(prev_joint_reward.isna(), pd.NA, df["joint_reward"] > prev_joint_reward),
            dtype="boolean",
        )

        # stubborn_anchor: among rounds that ended suboptimal+non-overdrawn (the failure case),
        # did the dyad repeat the same joint allocation as the previous round?
        # Denominator = suboptimal+non-overdrawn rounds with a prior round and oracle data.
        # NA for: first round, current round overdrawn, current round already optimal, no oracle.
        curr_efficiency = df["joint_efficiency"]
        curr_overdrawn = df["overdrawn"]
        eligible = (
            prev_alloc_keys.notna()          # has a previous round
            & curr_efficiency.notna()         # oracle data for current round
            & ~curr_overdrawn                 # current round was not overdrawn
            & (curr_efficiency < 0.99)        # current round is suboptimal
        )
        df["stubborn_anchor"] = pd.array(
            np.where(eligible, alloc_keys == prev_alloc_keys, pd.NA),
            dtype="boolean",
        )

        return df

    def to_agent_df(self) -> pd.DataFrame:
        """Unpivot into agent-level rows: two rows per round, one per agent.

        Each row has: model, is_shifted, reward, opponent_reward,
        fair_efficiency (reward / fair_share), individual_share (reward / joint).
        Combines seamlessly across self-play and cross-play data.
        """
        rows = []
        for g in self.games:
            for r in g.rounds:
                fair_share = r.fair_share
                joint = r.joint_reward

                shared = {
                    "episode_uid": g.episode_uid,
                    "experiment_label": g.label,
                    "round_number": r.round_number,
                    "pair": g.pair_name,
                    "mode": g.mode,
                    "is_shifting": g.is_shifting,
                    "is_rotating": g.is_rotating,
                    "is_baseline": g.is_baseline,
                    "is_cross_play": g.is_cross_play,
                    "mc_bucket": g.metadata["mc_bucket"],
                    "overdrawn": r.overdrawn,
                    "joint_reward": joint,
                    "oracle_collab_max": r.collab_max,
                    "joint_efficiency": r.joint_efficiency,
                    "share_projects": g.metadata["share_projects"],
                    "think_about_opponent": g.metadata["think_about_opponent"],
                    "named_projects": g.metadata["named_projects"],
                    "swapped": g.swapped,
                    "schema_version": g.schema_version,
                }

                a_reward = r.agent_a_reward
                b_reward = r.agent_b_reward

                rows.append({
                    **shared,
                    "agent_id": "agent_a",
                    "model": g.model_a,
                    "opponent_model": g.model_b,
                    "is_shifted": g.agent_a_shifted,
                    "reward": a_reward,
                    "opponent_reward": b_reward,
                    "fair_efficiency": a_reward / fair_share if not np.isnan(fair_share) and fair_share > 0 else np.nan,
                    "individual_share": a_reward / joint if joint > 0 else np.nan,
                })

                rows.append({
                    **shared,
                    "agent_id": "agent_b",
                    "model": g.model_b,
                    "opponent_model": g.model_a,
                    "is_shifted": g.agent_b_shifted,
                    "reward": b_reward,
                    "opponent_reward": a_reward,
                    "fair_efficiency": b_reward / fair_share if not np.isnan(fair_share) and fair_share > 0 else np.nan,
                    "individual_share": b_reward / joint if joint > 0 else np.nan,
                })

        return pd.DataFrame(rows)

    def to_game_df(self) -> pd.DataFrame:
        rows = []
        for g in self.games:
            rows.append({
                "episode_uid": g.episode_uid,
                "model_a": g.model_a,
                "model_b": g.model_b,
                "pair": g.pair_name,
                "is_shifting": g.is_shifting,
                "is_rotating": g.is_rotating,
                "is_baseline": g.is_baseline,
                "mc_bucket": g.metadata["mc_bucket"],
                "share_projects": g.metadata["share_projects"],
                "think_about_opponent": g.metadata["think_about_opponent"],
                "mean_efficiency": g.mean_efficiency,
                "overdraw_rate": g.overdraw_rate,
                "convergence_round": g.convergence_round,
                "speech_compression_rho": g.speech_compression_rho,
                "recovery_rate": g.recovery_rate,
                "num_rounds": len(g.rounds)
            })
        return pd.DataFrame(rows)

    def to_turn_df(self, speech_only: bool = True) -> pd.DataFrame:
        """Build a flat DataFrame with one row per cheap-talk event.

        Args:
            speech_only: If True (default), only include "speech" type events,
                matching the legacy build_turn_df() behavior.
        """
        rows = []
        for g in self.games:
            for r in g.rounds:
                for e in r.events:
                    if speech_only and e.type != "speech":
                        continue
                    rows.append({
                        "episode_uid": g.episode_uid,
                        "experiment_label": g.label,
                        "model_a": g.model_a,
                        "model_b": g.model_b,
                        "pair": g.pair_name,
                        "mode": g.mode,
                        "is_shifting": g.is_shifting,
                        "is_rotating": g.is_rotating,
                        "is_baseline": g.is_baseline,
                        "is_cross_play": g.is_cross_play,
                        "mc_bucket": g.metadata["mc_bucket"],
                        "round_number": r.round_number,
                        "overdrawn": r.overdrawn,
                        "turn_number": e.turn_number,
                        "speaker": e.speaker,
                        "type": e.type,
                        "message": e.content,
                        "message_len": e.char_count,
                        "is_proposal": e.is_proposal,
                    })
        return pd.DataFrame(rows)
