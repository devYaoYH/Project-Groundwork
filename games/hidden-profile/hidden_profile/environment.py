"""Repeated four-participant hidden-profile decisions in one episode session."""

import asyncio
import uuid
from typing import Literal
from pydantic import Field, model_validator
from a2a_engine import EventLog, EpisodeConfigBase, EpisodeTrace, register_environment
from a2a_engine.llm.factory import make_llm_client
from .agents import HiddenProfileAgent, ScriptedParticipant
from .bank import load_bank, project
from .declaration import DECLARATION


class HiddenProfileConfig(EpisodeConfigBase):
    environment_id: str = "hidden_profile"
    num_agents: Literal[4] = 4
    item_id: str | None = None
    condition: str | None = None
    sequence_id: str = "caucus_hidden_01"
    round_item_ids: list[str] = Field(
        default_factory=lambda: ["caucus_hidden_01", "relief_hidden_01"],
        min_length=2,
        max_length=20,
    )
    discussion_cycles: int = Field(default=6, ge=1, le=20)
    commitment_cycles: int = Field(default=3, ge=2, le=20)
    max_message_chars: int = Field(default=6000, ge=100, le=16000)
    turn_timeout_s: float = Field(default=120, gt=0, le=600)

    @model_validator(mode="before")
    @classmethod
    def select_frozen_sequence(cls, value):
        if isinstance(value, dict) and (
            value.get("item_id") or value.get("sequence_id")
        ):
            value = dict(value)
            try:
                selected_id = value.get("item_id") or value["sequence_id"]
                row = load_bank().get(selected_id).raw
                if (
                    value.get("sequence_id")
                    and value["sequence_id"] != row["params"]["sequence_id"]
                ):
                    raise ValueError("sequence ID differs from selected frozen item")
            except KeyError as exc:
                raise ValueError("unknown selected profile item") from exc
            sequence = row["params"]["round_item_ids"]
            if "round_item_ids" in value and value["round_item_ids"] != sequence:
                raise ValueError("round sequence differs from selected frozen item")
            if (
                value.get("condition") is not None
                and value["condition"] != row["condition"]
            ):
                raise ValueError("condition differs from selected frozen allocation")
            value.update(
                round_item_ids=sequence,
                condition=row["condition"],
                sequence_id=row["params"]["sequence_id"],
            )
        return value

    @model_validator(mode="after")
    def valid_items(self):
        bank = load_bank()
        if self.condition not in (None, "hidden", "full"):
            raise ValueError("unknown profile condition")
        for item in self.round_item_ids:
            bank.get(item)
        return self


class HiddenProfileEnvironment:
    def __init__(
        self,
        config,
        dry_run=False,
        *,
        episode=None,
        runtime_manager=None,
        runtime_context=None,
    ):
        from a2a_engine.remote.lobby import open_episode

        self.config = (
            config
            if isinstance(config, HiddenProfileConfig)
            else HiddenProfileConfig(**config)
        )
        self.dry_run = dry_run
        self.events = EventLog.from_config(self.config)
        self.bank = load_bank()
        self.rows = [self.bank.get(i).raw for i in self.config.round_item_ids]
        self._owns_episode = episode is None
        self.episode = episode or open_episode(
            runtime_manager,
            self.config.model_dump(),
            scripted=dry_run,
            runtime_context=runtime_context,
        )
        self.runtime = self.episode.managed
        self.agents = []
        try:
            for seat in range(4):
                cfg = (
                    self.config.agents[seat].model_dump()
                    if seat < len(self.config.agents)
                    else {
                        "type": "scripted" if dry_run else "llm",
                        "model": "gpt-4o-mini",
                    }
                )
                name = (
                    cfg.get("id") or cfg.get("participant_id") or f"participant_{seat}"
                )
                if cfg.get("runtime", "in_process") != "in_process":
                    from .remote import RemoteProfileAgent

                    if self.runtime is None:
                        raise ValueError("remote participant requires runtime context")
                    agent = RemoteProfileAgent(
                        self.runtime,
                        seat,
                        timeout_s=self.config.turn_timeout_s,
                        name=name,
                    )
                elif dry_run or cfg.get("type") == "scripted":
                    agent = ScriptedParticipant(name)
                else:
                    agent = HiddenProfileAgent(make_llm_client(cfg), name)
                self.agents.append(agent)
        except BaseException:
            if self._owns_episode:
                self.episode.close()
            raise

    @classmethod
    def build_program(cls):
        from .workflow import build_program

        return build_program()

    @classmethod
    def runtime_capability(cls):
        from a2a_engine.workflow_runtime import RuntimeCapability
        from a2a_engine.remote.contract import PROTOCOL_VERSION_V2
        from .remote import PROFILE_TOOLS

        return RuntimeCapability(
            PROFILE_TOOLS, protocols=frozenset({PROTOCOL_VERSION_V2})
        )

    def observation(self, state):
        row = self.rows[state["round_index"]]
        phase = state["phase"]
        instructions = {
            "pre_report": 'Sealed initial report using ONLY your available packet: {"choice":"A|B|C","scores":{"A":number,"B":number,"C":number}}.',
            "discussion": 'Free group discussion. You may disclose your packet, ask questions, discuss scores or pass. Other packets may have different evidence. Respond {"kind":"say","text":"your message"} or {"kind":"pass"}.',
            "post_report": 'Sealed postdiscussion report using your packet and current public discussion: {"choice":"A|B|C","scores":{"A":number,"B":number,"C":number}}.',
            "commitment": 'Commitment is separate from your sealed report. If no proposal, propose your preferred candidate. If you agree with the active proposal, explicitly endorse its candidate. A new proposal clears all endorsements. Unanimity requires every participant to endorse. Respond {"kind":"propose|endorse|pass","candidate":"A|B|C","text":"optional reason"}.',
        }
        return {
            "round": state["round_index"] + 1,
            "rounds_total": len(self.rows),
            "participant": state["seat"] + 1,
            "phase": phase,
            "task": row["title"],
            "candidates": ["A", "B", "C"],
            "rule": "Sum each distinct verified fact once. Repeating the same fact does not add points. Choose the greatest total. Ties prefer A then B then C.",
            "private_packet": project(row, state["seat"]),
            "discussion": list(state["discussion"]),
            "commitment_messages": list(state["commitment_messages"])
            if phase == "commitment"
            else [],
            "proposal": state["proposal"] if phase == "commitment" else None,
            "endorsed_participants": sorted(state["endorsements"])
            if phase == "commitment"
            else [],
            "turns_remaining": state["phase_limit"] - state["phase_turn"],
            "response_instruction": instructions[phase],
        }

    def run(self):
        return asyncio.run(self._run_async())

    async def _run_async(self):
        from .workflow import GameContext
        from a2a_engine.remote.lobby import Registration

        self.events.append(
            "game_start",
            data={
                "rounds": len(self.rows),
                "num_agents": 4,
                "item_bank_sha256": self.bank.item_bank_sha256,
            },
        )
        registrations = {
            i: Registration(
                {
                    "role": "participant",
                    "participant": i + 1,
                    "participant_id": self.agents[i].name,
                }
            )
            for i in range(4)
        }
        try:
            with self.episode.started(
                registrations,
                protocols=self.runtime_capability().protocols,
                register_timeout_s=self.config.turn_timeout_s,
            ) as roster:
                state = await self.build_program().ainvoke(
                    {"round_index": 0, "results": []},
                    context=GameContext(self, roster),
                    config={
                        "recursion_limit": 30
                        + len(self.rows)
                        * (
                            4
                            * (
                                self.config.discussion_cycles
                                + self.config.commitment_cycles
                                + 2
                            )
                            * 4
                            + 30
                        )
                    },
                )
            return self.trace(state)
        finally:
            if self._owns_episode:
                self.episode.close()

    def trace(self, state):
        results = state["results"]
        messages = [e for e in self.events.all() if e.type == "message"]
        metrics = {
            "rounds_completed": len(results),
            "optimal_rounds": sum(r["optimal_choice"] for r in results),
            "optimal_choice_rate": sum(r["optimal_choice"] for r in results)
            / len(results),
            "no_consensus_rounds": sum(r["choice"] is None for r in results),
            "decision_regret": sum(
                r["regret"] for r in results if r["regret"] is not None
            ),
            "messages": len(messages),
            "dialogue_words": sum(len(e.data["text"].split()) for e in messages),
            "invalid_actions": sum(
                e.type == "invalid_action" for e in self.events.all()
            ),
            "preference_revisions": sum(r["preference_revisions"] for r in results),
            "post_score_absolute_error": sum(
                r["post_score_absolute_error"] for r in results
            ),
            "valid_pre_reports": sum(len(r["pre_reports"]) for r in results),
            "valid_post_reports": sum(r["valid_post_reports"] for r in results),
            "missing_private_reports": sum(
                8 - len(r["pre_reports"]) - len(r["post_reports"]) for r in results
            ),
        }
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "rounds": results,
                "terminated_reason": "completed",
                "same_participants": True,
            },
            metrics=metrics,
        )


register_environment(
    "hidden_profile",
    HiddenProfileEnvironment,
    package="hidden-profile",
    declaration=DECLARATION,
    dry_run_checks_keys=False,
)
