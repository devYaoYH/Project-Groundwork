"""Agent-owned messages/history and response parsing."""

import json
import re
from a2a_engine import LLMAgent

SYSTEM = """You are one participant in a repeated hidden-profile decision task. Select the highest total-scoring candidate A, B, or C by pooling verified evidence with your group. Each distinct fact contributes once; shared facts repeated by several people must never be counted repeatedly. Negative contributions subtract. A new task round has NEW evidence: do not transfer factual scores between rounds, but retain communication experience. You see only your private packet and public conversation. Do not invent facts or assume unseen packets. Initial and postdiscussion reports are sealed. During free discussion, use your judgment to share evidence, ask questions, correct sums and explain preferences. All facts in a packet are verified. Distinct district achievements are distinct facts. Return one JSON object matching the requested phase. No markdown."""


def prompt(observation):
    return json.dumps(observation, ensure_ascii=False)


class HiddenProfileAgent(LLMAgent):
    def __init__(self, client, name, *, round_memory="full"):
        super().__init__(client, system_prompt=SYSTEM, name=name)
        self.history = []
        # Cross-round memory policy: anything but "full" drops the provider conversation when
        # a new decision round begins; the environment supplies the record and notes instead.
        self.round_memory = round_memory or "full"
        self.current_round = None
        self.request_history_lengths = []
        self.history_allowed = lambda: True

    def cross_round_boundary(self, observation):
        round_number = observation.get("round")
        if (
            self.current_round is not None
            and round_number != self.current_round
            and self.round_memory != "full"
        ):
            self.history = []
        self.current_round = round_number

    def build_messages(self, observation, tools):
        self.cross_round_boundary(observation)
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
        # Preserve actual private provider conversation, including malformed output.
        self.history.extend(
            [
                {"role": "user", "content": prompt(observation)},
                {"role": "assistant", "content": text},
            ]
        )
        try:
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            result = json.loads(clean)
            return result if isinstance(result, dict) else {"parse_failed": True}
        except (ValueError, TypeError):
            return {"parse_failed": True}


class ScriptedParticipant:
    """Offline evidence pooling from the exact same private/public observations."""

    def __init__(self, name):
        self.name = name
        self.history = []
        self.request_history_lengths = []

    async def act(self, observation, tools):
        self.request_history_lengths.append(len(self.history))
        facts = {
            f["statement"]: (f["candidate"], f["points"])
            for f in observation["private_packet"]
        }
        for message in observation["discussion"]:
            for line in message["text"].splitlines():
                match = re.fullmatch(r"([ABC]): ([+-]?\d+) — (.+)", line)
                if match:
                    facts[match[3]] = (match[1], int(match[2]))
        scores = {
            c: sum(n for candidate, n in facts.values() if candidate == c)
            for c in "ABC"
        }
        choice = max("ABC", key=lambda c: scores[c])
        phase = observation["phase"]
        if phase == "debrief":
            last = observation["last_round_result"]
            action = {
                "kind": "note",
                "text": f"Round {last['round']}: the group committed to {last['choice']} "
                f"({last['termination']}). Posting every packet fact as 'X: +n — statement' let everyone sum.",
            }
        elif phase in ("pre_report", "post_report"):
            action = {"choice": choice, "scores": scores}
        elif phase == "discussion":
            action = {
                "kind": "say",
                "text": "\n".join(
                    f"{f['candidate']}: {f['points']:+d} — {f['statement']}"
                    for f in observation["private_packet"]
                ),
            }
        else:
            proposal = observation["proposal"]
            action = {
                "kind": "endorse"
                if proposal and proposal["candidate"] == choice
                else "propose",
                "candidate": choice,
                "text": f"My integrated scores are {scores}.",
            }
        self.history.extend(
            [
                {"role": "user", "content": prompt(observation)},
                {"role": "assistant", "content": json.dumps(action)},
            ]
        )
        return action
