"""Frozen Phase 2 Calendar driver, test-only parity oracle. Generated from 9371e2e."""


from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import random
import uuid
from pathlib import Path

from a2a_engine import EventLog, EpisodeConfigBase, EpisodeTrace, register_environment
from a2a_engine.comm import CHANNELS, CommRouter, RoutingContext, Topology, canonical_channel
from a2a_engine.remote.seats import validate_runtime
from a2a_engine.adapters import adapters, resolve_bindings
from a2a_engine.manifest import redact_config
from calendar_game.resolve import resolve_config
from pydantic import Field, field_validator

from calendar_game.agents import Agent, BaseClient, GameConfig
from calendar_game.clients import (
    DSMClient,
    DSPyClient,
    IncrementalMAPClient,
    LLMClient,
    PaperDSMClient,
    PrivateDSMClient,
    SDClient,
    ScriptedClient,
)
from calendar_game.prompts import (
    append_phase_inbox,
    build_cheap_talk_prompt,
    build_decision_message,
    build_reflection_message,
    build_retry_message,
    build_system_prompt,
    build_voluntary_reschedule_message,
)
from calendar_game.privacy import hydrate_calendar_render_for_llm, hydrate_meeting_for_llm
from calendar_game.observability import InstrumentedCalendarClient
from calendar_game.ratings import CalendarRatingAdapter
from calendar_game.trace_contract import build_calendar_rating_context
from calendar_game.declaration import DECLARATION
from calendar_game.calendar import Calendar, apply_cell, validate_cell
from calendar_game.fallback import FallbackDepthExceeded, FallbackImpossible, find_fallback_slot
from calendar_game.scenario import generate_scenario
from calendar_game.solver import cost_by_agent_for_assignments, solve_greedy, solve_optimal


from calendar_game.game import CalendarGame, compute_headline_scores, _clip01


class LegacyCalendarGame(CalendarGame):
    def _build_agents(self, scenario):
        from ._legacy_remote_oracle import LegacyRemoteSeatClient
        agents = super()._build_agents(scenario)
        for seat, client in self._remote_clients.items():
            legacy = LegacyRemoteSeatClient(client.runtime, seat, client.router,
                turn_timeout_s=client.turn_timeout_s, private_prompts=client.private_prompts)
            agents[seat].client.delegate = legacy
            self._remote_clients[seat] = legacy
        return agents

    def _close_remote(self):
        if self.runtime_context is not None and self.runtime_context.episode.active:
            for client in self._remote_clients.values():
                client.episode_end()
        super()._close_remote()

    async def _run_reflection_measurement(
        self,
        *,
        agents: list[Agent],
        all_agent_ids: list[int],
        round_num: int | None,
        event_round: int,
        reporter_agent_ids: list[int] | None = None,
    ) -> tuple[list[dict], dict[int, int]]:
        """Run non-mutating reflection calls and append trace events.

        The client hook is expected to use record_history=False, so these calls
        are measurement-only and do not enter future agent context.
        """
        if not self.config.enable_reflection:
            return [], {}

        calls: list[tuple[int, int, str, asyncio.Task]] = []
        max_parallelism = self.config.reflection_max_parallelism
        semaphore = (
            asyncio.Semaphore(max(1, int(max_parallelism)))
            if max_parallelism is not None and int(max_parallelism) > 0
            else None
        )

        async def call_reflection(agent_id: int, target_agent_id: int):
            def run_sync():
                return agents[agent_id].client.reflect_calendar_belief(
                    target_agent_id,
                    self.config.num_slots,
                    round_num=round_num,
                )

            if semaphore is None:
                return await asyncio.to_thread(run_sync)
            async with semaphore:
                return await asyncio.to_thread(run_sync)

        reporter_ids = all_agent_ids if reporter_agent_ids is None else reporter_agent_ids

        for agent_id in reporter_ids:
            for target_agent_id in all_agent_ids:
                if target_agent_id == agent_id:
                    continue
                prompt_sent = build_reflection_message(
                    target_agent_id,
                    self.config.num_slots,
                    round_num=round_num,
                )
                self._append_event("reflection_start", data={
                    "round": event_round,
                    "turn": 0,
                    "phase": "REFLECTION",
                    "agent_id": agent_id,
                    "target_agent_id": target_agent_id,
                    "num_slots": self.config.num_slots,
                    "reflection_scope": "environment" if round_num is None else "round",
                    "prompt_sent": prompt_sent,
                })
                task = asyncio.create_task(call_reflection(agent_id, target_agent_id))
                calls.append((agent_id, target_agent_id, prompt_sent, task))

        results = await asyncio.gather(*(task for *_prefix, task in calls), return_exceptions=True)
        reflection_estimates: list[dict] = []
        client_call_counts: dict[int, int] = {}
        for (agent_id, target_agent_id, _prompt_sent, _task), result in zip(calls, results, strict=False):
            if isinstance(result, Exception):
                result = None
            if result is not None:
                client_call_counts[agent_id] = client_call_counts.get(agent_id, 0) + 1
                estimates = [
                    {
                        "agent_id": agent_id,
                        "round": event_round,
                        "reflection_scope": "environment" if round_num is None else "round",
                        **estimate,
                    }
                    for estimate in result.estimates
                ]
                reflection_estimates.extend(estimates)
                self._append_event("reflection_end", data={
                    "round": event_round,
                    "turn": 0,
                    "phase": "REFLECTION",
                    "agent_id": agent_id,
                    "target_agent_id": target_agent_id,
                    "num_slots": self.config.num_slots,
                    "reflection_scope": "environment" if round_num is None else "round",
                    "estimates": estimates,
                    "text": result.text,
                    "usage": result.usage.__dict__ if result.usage else None,
                    "latency_ms": result.latency_ms,
                    "raw_api_response": result.raw,
                })
            else:
                estimates = [
                    {
                        "agent_id": agent_id,
                        "target_agent_id": target_agent_id,
                        "round": event_round,
                        "reflection_scope": "environment" if round_num is None else "round",
                        "slot": slot,
                        "estimate_state": None,
                        "probability_free": None,
                        "probability_busy": None,
                        "confidence": None,
                        "logprobs": {"0": None, "1": None},
                    }
                    for slot in range(self.config.num_slots)
                ]
                self._append_event("reflection_end", data={
                    "round": event_round,
                    "turn": 0,
                    "phase": "REFLECTION",
                    "agent_id": agent_id,
                    "target_agent_id": target_agent_id,
                    "num_slots": self.config.num_slots,
                    "reflection_scope": "environment" if round_num is None else "round",
                    "estimates": estimates,
                    "text": None,
                    "usage": None,
                    "latency_ms": None,
                    "raw_api_response": None,
                })
        return reflection_estimates, client_call_counts

    async def _run_async(self, scenario: dict, agents: list[Agent] | None = None) -> EpisodeTrace:
        self._ensure_speaker_orders(scenario)
        optimal = (
            scenario.get("optimal")
            if scenario.get("optimal", {}).get("cost") is not None
            else solve_optimal(scenario["calendars"], scenario["meetings"], self.config.num_slots)
        )
        greedy = (
            scenario.get("greedy")
            if scenario.get("greedy", {}).get("cost") is not None
            else solve_greedy(scenario["calendars"], scenario["meetings"], self.config.num_slots)
        )

        # 2. Build agents (or use injected ones for testing)
        if agents is None:
            agents = self._build_agents(scenario)
        else:
            self._annotate_calendars(agents, scenario)
        nosy_agent_ids = self._nosy_agent_ids()
        blocked_slots_by_agent = self._blocked_slots_by_agent(scenario)
        communication_protocol = self._communication_protocol()
        team_metadata = self._agent_team_metadata()
        initial_calendar_densities = self._actual_calendar_densities(scenario["calendars"])
        representation_elo_by_agent = self._representation_elo_by_agent(initial_calendar_densities)

        # 3. game_start event — emitted before agent registration so it is always first
        self._append_event("game_start", data={
            "round": -1, "turn": -1, "phase": "GAME_START", "agent_id": None,
            "scenario_seed": self.config.seed,
            "num_agents": self.config.num_agents,
            "num_slots": self.config.num_slots,
            "optimal_cost": optimal.get("cost"),
            "greedy_cost": greedy.get("cost"),
            "nosy_agent_ids": nosy_agent_ids,
            "communication_protocol": communication_protocol,
            "difficulty": scenario.get("difficulty"),
            "difficulty_score": scenario.get("difficulty_score"),
            "difficulty_scorer": scenario.get("difficulty_scorer"),
            "setting_normalized_difficulty": scenario.get("setting_normalized_difficulty"),
            "config_normalized_difficulty": scenario.get("config_normalized_difficulty"),
            "agent_densities": scenario.get("agent_densities") or self.config.agent_densities,
            "initial_calendar_density_by_agent": initial_calendar_densities,
            "representation_elo_by_agent": representation_elo_by_agent,
            **team_metadata,
        })

        # 4. Register all agents
        def stopped():
            self._append_event("game_stopped", data={"reason": "seat_unavailable", "phase": "GAME_START"})
            return EpisodeTrace(episode_uid=str(uuid.uuid4()), config=self.config,
                                events=self.events.all(), stopped=True,
                                final_state={"calendars": [agent.calendar.slots for agent in agents],
                                             "rating_context": build_calendar_rating_context(scenario)}, metrics={})

        if self.runtime_context:
            try:
                for seat in self.runtime_context.episode.seats:
                    self.runtime_context.episode.wait_ready(seat)
            except TimeoutError:
                return stopped()
        all_agent_ids = list(range(self.config.num_agents))
        communication_policy_by_phase = (
            {phase: self.topology.effective(phase=phase, round=0).as_dict()
             for phase in ("CHEAP_TALK", "VOLUNTARY", "DECISION")}
            if not self.topology.legacy else {}
        )
        for agent_id, agent in enumerate(agents):
            dsm_prior_meetings = [
                {
                    "id": int(prior["id"]),
                    "participants": list(prior.get("participants", [])),
                    "slot": prior.get("slot"),
                }
                for prior in scenario.get("prior_meetings", [])
                if agent_id in prior.get("participants", [])
            ]
            game_config = GameConfig(
                num_agents=self.config.num_agents,
                num_slots=self.config.num_slots,
                agent_id=agent_id,
                all_agent_ids=all_agent_ids,
                dm_cap=self.config.dm_cap,
                decision_retries=self.config.decision_retries,
                dsm_num_proposals=self.config.dsm_num_proposals,
                dsm_cascade_depth=self.config.dsm_cascade_depth,
                dsm_displacement_targets=self.config.dsm_displacement_targets,
                dsm_exhaustive_search=self.config.dsm_exhaustive_search,
                dsm_stop_on_perfect=self.config.dsm_stop_on_perfect,
                dsm_prior_meetings=dsm_prior_meetings,
                dsm_lmin=self.config.dsm_lmin,
                dsm_lmax=self.config.dsm_lmax,
                dsm_beta=self.config.dsm_beta,
                dsm_theta=self.config.dsm_theta,
                dsm_social_welfare_weight=self.config.dsm_social_welfare_weight,
                dsm_privacy_unit_cost=self.config.dsm_privacy_unit_cost,
                dsm_initial_budget=self.config.dsm_initial_budget,
                sd_model={int(k): float(v) for k, v in self.config.sd_model.items()},
                communication_protocol=communication_protocol,
                communication_by_phase=(
                    {phase: self.topology.effective(phase=phase, round=0).protocol
                     for phase in ("CHEAP_TALK", "VOLUNTARY", "DECISION")}
                    if not self.topology.legacy else {}
                ),
                communication_policy_by_phase=communication_policy_by_phase,
            )
            try:
                agent.register(agent_id, game_config)
            except TimeoutError:
                if agent_id in self._remote_clients:
                    return stopped()
                raise
            system_prompt_text = getattr(agent.client, "_system_prompt", None) or build_system_prompt(
                dataclasses.asdict(game_config)
            )
            self._append_event("agent_registered", data={
                "round": -1, "turn": -1, "phase": "GAME_START", "agent_id": agent_id,
                "is_nosy_agent": agent_id in nosy_agent_ids,
                "nosy_agent_ids": nosy_agent_ids,
                "model": team_metadata["agent_models"][agent_id],
                "agent_type": team_metadata["agent_types"][agent_id],
                "initial_calendar_density": initial_calendar_densities[agent_id],
                "representation_elo": representation_elo_by_agent[agent_id],
                "system_prompt": system_prompt_text,
                "calendar_render": agent.calendar.render(),
            })

        # 5. Per-environment accumulators
        displacement_cost: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        fallback_displacement_cost: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        total_client_calls: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        total_dms_sent: int = 0
        total_participant_groupchat_messages_sent: int = 0
        total_all_groupchat_messages_sent: int = 0
        total_dm_chars: int = 0
        total_participant_groupchat_chars: int = 0
        total_all_groupchat_chars: int = 0
        max_dm_chars: int = 0
        max_participant_groupchat_chars: int = 0
        max_all_groupchat_chars: int = 0
        messages_sent_by_agent: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        messages_received_by_agent: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        dms_sent_by_agent: dict[int, int] = {i: 0 for i in range(self.config.num_agents)}
        participant_groupchat_messages_sent_by_agent: dict[int, int] = {
            i: 0 for i in range(self.config.num_agents)
        }
        all_groupchat_messages_sent_by_agent: dict[int, int] = {
            i: 0 for i in range(self.config.num_agents)
        }
        round_outcomes: list[dict] = []
        reflection_estimates: list[dict] = []
        reflection_frequency = str(self.config.reflection_frequency or "round").casefold()
        if reflection_frequency not in {"round", "environment"}:
            reflection_frequency = "round"
        registered_meetings: dict[int, dict] = {
            int(meeting["id"]): meeting
            for meeting in scenario["meetings"]
        }
        for prior in scenario.get("prior_meetings", []):
            registered_meetings[int(prior["id"])] = {
                "id": int(prior["id"]),
                "participants": list(prior.get("participants", [])),
                "duration": int(prior.get("duration", 1)),
                "cost": int(prior.get("cost", 1)),
            }
        meeting_registry: dict[int, int] = {
            int(prior["id"]): int(prior["slot"])
            for prior in scenario.get("prior_meetings", [])
            if prior.get("slot") is not None
        }  # meeting_id → canonical_slot

        def deliver_cheap_talk_tool(
            *,
            tool: dict,
            agent_id: int,
            meeting: dict,
            round_num: int,
            turn_index: int,
            already_queued: set[int],
            phase: str = "CHEAP_TALK",
        ) -> bool:
            nonlocal total_dms_sent
            nonlocal total_participant_groupchat_messages_sent
            nonlocal total_all_groupchat_messages_sent
            nonlocal total_dm_chars
            nonlocal total_participant_groupchat_chars
            nonlocal total_all_groupchat_chars
            nonlocal max_dm_chars
            nonlocal max_participant_groupchat_chars
            nonlocal max_all_groupchat_chars

            if self._remote_rejection(tool, agent_id=agent_id, round_num=round_num,
                                      turn=turn_index, phase=phase):
                return False
            tool_type = self._canonical_tool_type(tool.get("type"))
            decision = self.router.authorize(
                agent_id, tool_type, tool.get("to"), phase=phase, round=round_num,
                context=RoutingContext(tuple(all_agent_ids), meeting["id"], tuple(meeting["participants"])),
                attempted_this_round=round_messaging_tools_invoked_by_agent[agent_id],
            )
            if decision.charge_attempt:
                round_messaging_tools_invoked_by_agent[agent_id] += 1
            if not decision.allowed:
                if decision.reason is None:
                    return False
                self._invalid_tool_call(
                    round_num=round_num, turn=turn_index, phase=phase,
                    agent_id=agent_id, tool=tool,
                    reason=decision.reason,
                )
                return False
            if tool_type == "dm":
                to = decision.recipients[0]
                msg = {
                    "from": agent_id,
                    "to": to,
                    "channel": "dm",
                    "meeting_id": meeting["id"],
                    "content": str(tool.get("content", "")),
                }
                dm_chars = len(msg["content"])
                agents[to].inbox_queue.append(msg)
                messages_sent_by_agent[agent_id] += 1
                messages_received_by_agent[to] += 1
                dms_sent_by_agent[agent_id] += 1
                total_dms_sent += 1
                total_dm_chars += dm_chars
                max_dm_chars = max(max_dm_chars, dm_chars)
                self._append_event("dm_sent", data={
                    "round": round_num, "turn": turn_index, "phase": phase,
                    "agent_id": agent_id,
                    "from_agent": agent_id, "to_agent": to,
                    "meeting_id": msg["meeting_id"], "content": msg["content"],
                    "content_chars": dm_chars,
                    "channel": "dm",
                })
                if to not in meeting["participants"] and to not in already_queued:
                    already_queued.add(to)
                return True

            if tool_type in {"participant_groupchat", "all_groupchat"}:
                content = str(tool.get("content", ""))
                msg_chars = len(content)
                recipients = list(decision.recipients)
                event_type = f"{tool_type}_sent"
                for to in recipients:
                    agents[to].inbox_queue.append({
                        "from": agent_id,
                        "to": None,
                        "channel": tool_type,
                        "meeting_id": meeting["id"],
                        "content": content,
                    })
                    messages_received_by_agent[to] += 1
                    if tool_type == "all_groupchat" and to not in meeting["participants"]:
                        already_queued.add(to)
                messages_sent_by_agent[agent_id] += 1
                if tool_type == "participant_groupchat":
                    participant_groupchat_messages_sent_by_agent[agent_id] += 1
                    total_participant_groupchat_messages_sent += 1
                    total_participant_groupchat_chars += msg_chars
                    max_participant_groupchat_chars = max(max_participant_groupchat_chars, msg_chars)
                else:
                    all_groupchat_messages_sent_by_agent[agent_id] += 1
                    total_all_groupchat_messages_sent += 1
                    total_all_groupchat_chars += msg_chars
                    max_all_groupchat_chars = max(max_all_groupchat_chars, msg_chars)
                self._append_event(event_type, data={
                    "round": round_num, "turn": turn_index, "phase": phase,
                    "agent_id": agent_id,
                    "from_agent": agent_id,
                    "to_agents": recipients,
                    "meeting_id": meeting["id"],
                    "content": content,
                    "content_chars": msg_chars,
                    "channel": tool_type,
                })
                return True

            return False

        effective_policy = None

        def enter_phase(phase: str, round_num: int) -> str:
            nonlocal effective_policy
            policy = self.topology.effective(phase=phase, round=round_num)
            if policy != effective_policy:
                self._append_event("topology_changed", data={
                    "phase": phase, "round": round_num, **policy.as_dict(),
                })
                effective_policy = policy
            return policy.protocol

        def separate_messages(tool_calls: list, *, agent_id: int, phase: str) -> list:
            tool_calls = self._worker_attempts(agent_id, tool_calls)
            if self.topology.legacy and agent_id not in self._remote_clients:
                return tool_calls
            actions = []
            for tool in tool_calls:
                if self._remote_rejection(tool, agent_id=agent_id, round_num=round_num,
                                          turn=turn_index, phase=phase):
                    continue
                if isinstance(tool, dict) and self._canonical_tool_type(tool.get("type")) in CHANNELS:
                    deliver_cheap_talk_tool(
                        tool=tool, agent_id=agent_id, meeting=meeting,
                        round_num=round_num, turn_index=turn_index,
                        already_queued=already_queued, phase=phase,
                    )
                else:
                    actions.append(tool)
            return actions

        def decision_inbox(agent: Agent) -> dict:
            if self.topology.legacy:
                return {}
            messages = list(agent.inbox_queue)
            agent.inbox_queue.clear()
            agent.client.observe_messages(messages)
            return {"inbox_drained": messages}

        def retry_decision(agent_id, parent_phase, attempt, conflict):
            agent = agents[agent_id]
            inbox_data = decision_inbox(agent)
            remote = self._remote_clients.get(agent_id)
            data = {
                "round": round_num, "turn": turn_index, "phase": "DECISION_RETRY",
                "parent_phase": parent_phase, "attempt": attempt,
                "max_attempts": self.config.decision_retries, "conflict": conflict,
                "agent_id": agent_id,
                "calendar_snapshot_render": self._prompt_calendar_for_agent(agent, agent.calendar.snapshot().render(), round_num),
                "prompt_sent": append_phase_inbox(build_retry_message(attempt, self.config.decision_retries, conflict,
                                                                        parent_phase=parent_phase),
                                                   inbox_data.get("inbox_drained", [])),
                **inbox_data,
            }
            if remote:
                self._append_event("decide_start", data=data)
            result = agent.client.retry_decide(attempt, self.config.decision_retries, conflict)
            if remote:
                self._append_event("decide_end", data={
                    "round": round_num, "turn": turn_index, "phase": "DECISION_RETRY",
                    "parent_phase": parent_phase, "attempt": attempt, "agent_id": agent_id,
                    "tool_calls": result.tool_calls, "text": result.text, "thinking": result.thinking,
                    "usage": result.usage.__dict__ if result.usage else None,
                    "latency_ms": result.latency_ms, "raw_api_response": result.raw,
                    "retry_count": result.retry_count, "status": "pending",
                })
            return result

        # 6. Main loop — one round per meeting
        for round_num, meeting in enumerate(scenario["meetings"]):
            for agent_id, agent in enumerate(agents):
                if agent.inbox_queue:
                    dropped_messages = list(agent.inbox_queue)
                    agent.inbox_queue.clear()
                    self._append_event("inbox_cleared", data={
                        "round": round_num, "turn": 0, "phase": "ROUND_START",
                        "agent_id": agent_id,
                        "reason": "discard_unread_messages_from_previous_round",
                        "messages": dropped_messages,
                    })

            speaker_order = self._speaker_order(meeting)
            communication_protocol = enter_phase("CHEAP_TALK", round_num)
            self._append_event("round_start", data={
                "round": round_num, "turn": 0, "phase": "CHEAP_TALK", "agent_id": None,
                "meeting": meeting,
                "speaker_order": speaker_order,
                "communication_protocol": communication_protocol,
            })

            active_agent_ids: set[int] = set(speaker_order)
            active_agent_order = list(speaker_order)

            # call start_round for all active cheap-talk agents
            for agent_id in active_agent_order:
                agents[agent_id].start_round(meeting, round_num, incurred_penalty=displacement_cost[agent_id])

            # per-round state
            already_queued: set[int] = set()
            round_turn_agent_ids: set[int] = set()
            round_messaging_tools_invoked_by_agent: dict[int, int] = {
                i: 0 for i in range(self.config.num_agents)
            }
            self._remote_budget_usage = round_messaging_tools_invoked_by_agent
            turn_index = 0
            blocked_slot_violations: list[dict] = []

            # --- CHEAP_TALK PHASE ---
            has_activity = True
            while has_activity and turn_index < self.config.max_turns_per_round:
                has_activity = False

                # active cheap-talk agents act
                for agent_id in list(active_agent_order):
                    agent = agents[agent_id]
                    inbox_snapshot = list(agents[agent_id].inbox_queue)
                    calendar_render = agents[agent_id].calendar.render()
                    prompt_calendar_render = self._prompt_calendar_for_agent(agent, calendar_render, round_num)
                    prompt_meeting = self._prompt_meeting_for_agent(agent, meeting, round_num)
                    turn_prompt = build_cheap_talk_prompt(
                        prompt_meeting, prompt_calendar_render, round_num, inbox_snapshot,
                        first_turn=agent_id not in round_turn_agent_ids,
                        incurred_penalty=displacement_cost[agent_id], turn_index=turn_index,
                        max_turns_per_round=self.config.max_turns_per_round,
                        communication_protocol=communication_protocol,
                        communication_policy=communication_policy_by_phase.get("CHEAP_TALK"),
                    )
                    self._append_event("turn_start", data={
                        "round": round_num, "turn": turn_index, "phase": "CHEAP_TALK",
                        "agent_id": agent_id,
                        "inbox_drained": inbox_snapshot,
                        "calendar_render": prompt_calendar_render,
                        "prompt_sent": turn_prompt,
                    })
                    result = agents[agent_id].turn(turn_index, self.config.max_turns_per_round)
                    round_turn_agent_ids.add(agent_id)
                    total_client_calls[agent_id] += 1
                    self._append_event("turn_end", data={
                        "round": round_num, "turn": turn_index, "phase": "CHEAP_TALK",
                        "agent_id": agent_id,
                        "tool_calls": result.tool_calls,
                        "text": result.text,
                        "thinking": result.thinking,
                        "usage": result.usage.__dict__ if result.usage else None,
                        "latency_ms": result.latency_ms,
                        "raw_api_response": result.raw,
                    })

                    for tool in self._worker_attempts(agent_id, result.tool_calls):
                        if not isinstance(tool, dict):
                            self._invalid_tool_call(
                                round_num=round_num, turn=turn_index, phase="CHEAP_TALK",
                                agent_id=agent_id, tool=tool, reason="tool call is not an object",
                            )
                            continue
                        if deliver_cheap_talk_tool(
                            tool=tool,
                            agent_id=agent_id,
                            meeting=meeting,
                            round_num=round_num,
                            turn_index=turn_index,
                            already_queued=already_queued,
                        ):
                            has_activity = True

                # drain unique_queue (non-participants who got DMs or all-agent groupchat)
                queue = sorted(already_queued - active_agent_ids)
                for agent_id in queue:
                    active_agent_ids.add(agent_id)
                    active_agent_order.append(agent_id)
                    agents[agent_id].start_round(
                        meeting,
                        round_num,
                        incurred_penalty=displacement_cost[agent_id],
                    )
                    agent = agents[agent_id]
                    inbox_snapshot = list(agents[agent_id].inbox_queue)
                    calendar_render = agents[agent_id].calendar.render()
                    prompt_calendar_render = self._prompt_calendar_for_agent(agent, calendar_render, round_num)
                    turn_prompt = build_cheap_talk_prompt(
                        self._prompt_meeting_for_agent(agent, meeting, round_num),
                        prompt_calendar_render, round_num, inbox_snapshot, first_turn=True,
                        incurred_penalty=displacement_cost[agent_id], turn_index=turn_index,
                        max_turns_per_round=self.config.max_turns_per_round,
                        communication_protocol=communication_protocol,
                        communication_policy=communication_policy_by_phase.get("CHEAP_TALK"),
                    )
                    self._append_event("turn_start", data={
                        "round": round_num, "turn": turn_index, "phase": "CHEAP_TALK",
                        "agent_id": agent_id,
                        "inbox_drained": inbox_snapshot,
                        "calendar_render": prompt_calendar_render,
                        "prompt_sent": turn_prompt,
                    })
                    result = agents[agent_id].turn(turn_index, self.config.max_turns_per_round)
                    round_turn_agent_ids.add(agent_id)
                    total_client_calls[agent_id] += 1
                    self._append_event("turn_end", data={
                        "round": round_num, "turn": turn_index, "phase": "CHEAP_TALK",
                        "agent_id": agent_id,
                        "tool_calls": result.tool_calls,
                        "text": result.text, "thinking": result.thinking,
                        "usage": result.usage.__dict__ if result.usage else None,
                        "latency_ms": result.latency_ms, "raw_api_response": result.raw,
                    })
                    for tool in self._worker_attempts(agent_id, result.tool_calls):
                        if not isinstance(tool, dict):
                            self._invalid_tool_call(
                                round_num=round_num, turn=turn_index, phase="CHEAP_TALK",
                                agent_id=agent_id, tool=tool, reason="tool call is not an object",
                            )
                            continue
                        if deliver_cheap_talk_tool(
                            tool=tool,
                            agent_id=agent_id,
                            meeting=meeting,
                            round_num=round_num,
                            turn_index=turn_index,
                            already_queued=already_queued,
                        ):
                            has_activity = True

                turn_index += 1

            # --- VOLUNTARY RESCHEDULE PHASE (non-participants who received DMs) ---
            voluntary_protocol = enter_phase("VOLUNTARY", round_num)
            for agent_id in sorted(already_queued - set(meeting["participants"])):
                agent = agents[agent_id]
                inbox_data = decision_inbox(agent)
                calendar_render = agents[agent_id].calendar.render()
                prompt_calendar_render = self._prompt_calendar_for_agent(agent, calendar_render, round_num)
                prompt_meeting = self._prompt_meeting_for_agent(agent, meeting, round_num)
                self._append_event("decide_start", data={
                    "round": round_num, "turn": turn_index, "phase": "VOLUNTARY",
                    "agent_id": agent_id,
                    "calendar_render": prompt_calendar_render,
                    "prompt_sent": append_phase_inbox(
                        build_voluntary_reschedule_message(
                            prompt_meeting, prompt_calendar_render, voluntary_protocol,
                            communication_policy=communication_policy_by_phase.get("VOLUNTARY"),
                        ),
                        inbox_data.get("inbox_drained", []),
                    ),
                    **inbox_data,
                })
                result = agents[agent_id].voluntary_decide(meeting)
                total_client_calls[agent_id] += 1
                self._append_event("decide_end", data={
                    "round": round_num, "turn": turn_index, "phase": "VOLUNTARY",
                    "agent_id": agent_id,
                    "tool_calls": result.tool_calls, "text": result.text,
                    "thinking": result.thinking,
                    "usage": result.usage.__dict__ if result.usage else None,
                    "latency_ms": result.latency_ms, "raw_api_response": result.raw,
                })
                actions = [
                    a for a in separate_messages(result.tool_calls, agent_id=agent_id, phase="VOLUNTARY")
                    if isinstance(a, dict) and a.get("type") == "reschedule"
                ]
                for attempt in range(self.config.decision_retries + 1):
                    blocked_slot_violations.extend(self._blocked_reschedule_violations(
                        agents[agent_id].calendar,
                        actions,
                        agent_id=agent_id,
                        phase="VOLUNTARY",
                        attempt=attempt,
                    ))
                    ok, conflict = validate_cell(agents[agent_id].calendar, actions, require_schedule=False)
                    if ok:
                        for action in actions:
                            from_slot = int(action["from_slot"])
                            item = agents[agent_id].calendar.get(from_slot)
                            if isinstance(item, dict) and "cost" in item:
                                displacement_cost[agent_id] += int(item["cost"])
                        apply_cell(agents[agent_id].calendar, actions)
                        self._append_event("cell_applied", data={
                            "round": round_num, "turn": turn_index, "phase": "VOLUNTARY",
                            "agent_id": agent_id,
                            "actions": actions,
                            "calendar_render_after": agents[agent_id].calendar.render(),
                        })
                        break
                    else:
                        self._append_event("cell_rejected", data={
                            "round": round_num, "turn": turn_index, "phase": "VOLUNTARY",
                            "agent_id": agent_id,
                            "attempt": attempt, "conflict_description": conflict, "actions": actions,
                        })
                        if attempt < self.config.decision_retries:
                            retry_result = retry_decision(agent_id, "VOLUNTARY", attempt + 1, conflict)
                            total_client_calls[agent_id] += 1
                            actions = [
                                a for a in separate_messages(retry_result.tool_calls, agent_id=agent_id, phase="VOLUNTARY")
                                if isinstance(a, dict) and a.get("type") == "reschedule"
                            ]
                        else:
                            break

            # --- DECISION PHASE ---
            decision_protocol = enter_phase("DECISION", round_num)
            staged_decisions: dict[int, tuple[Calendar, list[dict], int]] = {}
            decision_phase_failed = False
            for agent_id in speaker_order:
                agent = agents[agent_id]
                inbox_data = decision_inbox(agent)
                snapshot_render = agents[agent_id].calendar.snapshot().render()
                prompt_snapshot_render = self._prompt_calendar_for_agent(agent, snapshot_render, round_num)
                prompt_meeting = self._prompt_meeting_for_agent(agent, meeting, round_num)
                decision_prompt = append_phase_inbox(
                    build_decision_message(
                        prompt_meeting, prompt_snapshot_render, decision_protocol,
                        communication_policy=communication_policy_by_phase.get("DECISION"),
                    ),
                    inbox_data.get("inbox_drained", []),
                )
                self._append_event("decide_start", data={
                    "round": round_num, "turn": turn_index, "phase": "DECISION",
                    "agent_id": agent_id,
                    "calendar_snapshot_render": prompt_snapshot_render,
                    "prompt_sent": decision_prompt,
                    **inbox_data,
                })
                result = agents[agent_id].decide(meeting)
                total_client_calls[agent_id] += 1
                self._append_event("decide_end", data={
                    "round": round_num, "turn": turn_index, "phase": "DECISION",
                    "agent_id": agent_id,
                    "tool_calls": result.tool_calls, "text": result.text,
                    "thinking": result.thinking,
                    "usage": result.usage.__dict__ if result.usage else None,
                    "latency_ms": result.latency_ms, "raw_api_response": result.raw,
                    "retry_count": result.retry_count, "status": "pending",
                })

                # validate and apply cell with retries
                # Inject meeting cost into schedule actions so apply_cell can store it
                actions = self._decision_actions(
                    separate_messages(result.tool_calls, agent_id=agent_id, phase="DECISION"), meeting,
                    round_num=round_num, turn_index=turn_index,
                    agent_id=agent_id, attempt=0,
                )
                for attempt in range(self.config.decision_retries + 1):
                    blocked_slot_violations.extend(self._blocked_reschedule_violations(
                        agents[agent_id].calendar,
                        actions,
                        agent_id=agent_id,
                        phase="DECISION",
                        attempt=attempt,
                    ))
                    ok, conflict = validate_cell(agents[agent_id].calendar, actions)
                    if ok:
                        staged_calendar = agents[agent_id].calendar.snapshot()
                        pending_displacement_cost = 0
                        for action in actions:
                            if action.get("type") == "reschedule":
                                from_slot = int(action["from_slot"])
                                item = agents[agent_id].calendar.get(from_slot)
                                if isinstance(item, dict) and "cost" in item:
                                    pending_displacement_cost += int(item["cost"])
                        apply_cell(staged_calendar, actions)
                        staged_decisions[agent_id] = (
                            staged_calendar,
                            actions,
                            pending_displacement_cost,
                        )
                        break
                    else:
                        self._append_event("cell_rejected", data={
                            "round": round_num, "turn": turn_index, "phase": "DECISION",
                            "agent_id": agent_id,
                            "attempt": attempt, "conflict_description": conflict, "actions": actions,
                        })
                        if attempt < self.config.decision_retries:
                            retry_result = retry_decision(agent_id, "DECISION", attempt + 1, conflict)
                            total_client_calls[agent_id] += 1
                            actions = self._decision_actions(
                                separate_messages(retry_result.tool_calls, agent_id=agent_id, phase="DECISION"), meeting,
                                round_num=round_num, turn_index=turn_index,
                                agent_id=agent_id, attempt=attempt + 1,
                            )
                        else:
                            self._append_event("decision_failed", data={
                                "round": round_num, "turn": turn_index, "phase": "DECISION",
                                "agent_id": agent_id,
                                "attempts_exhausted": self.config.decision_retries + 1,
                            })
                            decision_phase_failed = True
                            break

            if (
                not decision_phase_failed
                and not blocked_slot_violations
                and len(staged_decisions) == len(speaker_order)
            ):
                for agent_id in speaker_order:
                    staged_calendar, actions, pending_displacement_cost = staged_decisions[agent_id]
                    agents[agent_id].calendar.slots = staged_calendar.slots
                    agents[agent_id].calendar.meeting_participants = staged_calendar.meeting_participants
                    displacement_cost[agent_id] += pending_displacement_cost
                    self._append_event("cell_applied", data={
                        "round": round_num, "turn": turn_index, "phase": "DECISION",
                        "agent_id": agent_id,
                        "actions": actions,
                        "calendar_render_after": agents[agent_id].calendar.render(),
                    })
            elif staged_decisions:
                rollback_reason = (
                    "blocked slot violation"
                    if blocked_slot_violations
                    else "another participant failed decision validation"
                )
                for agent_id, (_staged_calendar, actions, _pending_cost) in staged_decisions.items():
                    self._append_event("cell_rolled_back", data={
                        "round": round_num, "turn": turn_index, "phase": "DECISION",
                        "agent_id": agent_id,
                        "actions": actions,
                        "reason": rollback_reason,
                        "calendar_render_after": agents[agent_id].calendar.render(),
                    })

            # --- RESOLUTION PHASE ---
            enter_phase("RESOLUTION", round_num)
            per_agent_slot: dict[str, int | None] = {}
            for agent_id in meeting["participants"]:
                found_slot = None
                for slot_idx, slot_val in enumerate(agents[agent_id].calendar.slots):
                    if isinstance(slot_val, dict) and slot_val.get("meeting_id") == meeting["id"]:
                        found_slot = slot_idx
                        break
                per_agent_slot[str(agent_id)] = found_slot

            slots_chosen = [s for s in per_agent_slot.values() if s is not None]
            coordinated = len(slots_chosen) == len(meeting["participants"]) and len(set(slots_chosen)) == 1

            # slot conflicts: any agent with >1 meeting in same slot
            slot_conflicts: dict[str, list[int]] = {}
            for agent_id in range(self.config.num_agents):
                seen: dict[int, list] = {}
                for slot_idx, slot_val in enumerate(agents[agent_id].calendar.slots):
                    if isinstance(slot_val, dict) and "meeting_id" in slot_val:
                        seen.setdefault(slot_idx, []).append(f"M{slot_val['meeting_id']}")
                conflicts = [slot_idx for slot_idx, vals in seen.items() if len(vals) > 1]
                if conflicts:
                    slot_conflicts[str(agent_id)] = conflicts

            for agent_id, blocked_slots in blocked_slots_by_agent.items():
                for slot_idx in blocked_slots:
                    slot_val = agents[agent_id].calendar.get(slot_idx)
                    if isinstance(slot_val, dict) and "meeting_id" in slot_val:
                        blocked_slot_violations.append({
                            "agent_id": agent_id,
                            "phase": "RESOLUTION",
                            "kind": "meeting_on_blocked_slot",
                            "slot": slot_idx,
                            "meeting_id": slot_val.get("meeting_id"),
                        })

            # constraint B: all participants of each registered meeting must agree on slot
            consistency_violated_ids: set[int] = set()
            for reg_mid, canonical_slot in meeting_registry.items():
                reg_meeting = registered_meetings.get(reg_mid)
                if reg_meeting is None:
                    continue
                agent_reg_slots: dict[int, int | None] = {}
                for pid in reg_meeting["participants"]:
                    found = None
                    for s_idx, s_val in enumerate(agents[pid].calendar.slots):
                        if isinstance(s_val, dict) and s_val.get("meeting_id") == reg_mid:
                            found = s_idx
                            break
                    agent_reg_slots[pid] = found
                slot_set = set(agent_reg_slots.values())
                if len(slot_set) > 1 or None in slot_set:
                    consistency_violated_ids.add(reg_mid)
                    for pid, s in agent_reg_slots.items():
                        self._append_event("consistency_violation", data={
                            "round": round_num, "turn": turn_index, "phase": "RESOLUTION",
                            "agent_id": pid,
                            "meeting_id": reg_mid,
                            "canonical_slot": canonical_slot,
                            "agent_slot": s,
                        })

            if consistency_violated_ids:
                coordinated = False
            if blocked_slot_violations:
                coordinated = False

            # update registry: consistent moves update canonical slot
            for reg_mid in list(meeting_registry):
                if reg_mid in consistency_violated_ids:
                    continue
                reg_meeting = registered_meetings.get(reg_mid)
                if reg_meeting is None:
                    continue
                new_slots: set[int] = set()
                for pid in reg_meeting["participants"]:
                    for s_idx, s_val in enumerate(agents[pid].calendar.slots):
                        if isinstance(s_val, dict) and s_val.get("meeting_id") == reg_mid:
                            new_slots.add(s_idx)
                            break
                if len(new_slots) == 1:
                    meeting_registry[reg_mid] = next(iter(new_slots))

            if coordinated:
                meeting_registry[meeting["id"]] = slots_chosen[0]

            self._append_event("resolution", data={
                "round": round_num, "turn": turn_index, "phase": "RESOLUTION",
                "agent_id": None,
                "meeting_id": meeting["id"],
                "per_agent_slot": per_agent_slot,
                "coordinated": coordinated,
                "slot_conflicts": slot_conflicts,
                "consistency_violated_meeting_ids": sorted(consistency_violated_ids),
                "blocked_slot_violations": blocked_slot_violations,
            })

            # --- FALLBACK PHASE ---
            if not coordinated and not blocked_slot_violations and self.config.enable_fallback and not self.dry_run:
                enter_phase("FALLBACK", round_num)
                self._append_event("fallback_start", data={
                    "round": round_num, "turn": turn_index, "phase": "FALLBACK",
                    "agent_id": None,
                    "meeting_id": meeting["id"],
                    "participants": meeting["participants"],
                })
                try:
                    chosen_slot, disp_plan = find_fallback_slot(
                        agents, meeting, self.config.num_slots,
                        meeting_registry, list(registered_meetings.values()),
                        max_depth=self.config.fallback_max_depth,
                    )
                    # Apply displacement actions grouped by agent
                    registry_cascades: list[int] = []
                    for action in disp_plan:
                        aid = action["agent_id"]
                        fs, ts = action["from_slot"], action["to_slot"]
                        item = agents[aid].calendar.get(fs)
                        if isinstance(item, dict) and "cost" in item:
                            fallback_displacement_cost[aid] += int(item["cost"])
                        agents[aid].calendar.slots[ts] = agents[aid].calendar.slots[fs]
                        agents[aid].calendar.slots[fs] = None
                        if action.get("is_meeting_cascade"):
                            mid_c = action["item_id"]
                            if mid_c not in registry_cascades:
                                registry_cascades.append(mid_c)
                    # Clear any stale placement of this meeting from the failed round
                    for pid in meeting["participants"]:
                        for s_idx, s_val in enumerate(agents[pid].calendar.slots):
                            if isinstance(s_val, dict) and s_val.get("meeting_id") == meeting["id"]:
                                agents[pid].calendar.slots[s_idx] = None
                    # Place meeting on all participants' calendars at chosen_slot
                    for pid in meeting["participants"]:
                        agents[pid].calendar.slots[chosen_slot] = {
                            "meeting_id": meeting["id"],
                            "cost": meeting.get("cost", 1),
                        }
                    # Update registry for cascaded meetings and the new meeting
                    for mid_c in registry_cascades:
                        reg_m = registered_meetings.get(mid_c)
                        if reg_m:
                            cascade_slots: set[int] = set()
                            for pid in reg_m["participants"]:
                                for s_idx, s_val in enumerate(agents[pid].calendar.slots):
                                    if isinstance(s_val, dict) and s_val.get("meeting_id") == mid_c:
                                        cascade_slots.add(s_idx)
                                        break
                            if len(cascade_slots) == 1:
                                meeting_registry[mid_c] = next(iter(cascade_slots))
                    meeting_registry[meeting["id"]] = chosen_slot
                    coordinated = True
                    per_agent_slot = {str(pid): chosen_slot for pid in meeting["participants"]}
                    self._append_event("fallback_applied", data={
                        "round": round_num, "turn": turn_index, "phase": "FALLBACK",
                        "agent_id": None,
                        "meeting_id": meeting["id"],
                        "chosen_slot": chosen_slot,
                        "displacement_plan": disp_plan,
                        "fallback_displacement_cost": sum(fallback_displacement_cost.values()),
                        "registry_cascades": registry_cascades,
                        "calendar_renders_after": {
                            str(pid): agents[pid].calendar.render()
                            for pid in meeting["participants"]
                        },
                    })
                except FallbackDepthExceeded as exc:
                    self._append_event("fallback_error", data={
                        "round": round_num, "turn": turn_index, "phase": "FALLBACK",
                        "agent_id": None,
                        "meeting_id": meeting["id"],
                        "reason": "depth_exceeded",
                        "depth": exc.depth,
                        "detail": str(exc),
                    })
                except FallbackImpossible as exc:
                    self._append_event("fallback_error", data={
                        "round": round_num, "turn": turn_index, "phase": "FALLBACK",
                        "agent_id": None,
                        "meeting_id": meeting["id"],
                        "reason": "no_feasible_slot",
                        "detail": str(exc),
                    })

            round_outcomes.append({
                "meeting_id": meeting["id"],
                "coordinated": coordinated,
                "per_agent_slot": per_agent_slot,
                "slot_conflicts": slot_conflicts,
                "consistency_violated_meeting_ids": sorted(consistency_violated_ids),
                "blocked_slot_violations": blocked_slot_violations,
            })

            if reflection_frequency == "round":
                if self.config.enable_reflection:
                    enter_phase("REFLECTION", round_num)
                round_reflections, reflection_call_counts = await self._run_reflection_measurement(
                    agents=agents,
                    all_agent_ids=all_agent_ids,
                    round_num=round_num,
                    event_round=round_num,
                    reporter_agent_ids=sorted(round_turn_agent_ids),
                )
                reflection_estimates.extend(round_reflections)
                for agent_id, count in reflection_call_counts.items():
                    total_client_calls[agent_id] += count

        if reflection_frequency == "environment":
            if self.config.enable_reflection:
                enter_phase("REFLECTION", len(scenario["meetings"]))
            game_reflections, reflection_call_counts = await self._run_reflection_measurement(
                agents=agents,
                all_agent_ids=all_agent_ids,
                round_num=None,
                event_round=len(scenario["meetings"]),
            )
            reflection_estimates.extend(game_reflections)
            for agent_id, count in reflection_call_counts.items():
                total_client_calls[agent_id] += count

        # 7. Compute final metrics
        total_meetings = len(scenario["meetings"])
        coordinated_meetings = sum(1 for o in round_outcomes if o["coordinated"])
        coordination_rate = coordinated_meetings / total_meetings if total_meetings > 0 else 1.0

        agents_with_conflicts = sum(
            1 for agent_id in range(self.config.num_agents)
            if any(str(agent_id) in o["slot_conflicts"] for o in round_outcomes)
        )
        slot_conflict_rate = agents_with_conflicts / self.config.num_agents if self.config.num_agents > 0 else 0.0

        realized_cost = sum(displacement_cost.values())
        total_fallback_cost = sum(fallback_displacement_cost.values())
        optimal_cost = optimal.get("cost") or 0
        scheduled_meeting_ids = [
            int(outcome["meeting_id"])
            for outcome in round_outcomes
            if outcome["coordinated"]
        ]
        scheduled_meeting_id_set = set(scheduled_meeting_ids)
        unscheduled_meeting_ids = [
            int(meeting["id"])
            for meeting in scenario["meetings"]
            if int(meeting["id"]) not in scheduled_meeting_id_set
        ]
        scheduled_meetings = [
            meeting
            for meeting in scenario["meetings"]
            if int(meeting["id"]) in scheduled_meeting_id_set
        ]
        if scheduled_meetings:
            scheduled_only_optimal = solve_optimal(
                scenario["calendars"],
                scheduled_meetings,
                self.config.num_slots,
            )
            scheduled_only_greedy = solve_greedy(
                scenario["calendars"],
                scheduled_meetings,
                self.config.num_slots,
            )
        else:
            scheduled_only_optimal = {"cost": 0, "assignments": {}}
            scheduled_only_greedy = {"cost": 0, "assignments": {}}
        scheduled_only_optimal_cost = scheduled_only_optimal.get("cost")
        if scheduled_only_optimal_cost is None:
            scheduled_only_optimal_cost = 0
        scheduled_only_greedy_cost = scheduled_only_greedy.get("cost")
        replayed_oracle_cost, oracle_per_agent_cost = cost_by_agent_for_assignments(
            scenario["calendars"],
            scenario["meetings"],
            optimal.get("assignments", {}),
        )
        if replayed_oracle_cost is None:
            oracle_per_agent_cost = [0 for _ in range(self.config.num_agents)]
        scheduled_only_replayed_oracle_cost, scheduled_only_oracle_per_agent_cost = cost_by_agent_for_assignments(
            scenario["calendars"],
            scheduled_meetings,
            scheduled_only_optimal.get("assignments", {}),
        )
        if scheduled_only_replayed_oracle_cost is None:
            scheduled_only_oracle_per_agent_cost = [0 for _ in range(self.config.num_agents)]
        per_agent_cost_list = [displacement_cost[i] for i in range(self.config.num_agents)]
        per_agent_fallback_cost_list = [fallback_displacement_cost[i] for i in range(self.config.num_agents)]
        per_agent_excess_burden = [
            per_agent_cost_list[i] - oracle_per_agent_cost[i]
            for i in range(self.config.num_agents)
        ]
        per_agent_scheduled_only_excess_burden = [
            per_agent_cost_list[i] - scheduled_only_oracle_per_agent_cost[i]
            for i in range(self.config.num_agents)
        ]

        max_cost = max(per_agent_cost_list) if per_agent_cost_list else 0
        fairness = min(per_agent_cost_list) / max_cost if max_cost > 0 else 1.0
        participant_rounds = {i: 0 for i in range(self.config.num_agents)}
        coordinated_participant_rounds = {i: 0 for i in range(self.config.num_agents)}
        for outcome, meeting in zip(round_outcomes, scenario["meetings"], strict=False):
            for agent_id in meeting["participants"]:
                participant_rounds[agent_id] += 1
                if outcome["coordinated"]:
                    coordinated_participant_rounds[agent_id] += 1

        total_groupchat_messages_sent = (
            total_participant_groupchat_messages_sent
            + total_all_groupchat_messages_sent
        )
        total_cheap_talk_messages = total_dms_sent + total_groupchat_messages_sent
        total_groupchat_chars = total_participant_groupchat_chars + total_all_groupchat_chars
        total_cheap_talk_chars = total_dm_chars + total_groupchat_chars
        max_groupchat_chars = max(max_participant_groupchat_chars, max_all_groupchat_chars)
        # efficiency: avg cheap-talk messages sent per coordinated meeting (lower = more efficient)
        efficiency = (
            total_cheap_talk_messages / coordinated_meetings
            if coordinated_meetings > 0
            else float("inf")
        )
        avg_dm_chars = total_dm_chars / total_dms_sent if total_dms_sent > 0 else 0.0
        avg_cheap_talk_chars = (
            total_cheap_talk_chars / total_cheap_talk_messages
            if total_cheap_talk_messages > 0
            else 0.0
        )
        dm_chars_per_meeting = total_dm_chars / coordinated_meetings if coordinated_meetings > 0 else float("inf")
        cheap_talk_chars_per_meeting = (
            total_cheap_talk_chars / coordinated_meetings
            if coordinated_meetings > 0
            else float("inf")
        )
        per_agent_message_fraction = [
            (
                messages_sent_by_agent[i] / max(total_cheap_talk_messages, 1)
                if total_cheap_talk_messages > 0
                else 0.0
            )
            for i in range(self.config.num_agents)
        ]
        per_agent_communication_score = [
            _clip01(1.0 - value)
            for value in per_agent_message_fraction
        ]
        scheduled_only_excess_values = [
            max(0.0, float(value))
            for value in per_agent_scheduled_only_excess_burden
        ]
        per_agent_excess_normalizer = max(sum(scheduled_only_excess_values), 1.0)
        per_agent_normalized_excess_cost = [
            _clip01(value / per_agent_excess_normalizer)
            for value in scheduled_only_excess_values
        ]
        per_agent_excess_cost_score = [
            _clip01(1.0 - value)
            for value in per_agent_normalized_excess_cost
        ]
        max_representation_elo = max(representation_elo_by_agent) if representation_elo_by_agent else 1.0
        contribution_scores: list[dict] = []
        for agent_id in range(self.config.num_agents):
            participation_count = participant_rounds[agent_id]
            coordination_rate_for_agent = (
                coordinated_participant_rounds[agent_id] / participation_count
                if participation_count > 0
                else 0.0
            )
            communication_share = (
                messages_sent_by_agent[agent_id] / total_cheap_talk_messages
                if total_cheap_talk_messages > 0
                else 0.0
            )
            cost_efficiency = (
                1.0 - (per_agent_cost_list[agent_id] / max_cost)
                if max_cost > 0
                else 1.0
            )
            raw_score = (
                100.0
                * (
                    0.60 * coordination_rate_for_agent
                    + 0.25 * cost_efficiency
                    + 0.15 * communication_share
                )
            )
            representation_weight = (
                representation_elo_by_agent[agent_id] / max_representation_elo
                if max_representation_elo > 0
                else 1.0
            )
            model = team_metadata["agent_models"][agent_id]
            contribution_scores.append({
                "agent_id": agent_id,
                "model": model,
                "agent_type": team_metadata["agent_types"][agent_id],
                "participant_rounds": participation_count,
                "coordinated_participant_rounds": coordinated_participant_rounds[agent_id],
                "coordination_rate": round(coordination_rate_for_agent, 6),
                "messages_sent": messages_sent_by_agent[agent_id],
                "messages_received": messages_received_by_agent[agent_id],
                "communication_share": round(communication_share, 6),
                "cost": per_agent_cost_list[agent_id],
                "oracle_cost": oracle_per_agent_cost[agent_id],
                "excess_burden": per_agent_excess_burden[agent_id],
                "scheduled_only_oracle_cost": scheduled_only_oracle_per_agent_cost[agent_id],
                "scheduled_only_excess_burden": per_agent_scheduled_only_excess_burden[agent_id],
                "normalized_excess_cost": per_agent_normalized_excess_cost[agent_id],
                "excess_cost_score": per_agent_excess_cost_score[agent_id],
                "fallback_cost": per_agent_fallback_cost_list[agent_id],
                "communication_score": per_agent_communication_score[agent_id],
                "cost_efficiency": round(cost_efficiency, 6),
                "calendar_density": round(initial_calendar_densities[agent_id], 6),
                "representation_elo": representation_elo_by_agent[agent_id],
                "contribution_score": round(raw_score, 6),
                "density_adjusted_contribution_score": round(raw_score * representation_weight, 6),
            })

        model_contribution_summary: dict[str, dict] = {}
        for model in sorted(set(team_metadata["agent_models"])):
            rows = [row for row in contribution_scores if row["model"] == model]
            if not rows:
                continue
            model_contribution_summary[model] = {
                "agent_count": len(rows),
                "mean_contribution_score": round(
                    sum(float(row["contribution_score"]) for row in rows) / len(rows),
                    6,
                ),
                "mean_density_adjusted_contribution_score": round(
                    sum(float(row["density_adjusted_contribution_score"]) for row in rows) / len(rows),
                    6,
                ),
                "total_messages_sent": sum(int(row["messages_sent"]) for row in rows),
                "total_cost": sum(float(row["cost"]) for row in rows),
            }

        metrics = {
            "num_agents": self.config.num_agents,
            "num_meetings": total_meetings,
            "num_participants": self.config.num_participants,
            "density": self.config.density,
            "dm_cap": self.config.dm_cap,
            "task_id": scenario.get("task_id") or self.config.task_id,
            "task_path": self.config.task_path,
            "difficulty": scenario.get("difficulty"),
            "difficulty_score": scenario.get("difficulty_score"),
            "difficulty_scorer": scenario.get("difficulty_scorer"),
            "setting_normalized_difficulty": scenario.get("setting_normalized_difficulty"),
            "config_normalized_difficulty": scenario.get("config_normalized_difficulty"),
            "coordination_rate": coordination_rate,
            "slot_conflict_rate": slot_conflict_rate,
            "efficiency": efficiency,
            "fairness": fairness,
            "meetings_scheduled": coordinated_meetings,
            "total_dms_sent": total_dms_sent,
            "total_groupchat_messages_sent": total_groupchat_messages_sent,
            "total_participant_groupchat_messages_sent": total_participant_groupchat_messages_sent,
            "total_all_groupchat_messages_sent": total_all_groupchat_messages_sent,
            "total_cheap_talk_messages": total_cheap_talk_messages,
            "total_dm_chars": total_dm_chars,
            "total_groupchat_chars": total_groupchat_chars,
            "total_cheap_talk_chars": total_cheap_talk_chars,
            "total_participant_groupchat_chars": total_participant_groupchat_chars,
            "total_all_groupchat_chars": total_all_groupchat_chars,
            "avg_dm_chars": avg_dm_chars,
            "avg_cheap_talk_chars": avg_cheap_talk_chars,
            "max_dm_chars": max_dm_chars,
            "max_groupchat_chars": max_groupchat_chars,
            "max_participant_groupchat_chars": max_participant_groupchat_chars,
            "max_all_groupchat_chars": max_all_groupchat_chars,
            "dm_chars_per_meeting": dm_chars_per_meeting,
            "cheap_talk_chars_per_meeting": cheap_talk_chars_per_meeting,
            "realized_cost": realized_cost,
            "fallback_displacement_cost": total_fallback_cost,
            "optimal_cost": optimal_cost,
            "greedy_cost": greedy.get("cost"),
            "oracle_cost_replayed": replayed_oracle_cost,
            "scheduled_meeting_ids": scheduled_meeting_ids,
            "unscheduled_meeting_ids": unscheduled_meeting_ids,
            "scheduled_only_optimal_cost": scheduled_only_optimal_cost,
            "scheduled_only_greedy_cost": scheduled_only_greedy_cost,
            "scheduled_only_oracle_cost_replayed": scheduled_only_replayed_oracle_cost,
            "scheduled_only_oracle_assignments": scheduled_only_optimal.get("assignments", {}),
            "per_agent_realized_cost": per_agent_cost_list,
            "oracle_per_agent_cost": oracle_per_agent_cost,
            "per_agent_oracle_cost": oracle_per_agent_cost,
            "per_agent_excess_burden": per_agent_excess_burden,
            "scheduled_only_oracle_per_agent_cost": scheduled_only_oracle_per_agent_cost,
            "per_agent_scheduled_only_oracle_cost": scheduled_only_oracle_per_agent_cost,
            "per_agent_scheduled_only_excess_burden": per_agent_scheduled_only_excess_burden,
            "per_agent_dms_sent": [
                dms_sent_by_agent[i] for i in range(self.config.num_agents)
            ],
            "per_agent_participant_groupchat_messages_sent": [
                participant_groupchat_messages_sent_by_agent[i]
                for i in range(self.config.num_agents)
            ],
            "per_agent_all_groupchat_messages_sent": [
                all_groupchat_messages_sent_by_agent[i] for i in range(self.config.num_agents)
            ],
            "per_agent_groupchat_messages_sent": [
                (
                    participant_groupchat_messages_sent_by_agent[i]
                    + all_groupchat_messages_sent_by_agent[i]
                )
                for i in range(self.config.num_agents)
            ],
            "per_agent_cheap_talk_messages_sent": [
                messages_sent_by_agent[i] for i in range(self.config.num_agents)
            ],
            "per_agent_message_fraction": per_agent_message_fraction,
            "per_agent_communication_score": per_agent_communication_score,
            "per_agent_normalized_excess_cost": per_agent_normalized_excess_cost,
            "per_agent_excess_cost_score": per_agent_excess_cost_score,
            "per_agent_excess_cost_normalizer": per_agent_excess_normalizer,
            "total_excess_burden": sum(per_agent_excess_burden),
            "total_scheduled_only_excess_burden": sum(per_agent_scheduled_only_excess_burden),
            "nosy_agent_count": len(nosy_agent_ids),
            "communication_protocol": communication_protocol,
            "team_model_label": team_metadata["team_model_label"],
            "team_model_counts": team_metadata["team_model_counts"],
            "is_heterogeneous_team": team_metadata["is_heterogeneous_team"],
            "reflection_estimate_count": len(reflection_estimates),
            "mean_contribution_score": (
                sum(float(row["contribution_score"]) for row in contribution_scores) / len(contribution_scores)
                if contribution_scores
                else 0.0
            ),
            "model_contribution_summary": model_contribution_summary,
        }
        metrics.update(compute_headline_scores(metrics, self.config))

        self._append_event("game_end", data={
            "round": len(scenario["meetings"]), "turn": 0, "phase": "GAME_END", "agent_id": None,
            **metrics,
        })

        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "calendars": [agent.calendar.slots for agent in agents],
                "per_agent_cost": per_agent_cost_list,
                "per_agent_fallback_cost": per_agent_fallback_cost_list,
                "oracle_per_agent_cost": oracle_per_agent_cost,
                "per_agent_excess_burden": per_agent_excess_burden,
                "scheduled_only_oracle_per_agent_cost": scheduled_only_oracle_per_agent_cost,
                "per_agent_scheduled_only_excess_burden": per_agent_scheduled_only_excess_burden,
                "per_agent_communication_score": per_agent_communication_score,
                "per_agent_normalized_excess_cost": per_agent_normalized_excess_cost,
                "per_agent_excess_cost_score": per_agent_excess_cost_score,
                "per_agent_messages_sent": [
                    messages_sent_by_agent[i] for i in range(self.config.num_agents)
                ],
                "per_agent_messages_received": [
                    messages_received_by_agent[i] for i in range(self.config.num_agents)
                ],
                "initial_calendar_density_by_agent": initial_calendar_densities,
                "representation_elo_by_agent": representation_elo_by_agent,
                "contribution_scores": contribution_scores,
                "model_contribution_summary": model_contribution_summary,
                "round_outcomes": round_outcomes,
                "reflection_estimates": reflection_estimates,
                "nosy_agent_ids": nosy_agent_ids,
                # The completed trace is self-sufficient for future rating
                # replay: score-margin extraction must not depend on whatever
                # task files happen to exist in a later checkout.
                "rating_context": build_calendar_rating_context(scenario),
                **team_metadata,
            },
            metrics=metrics,
        )
