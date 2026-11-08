"""Agent-owned messages/history and response parsing."""

import json
import math
import re
from a2a_engine import LLMAgent

SYSTEM = """You are one of several coastal emergency managers facing an approaching hurricane. Each manager owns one sector of zones and can order evacuations only there. The storm's landfall zone is hidden; zones within impact_radius of it will be hit. Every round you may run one instrument (recon is precise but the flight budget is shared; model runs are noisy early and sharpen each round) and only you see the ensemble it returns until you share it. You have an attention budget of actions per round: probe, post to the public bulletin, send a direct message to one colleague (they can reply this round), write a durable note on the shared board, convene a live private meeting, or order evacuations in your sector. Data you share in a direct message or a meeting is seen only by those people; data you post on the bulletin is seen by everyone. Meetings let you talk back and forth within the round but cost attendees attention next round. Orders are irreversible, cost the zone's population, and only protect people when issued at least evacuation_lead_time rounds before landfall. Casualties cost far more than evacuations. You all share one team score. Reason about uncertainty from every ensemble you know, say what you believe and why, and respond to your colleagues. Return exactly one JSON object matching the requested turn. No markdown."""


def prompt(observation):
    return json.dumps(observation, ensure_ascii=False)


def pooled_estimate(observation):
    """Rounded mean of every ensemble member the seat knows; None when it knows nothing."""
    samples = [s for r in observation["your_readings"] for s in r["ensemble"]] + [
        s for r in observation["known_data"] for s in r["ensemble"]
    ]
    if not samples:
        return None
    return int(math.floor(sum(samples) / len(samples) + 0.5))


def parse_turn(text):
    """One JSON object per turn: {"actions": [...]}, a bare action, or a bare list."""
    try:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
        result = json.loads(clean)
    except (ValueError, TypeError):
        return {"parse_failed": True}
    if isinstance(result, list):
        return {"actions": result}
    return result if isinstance(result, dict) else {"parse_failed": True}


def storm_of(observation):
    return observation.get("season", {}).get("storm", 1)


class HurricaneAgent(LLMAgent):
    def __init__(self, client, name, *, history_window=None, season_memory="full"):
        super().__init__(client, system_prompt=SYSTEM, name=name)
        self.history = []
        self.history_window = history_window
        # Cross-storm memory policy: anything but "full" drops the provider conversation at a
        # storm boundary; the environment supplies outcomes and notes in the observation.
        self.season_memory = season_memory or "full"
        self.current_storm = None
        self.request_history_lengths = []
        self.history_allowed = lambda: True

    def window(self):
        # Bounded internal memory: only the last history_window turns reach the provider.
        if self.history_window is None:
            return list(self.history)
        return self.history[-2 * self.history_window:]

    def cross_storm_boundary(self, observation):
        storm = storm_of(observation)
        if self.current_storm is not None and storm != self.current_storm and self.season_memory != "full":
            self.history = []
        self.current_storm = storm

    def build_messages(self, observation, tools):
        self.cross_storm_boundary(observation)
        self.request_history_lengths.append(len(self.history))
        return [
            {"role": "system", "content": self.system_prompt},
            *self.window(),
            {"role": "user", "content": prompt(observation)},
        ]

    def parse_response(self, text, observation, tools):
        # A cancelled or expired runtime call cannot append provider history.
        if not self.history_allowed():
            return {"parse_failed": True}
        # Preserve the actual private provider conversation, including malformed output.
        self.history.extend(
            [
                {"role": "user", "content": prompt(observation)},
                {"role": "assistant", "content": text},
            ]
        )
        return parse_turn(text)


class ScriptedManager:
    """Deterministic inquiry from the exact same observation a model would receive.

    Probes every round (recon while the budget lasts), posts its readings and pooled estimate
    on the public bulletin, answers meetings once and leaves, ignores direct messages, and
    evacuates the recommended zones in its sector in the last effective round.
    """

    def __init__(self, name):
        self.name = name
        self.history = []
        self.request_history_lengths = []

    def plan(self, observation):
        kind = observation["turn_kind"]
        if kind == "debrief":
            result = observation["landfall_result"]
            text = (
                f"Storm {result['storm']}: landfall at zone {result['landfall_zone']}, zones "
                f"{result['hit_zones']} hit; regret {result['regret']:g}. Pooling every reading and "
                f"evacuating within impact_radius of the mean worked."
            )
            return {"actions": [{"kind": "note", "text": text}]}
        estimate = pooled_estimate(observation)
        radius = observation["impact_radius"]
        num_zones = len(observation["zones"])
        target = [z for z in range(num_zones) if abs(z - estimate) <= radius] if estimate is not None else []
        if kind == "meeting":
            said = any(
                m["participant"] == observation["participant"]
                for m in observation["current_meeting"]["transcript"]
            )
            if said:
                return {"actions": [{"kind": "leave"}]}
            text = (
                f"Pooling everything I know, I estimate landfall at zone {estimate}."
                if estimate is not None else "I have no readings to pool yet."
            )
            return {"actions": [{"kind": "say", "text": text, "share": True, "estimate": estimate,
                                 "recommend": target or None}]}
        if kind == "reply":
            return {"actions": [{"kind": "pass"}]}
        actions = []
        if observation["attention_budget"] >= 1 and not observation["probed_this_round"]:
            instrument = "recon" if observation["instruments"]["recon"]["remaining"] > 0 else "model"
            actions.append({"kind": "probe", "instrument": instrument})
        if observation["attention_budget"] >= 2:
            # The post is planned before this turn's probe lands; share:true still discloses the
            # new reading because disclosure applies when the post commits.
            parts = [f"Round {observation['round']}: sharing every reading I hold as data."]
            if estimate is not None:
                parts.append(f"Pooling everything I know, I estimate landfall at zone {estimate}.")
                parts.append(f"I recommend evacuating zones {target}.")
            actions.append({"kind": "post", "text": " ".join(parts), "share": True,
                            "estimate": estimate, "recommend": target or None})
        ordered = {z["zone"] for z in observation["zones"] if z["order"]}
        zones = [z for z in target if z in observation["your_zones"] and z not in ordered]
        if zones and observation["round"] == observation["last_effective_round"] and observation["attention_budget"] >= 3:
            actions.append({"kind": "evacuate", "zones": zones})
        return {"actions": actions or [{"kind": "pass"}]}

    async def act(self, observation, tools):
        self.request_history_lengths.append(len(self.history))
        action = self.plan(observation)
        self.history.extend(
            [
                {"role": "user", "content": prompt(observation)},
                {"role": "assistant", "content": json.dumps(action)},
            ]
        )
        return action
