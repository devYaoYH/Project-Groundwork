"""Agent-owned messages/history and response parsing."""

import json
import math
import re
from a2a_engine import LLMAgent

SYSTEM = """You are one of several coastal emergency managers facing an approaching hurricane. Each manager owns one sector of zones and can order evacuations only there. The storm's landfall zone is hidden; zones within impact_radius of it will be hit. Every round you may run one instrument (recon is precise but the flight budget is shared; model runs are noisy early and sharpen each round) and only you see the ensemble it returns. Then the group talks: you may share your readings as data, state a public landfall estimate, and recommend zones to evacuate anywhere on the coast. Then you order evacuations in your sector. Orders are irreversible, cost the zone's population, and only protect people when issued at least evacuation_lead_time rounds before landfall. Casualties cost far more than evacuations. You all share one team score. Reason about uncertainty from every ensemble you know, say what you believe and why, and respond to your colleagues. Return exactly one JSON object matching the requested phase. No markdown."""


def prompt(observation):
    return json.dumps(observation, ensure_ascii=False)


def pooled_estimate(observation):
    """Rounded mean of every ensemble member the seat knows; None when it knows nothing."""
    participant = observation["participant"]
    samples = [s for r in observation["your_readings"] for s in r["ensemble"]] + [
        s
        for r in observation["shared_data"]
        if r["participant"] != participant
        for s in r["ensemble"]
    ]
    if not samples:
        return None
    return int(math.floor(sum(samples) / len(samples) + 0.5))


class HurricaneAgent(LLMAgent):
    def __init__(self, client, name):
        super().__init__(client, system_prompt=SYSTEM, name=name)
        self.history = []
        self.request_history_lengths = []
        self.history_allowed = lambda: True

    def build_messages(self, observation, tools):
        self.request_history_lengths.append(len(self.history))
        return [
            {"role": "system", "content": self.system_prompt},
            *self.history,
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
        try:
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
            result = json.loads(clean)
            return result if isinstance(result, dict) else {"parse_failed": True}
        except (ValueError, TypeError):
            return {"parse_failed": True}


class ScriptedManager:
    """Deterministic inquiry from the exact same observation a model would receive."""

    def __init__(self, name):
        self.name = name
        self.history = []
        self.request_history_lengths = []

    async def act(self, observation, tools):
        self.request_history_lengths.append(len(self.history))
        phase = observation["phase"]
        estimate = pooled_estimate(observation)
        radius = observation["impact_radius"]
        num_zones = len(observation["zones"])
        target = (
            [z for z in range(num_zones) if abs(z - estimate) <= radius]
            if estimate is not None
            else []
        )
        if phase == "experiment":
            instrument = "recon" if observation["instruments"]["recon"]["remaining"] > 0 else "model"
            action = {"kind": "probe", "instrument": instrument}
        elif phase == "discussion":
            unshared = [r for r in observation["your_readings"] if not r["shared"]]
            parts = [
                f"Round {observation['round']}: "
                + (
                    "my new readings are "
                    + "; ".join(f"{r['instrument']} {r['ensemble']}" for r in unshared)
                    if unshared
                    else "no new readings"
                )
                + "."
            ]
            if estimate is not None:
                parts.append(f"Pooling everything I know, I estimate landfall at zone {estimate}.")
                parts.append(f"I recommend evacuating zones {target}.")
            action = {
                "kind": "say",
                "text": " ".join(parts),
                "share": True,
                "estimate": estimate,
                "recommend": target or None,
            }
        else:
            ordered = {z["zone"] for z in observation["zones"] if z["order"]}
            zones = [z for z in target if z in observation["your_zones"] and z not in ordered]
            if zones and observation["round"] == observation["last_effective_round"]:
                action = {"kind": "evacuate", "zones": zones}
            else:
                action = {"kind": "hold"}
        self.history.extend(
            [
                {"role": "user", "content": prompt(observation)},
                {"role": "assistant", "content": json.dumps(action)},
            ]
        )
        return action
