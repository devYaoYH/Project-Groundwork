"""Frozen Phase 4 orchestration oracle; test-only, never exported."""
import json
from negotiation_game.backend.engine import GameEngine, GameMode, RoundResult
from negotiation_game.backend.defaults import MAX_DECISION_RETRIES

class LegacyEngine(GameEngine):
    async def _request_decision_with_retries(self, agent_impl, agent_id: str,
                                              round_number: int, pub_config: dict,
                                              transcript: list, memory: list) -> tuple[dict[str, int], dict[str, int] | None]:
        """Call decide_allocation with retries on malformed JSON or validation errors.
        Returns (resource_allocation, project_runs).
        """
        for attempt in range(MAX_DECISION_RETRIES):
            alloc = await agent_impl.decide_allocation(
                agent_id, round_number, pub_config, transcript, memory,
            )
            # For project mode, parse project_runs from the allocation if embedded
            project_runs = None
            if isinstance(alloc, dict) and "projects" in alloc:
                proj_raw = alloc.pop("projects")
                if isinstance(proj_raw, dict):
                    project_runs = {k: int(v) for k, v in proj_raw.items() if int(v) > 0}
            alloc = await self._auto_fill_resources(alloc, project_runs, agent_id)
            valid, err = self._validate_allocation(alloc, agent_id)
            if valid:
                return alloc, project_runs
            error_msg = f"Invalid decision: {err}. Please try again with a valid JSON purchase."
            await self._emit("validation_error", {"agent": agent_id, "error": err, "attempt": attempt + 1})
            if hasattr(agent_impl, '_append_user'):
                agent_impl._append_user(error_msg)
        await self._emit("validation_error", {
            "agent": agent_id,
            "error": f"{agent_id} failed to submit a valid decision after {MAX_DECISION_RETRIES} attempts",
        })
        return {}, None

    async def run_round(self, round_number: int) -> RoundResult:
        self._update_projects_for_round(round_number)

        # Build per-agent project update text for rotating games (rounds > 1)
        project_update_a = None
        project_update_b = None
        if self.config.rotate_projects and round_number > 1:
            for label, state in [
                ("agent_a", self.agent_a_state),
                ("agent_b", self.agent_b_state),
            ]:
                if state.project_calculator:
                    projects = state.project_calculator.projects
                    proj_text = "\n".join(
                        f"  - {p.name}: requires [{', '.join(f'{r}×{q}' for r, q in p.requirements.items())}], reward = {p.reward}/run"
                        for p in projects
                    )
                    opp_state = self.agent_b_state if label == "agent_a" else self.agent_a_state
                    if self.config.full_transparency and opp_state.project_calculator:
                        opp_proj_text = "\n".join(
                            f"  - {p.name}: requires [{', '.join(f'{r}×{q}' for r, q in p.requirements.items())}], reward = {p.reward}/run"
                            for p in opp_state.project_calculator.projects
                        )
                        opponent_section = (
                            f"\nThe other party's projects have also changed. Their projects this round:\n"
                            f"{opp_proj_text}\n"
                        )
                    else:
                        opponent_section = f"\nThe other party has their own projects with different requirements and rewards.\n"
                    update = (
                        f"--- NEW PROJECTS FOR THIS ROUND ---\n"
                        f"Your projects have changed. These are your ONLY available projects this round:\n"
                        f"{proj_text}\n"
                        f"{opponent_section}"
                        f"All resource availability, costs, and your budget remain the same."
                    )
                    if label == "agent_a":
                        project_update_a = update
                    else:
                        project_update_b = update
                    await self._emit("project_instructions", {
                        "round": round_number,
                        "agent": label,
                        "prompt": update,
                    })
            await self._emit("projects_rotated", {"round": round_number})

        pub_config_a = self._public_config("agent_a")
        pub_config_b = self._public_config("agent_b")
        # Shifting agents always see round 1 (they think it's a one-shot environment)
        round_num_a = 1 if self._shifting_a else round_number
        round_num_b = 1 if self._shifting_b else round_number
        transcript = []
        alloc_a = None
        alloc_b = None
        proj_runs_a = None
        proj_runs_b = None

        # --- No-talk baseline: skip cheap talk ---
        if not self.config.enable_cheap_talk:
            await self._emit("phase_start", {"round": round_number, "phase": "decision"})
            # In no-talk mode, inject project updates directly before decision
            if project_update_a and hasattr(self.agent_a_impl, '_append_user'):
                self.agent_a_impl._append_user(project_update_a)
            if project_update_b and hasattr(self.agent_b_impl, '_append_user'):
                self.agent_b_impl._append_user(project_update_b)
        else:
            await self._emit("phase_start", {"round": round_number, "phase": "cheap_talk"})

            max_turns = self.config.cheap_talk_turns if self.config.cheap_talk_turns > 0 else 1000
            turn = 0
            while turn < max_turns:
                if alloc_a is not None and alloc_b is not None:
                    break

                # --- Agent A's turn ---
                if alloc_a is None:
                    if alloc_b is not None:
                        notice = {"speaker": "system", "type": "system", "turn": turn,
                                  "message": "Agent B has already made their final selection. Please submit your purchase as a JSON object."}
                        transcript.append(notice)
                        await self._emit("cheap_talk", {"round": round_number, **notice})

                    if self._is_human(self.agent_a_impl):
                        await self._emit("waiting_for_human", {
                            "agent": "agent_a",
                            "round": round_number,
                            "turn": turn,
                            "phase": "cheap_talk",
                            "public_config": pub_config_a,
                        })

                    self._set_stream_callback(self.agent_a_impl, "agent_a", round_number, "cheap_talk", turn)
                    msg_a = await self.agent_a_impl.cheap_talk(
                        "agent_a", round_num_a, turn, pub_config_a,
                        transcript, self.agent_a_state.memory,
                        project_update=project_update_a if turn == 0 else None,
                    )

                    api_meta = self._get_api_meta(self.agent_a_impl)
                    if api_meta and "error_type" in api_meta:
                        await self._emit_api_failure(self.agent_a_impl, "agent_a", round_number, "cheap_talk", turn)

                    await self._emit_thinking(self.agent_a_impl, "agent_a", round_number, transcript, turn=turn)
                    await self._emit_reasoning(self.agent_a_impl, "agent_a", round_number, transcript, turn=turn)

                    try:
                        parsed_alloc, parsed_proj = self._try_parse_purchase(msg_a)
                        if parsed_alloc is not None:
                            parsed_alloc = await self._auto_fill_resources(parsed_alloc, parsed_proj, "agent_a")
                            valid, err = self._validate_allocation(parsed_alloc, "agent_a")
                            if valid:
                                alloc_a = parsed_alloc
                                proj_runs_a = parsed_proj
                                # Emit the speech that accompanied the early decision
                                early_speech = getattr(self.agent_a_impl, 'last_speech', '')
                                if early_speech:
                                    speech_entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": early_speech}
                                    transcript.append(speech_entry)
                                    await self._emit("cheap_talk", {"round": round_number, **speech_entry})
                                entry = {"speaker": "agent_a", "type": "decision", "turn": turn,
                                         "message": f"[DECISION] {json.dumps(alloc_a)}"}
                                transcript.append(entry)
                                event_data = {"round": round_number, "agent": "agent_a", "allocation": alloc_a}
                                if proj_runs_a is not None:
                                    event_data["project_runs"] = proj_runs_a
                                await self._emit("early_decision", event_data)
                            else:
                                error_msg = f"Invalid purchase: {err}. Please fix and resubmit as JSON."
                                if hasattr(self.agent_a_impl, '_append_user'):
                                    self.agent_a_impl._append_user(error_msg)
                                await self._emit("validation_error", {"agent": "agent_a", "error": err})
                                entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                                transcript.append(entry)
                                await self._emit("cheap_talk", {"round": round_number, **entry})
                        else:
                            entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                            transcript.append(entry)
                            await self._emit("cheap_talk", {"round": round_number, **entry})
                    except ValueError:
                        error_msg = f"Your response looked like JSON but could not be parsed. Please send either a plain text message or a valid JSON purchase."
                        if hasattr(self.agent_a_impl, '_append_user'):
                            self.agent_a_impl._append_user(error_msg)
                        await self._emit("validation_error", {"agent": "agent_a", "error": "malformed JSON"})
                        entry = {"speaker": "agent_a", "type": "speech", "turn": turn, "message": msg_a}
                        transcript.append(entry)
                        await self._emit("cheap_talk", {"round": round_number, **entry})

                if alloc_a is not None and alloc_b is not None:
                    break

                # --- Agent B's turn ---
                if alloc_b is None:
                    if alloc_a is not None:
                        notice = {"speaker": "system", "type": "system", "turn": turn,
                                  "message": "Agent A has already made their final selection. Please submit your purchase as a JSON object."}
                        transcript.append(notice)
                        await self._emit("cheap_talk", {"round": round_number, **notice})

                    if self._is_human(self.agent_b_impl):
                        await self._emit("waiting_for_human", {
                            "agent": "agent_b",
                            "round": round_number,
                            "turn": turn,
                            "phase": "cheap_talk",
                            "public_config": pub_config_b,
                        })

                    self._set_stream_callback(self.agent_b_impl, "agent_b", round_number, "cheap_talk", turn)
                    msg_b = await self.agent_b_impl.cheap_talk(
                        "agent_b", round_num_b, turn, pub_config_b,
                        transcript, self.agent_b_state.memory,
                        project_update=project_update_b if turn == 0 else None,
                    )

                    api_meta = self._get_api_meta(self.agent_b_impl)
                    if api_meta and "error_type" in api_meta:
                        await self._emit_api_failure(self.agent_b_impl, "agent_b", round_number, "cheap_talk", turn)

                    await self._emit_thinking(self.agent_b_impl, "agent_b", round_number, transcript, turn=turn)
                    await self._emit_reasoning(self.agent_b_impl, "agent_b", round_number, transcript, turn=turn)

                    try:
                        parsed_alloc, parsed_proj = self._try_parse_purchase(msg_b)
                        if parsed_alloc is not None:
                            parsed_alloc = await self._auto_fill_resources(parsed_alloc, parsed_proj, "agent_b")
                            valid, err = self._validate_allocation(parsed_alloc, "agent_b")
                            if valid:
                                alloc_b = parsed_alloc
                                proj_runs_b = parsed_proj
                                early_speech = getattr(self.agent_b_impl, 'last_speech', '')
                                if early_speech:
                                    speech_entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": early_speech}
                                    transcript.append(speech_entry)
                                    await self._emit("cheap_talk", {"round": round_number, **speech_entry})
                                entry = {"speaker": "agent_b", "type": "decision", "turn": turn,
                                         "message": f"[DECISION] {json.dumps(alloc_b)}"}
                                transcript.append(entry)
                                event_data = {"round": round_number, "agent": "agent_b", "allocation": alloc_b}
                                if proj_runs_b is not None:
                                    event_data["project_runs"] = proj_runs_b
                                await self._emit("early_decision", event_data)
                            else:
                                error_msg = f"Invalid purchase: {err}. Please fix and resubmit as JSON."
                                if hasattr(self.agent_b_impl, '_append_user'):
                                    self.agent_b_impl._append_user(error_msg)
                                await self._emit("validation_error", {"agent": "agent_b", "error": err})
                                entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                                transcript.append(entry)
                                await self._emit("cheap_talk", {"round": round_number, **entry})
                        else:
                            entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                            transcript.append(entry)
                            await self._emit("cheap_talk", {"round": round_number, **entry})
                    except ValueError:
                        error_msg = f"Your response looked like JSON but could not be parsed. Please send either a plain text message or a valid JSON purchase."
                        if hasattr(self.agent_b_impl, '_append_user'):
                            self.agent_b_impl._append_user(error_msg)
                        await self._emit("validation_error", {"agent": "agent_b", "error": "malformed JSON"})
                        entry = {"speaker": "agent_b", "type": "speech", "turn": turn, "message": msg_b}
                        transcript.append(entry)
                        await self._emit("cheap_talk", {"round": round_number, **entry})

                turn += 1

        # --- Decision phase ---
        if (alloc_a is None or alloc_b is None) and self.config.enable_cheap_talk:
            await self._emit("phase_start", {"round": round_number, "phase": "decision"})

        if alloc_a is None:
            if self._is_human(self.agent_a_impl):
                await self._emit("waiting_for_human", {
                    "agent": "agent_a",
                    "round": round_number,
                    "phase": "decision",
                    "public_config": pub_config_a,
                })
            self._set_stream_callback(self.agent_a_impl, "agent_a", round_number, "decision")
            alloc_a, proj_runs_a = await self._request_decision_with_retries(
                self.agent_a_impl, "agent_a", round_num_a,
                pub_config_a, transcript, self.agent_a_state.memory,
            )
            api_meta = self._get_api_meta(self.agent_a_impl)
            if api_meta and "error_type" in api_meta:
                await self._emit_api_failure(self.agent_a_impl, "agent_a", round_number, "decision")
            await self._emit_thinking(self.agent_a_impl, "agent_a", round_number, transcript)
            await self._emit_reasoning(self.agent_a_impl, "agent_a", round_number, transcript)

        if alloc_b is None:
            if self._is_human(self.agent_b_impl):
                await self._emit("waiting_for_human", {
                    "agent": "agent_b",
                    "round": round_number,
                    "phase": "decision",
                    "public_config": pub_config_b,
                })
            self._set_stream_callback(self.agent_b_impl, "agent_b", round_number, "decision")
            alloc_b, proj_runs_b = await self._request_decision_with_retries(
                self.agent_b_impl, "agent_b", round_num_b,
                pub_config_b, transcript, self.agent_b_state.memory,
            )
            api_meta = self._get_api_meta(self.agent_b_impl)
            if api_meta and "error_type" in api_meta:
                await self._emit_api_failure(self.agent_b_impl, "agent_b", round_number, "decision")
            await self._emit_thinking(self.agent_b_impl, "agent_b", round_number, transcript)
            await self._emit_reasoning(self.agent_b_impl, "agent_b", round_number, transcript)

        # --- Resolve round ---
        total_demanded = {}
        for r in self.config.resource_types:
            total_demanded[r] = alloc_a.get(r, 0) + alloc_b.get(r, 0)

        overdrawn = any(
            total_demanded[r] > self.config.resource_supply[r]
            for r in self.config.resource_types
        )

        proj_details_a = None
        proj_details_b = None

        if overdrawn:
            reward_a = 0.0
            reward_b = 0.0
        else:
            # Project-based rewards
            calc_a = self.agent_a_state.project_calculator
            calc_b = self.agent_b_state.project_calculator

            # Resolve project runs
            resolved_a, auto_allocated_a = calc_a.resolve_project_runs(alloc_a, proj_runs_a)
            resolved_b, auto_allocated_b = calc_b.resolve_project_runs(alloc_b, proj_runs_b)

            # Emit auto-allocation events for observability
            if auto_allocated_a:
                await self._emit("project_auto_allocation", {
                    "round": round_number,
                    "agent": "agent_a",
                    "purchased": alloc_a,
                    "resolved_runs": resolved_a,
                    "reason": "Agent submitted resource purchases without explicit project allocations; resources auto-assigned greedily to projects.",
                })
            if auto_allocated_b:
                await self._emit("project_auto_allocation", {
                    "round": round_number,
                    "agent": "agent_b",
                    "purchased": alloc_b,
                    "resolved_runs": resolved_b,
                    "reason": "Agent submitted resource purchases without explicit project allocations; resources auto-assigned greedily to projects.",
                })

            # Compute rewards with scenario-level synergy
            details_a = calc_a.compute_reward(resolved_a, alloc_a, alloc_b, self.config.scenario_synergy)
            details_b = calc_b.compute_reward(resolved_b, alloc_b, alloc_a, self.config.scenario_synergy)

            # Add auto-allocation flag to project details for schema
            details_a["auto_allocated"] = auto_allocated_a
            details_b["auto_allocated"] = auto_allocated_b

            reward_a = details_a["total_reward"]
            reward_b = details_b["total_reward"]
            proj_details_a = details_a
            proj_details_b = details_b

        self.agent_a_state.cumulative_reward += reward_a
        self.agent_b_state.cumulative_reward += reward_b

        # Update memories
        self.agent_a_state.memory.append({
            "round": round_number,
            "my_allocation": alloc_a,
            "my_reward": reward_a,
            "overdrawn": overdrawn,
            "cheap_talk": transcript,
        })
        self.agent_b_state.memory.append({
            "round": round_number,
            "my_allocation": alloc_b,
            "my_reward": reward_b,
            "overdrawn": overdrawn,
            "cheap_talk": transcript,
        })

        # Notify agents of round results
        if hasattr(self.agent_a_impl, 'notify_round_result'):
            self.agent_a_impl.notify_round_result(
                round_num_a, alloc_a, reward_a, alloc_b, reward_b,
                overdrawn, self.config.visible_opponent_reward,
                proj_details_a,
            )
        if hasattr(self.agent_b_impl, 'notify_round_result'):
            self.agent_b_impl.notify_round_result(
                round_num_b, alloc_b, reward_b, alloc_a, reward_a,
                overdrawn, self.config.visible_opponent_reward,
                proj_details_b,
            )

        result = RoundResult(
            round_number=round_number,
            agent_a_allocation=alloc_a,
            agent_b_allocation=alloc_b,
            total_demanded=total_demanded,
            resource_supply=dict(self.config.resource_supply),
            overdrawn=overdrawn,
            agent_a_reward=reward_a,
            agent_b_reward=reward_b,
            agent_a_project_runs=proj_details_a,
            agent_b_project_runs=proj_details_b,
            cheap_talk_transcript=transcript,
        )

        round_complete_data = {
            "round_number": round_number,
            "agent_a_allocation": alloc_a,
            "agent_b_allocation": alloc_b,
            "total_demanded": total_demanded,
            "resource_supply": dict(self.config.resource_supply),
            "overdrawn": overdrawn,
            "agent_a_reward": reward_a,
            "agent_b_reward": reward_b,
        }
        if proj_details_a:
            round_complete_data["agent_a_project_runs"] = proj_details_a
        if proj_details_b:
            round_complete_data["agent_b_project_runs"] = proj_details_b

        await self._emit("round_complete", round_complete_data)
        self.results.append(result)
        return result

    async def run_game(self) -> dict:
        game_start_config = {
            "mode": self.config.mode.value,
            "num_rounds": self.config.num_rounds,
            "resource_types": self.config.resource_types,
            "resource_supply": self.config.resource_supply,
            "resource_costs": self.config.resource_costs,
            "agent_budget": self.config.agent_budget,
            "cheap_talk_turns": self.config.cheap_talk_turns,
            "first_speaker": self.config.first_speaker,
            "seed": self.config.seed,
            "swapped": self.config.swapped,
            "goal": self.config.goal,
            "thinking": self.config.thinking,
            "visible_utilities": self.config.visible_utilities,
            "visible_opponent_reward": self.config.visible_opponent_reward,
            "enable_cheap_talk": self.config.enable_cheap_talk,
            "share_projects": self.config.share_projects,
            "think_about_opponent": self.config.think_about_opponent,
            "maximize_joint": self.config.maximize_joint,
            "full_transparency": self.config.full_transparency,
        }
        game_start_config["agent_projects"] = self.config.agent_projects
        if self.config.scenario_synergy:
            game_start_config["scenario_synergy"] = self.config.scenario_synergy
        if self.config.oracle_stats:
            game_start_config["oracle_stats"] = self.config.oracle_stats

        await self._emit("game_start", {
            "episode_uid": self.config.episode_uid,
            "config": game_start_config,
        })

        # Pre-initialize LLM agents
        for label, impl in [("agent_a", self.agent_a_impl), ("agent_b", self.agent_b_impl)]:
            if hasattr(impl, '_init_session'):
                impl._init_session(label, self._public_config(label))
            prompt = getattr(impl, '_system_prompt', None)
            if prompt:
                await self._emit("system_prompt", {"agent": label, "prompt": prompt})

        for round_num in range(1, self.config.num_rounds + 1):
            if self._stopped:
                await self._emit("game_stopped", {"reason": "stopped by user", "after_round": round_num - 1})
                break

            if self.config.mode == GameMode.SHIFTING and round_num > 1:
                if self._shifting_a:
                    if hasattr(self.agent_a_impl, 'reset_context'):
                        self.agent_a_impl.reset_context()
                    await self._emit("context_reset", {"round": round_num, "agent": "agent_a"})
                if self._shifting_b:
                    if hasattr(self.agent_b_impl, 'reset_context'):
                        self.agent_b_impl.reset_context()
                    await self._emit("context_reset", {"round": round_num, "agent": "agent_b"})

            await self.run_round(round_num)

        # Build result
        rounds = []
        for r in self.results:
            stats = self._compute_round_stats(r.cheap_talk_transcript, self.config.cheap_talk_turns)
            round_data = {
                "round_number": r.round_number,
                "agent_a_allocation": r.agent_a_allocation,
                "agent_b_allocation": r.agent_b_allocation,
                "total_demanded": r.total_demanded,
                "resource_supply": r.resource_supply,
                "overdrawn": r.overdrawn,
                "agent_a_reward": r.agent_a_reward,
                "agent_b_reward": r.agent_b_reward,
                "stats": stats,
            }
            if r.agent_a_project_runs:
                round_data["agent_a_project_runs"] = r.agent_a_project_runs
            if r.agent_b_project_runs:
                round_data["agent_b_project_runs"] = r.agent_b_project_runs
            rounds.append(round_data)

        summary = {
            "episode_uid": self.config.episode_uid,
            "mode": self.config.mode.value,
            "num_rounds": len(self.results),
            "agent_a_cumulative_reward": self.agent_a_state.cumulative_reward,
            "agent_b_cumulative_reward": self.agent_b_state.cumulative_reward,
            "rounds": rounds,
            "first_speaker": self.config.first_speaker,
            "seed": self.config.seed,
            "swapped": self.config.swapped,
            "stopped": self._stopped,
            "api_failures": self._api_failures,
        }

        summary["agent_projects"] = self.config.agent_projects

        # Build per_round_scenarios: for rotating games, already populated per round;
        # for non-rotating games, repeat the same scenario for each round
        if self._per_round_scenarios:
            summary["per_round_scenarios"] = self._per_round_scenarios
        else:
            static_scenario = {
                "agent_projects": self.config.agent_projects,
            }
            if self.config.oracle_stats:
                static_scenario["oracle_stats"] = self.config.oracle_stats
            summary["per_round_scenarios"] = [static_scenario] * len(self.results)

        # Calculate theoretical joint maximum from oracle stats
        # For stable agents: sum across all rounds
        # For shifted agents: only the final round (they don't remember previous rounds)
        theoretical_joint_max_sum = None
        theoretical_joint_max_final = None

        if self._per_round_scenarios:
            joint_max_sum = 0.0
            for scenario in self._per_round_scenarios:
                oracle = scenario.get("oracle_stats")
                if oracle and "collab_max" in oracle:
                    joint_max_sum += oracle["collab_max"]
            if joint_max_sum > 0:
                theoretical_joint_max_sum = joint_max_sum
                # Final round oracle stats
                final_oracle = self._per_round_scenarios[-1].get("oracle_stats")
                if final_oracle and "collab_max" in final_oracle:
                    theoretical_joint_max_final = final_oracle["collab_max"]
        elif self.config.oracle_stats:
            oracle = self.config.oracle_stats
            collab_max = oracle["collab_max"] if isinstance(oracle, dict) else oracle.collab_max
            theoretical_joint_max_sum = collab_max * len(self.results)
            theoretical_joint_max_final = collab_max

        # Request post-environment reflections from LLM agents
        # Skip reflections when cheap talk is disabled (no-talk baseline)
        reflections = {}
        if self.config.enable_cheap_talk:
            for label, impl in [("agent_a", self.agent_a_impl), ("agent_b", self.agent_b_impl)]:
                if hasattr(impl, 'reflect'):
                    is_shifted = self._shifting_a if label == "agent_a" else self._shifting_b

                    if is_shifted:
                        # Shifted agents only have context from the last round
                        last_result = self.results[-1]
                        own_reward = last_result.agent_a_reward if label == "agent_a" else last_result.agent_b_reward
                        opp_reward = last_result.agent_b_reward if label == "agent_a" else last_result.agent_a_reward
                    else:
                        own_reward = self.agent_a_state.cumulative_reward if label == "agent_a" else self.agent_b_state.cumulative_reward
                        opp_reward = self.agent_b_state.cumulative_reward if label == "agent_a" else self.agent_a_state.cumulative_reward

                    # Use final round oracle for shifted agents, sum for stable agents
                    theoretical_joint_max = theoretical_joint_max_final if is_shifted else theoretical_joint_max_sum

                    reflection_text = await impl.reflect(
                        label, len(self.results) if not is_shifted else 1,
                        own_reward, opp_reward,
                        self.config.visible_opponent_reward,
                        theoretical_joint_max,
                    )

                    if reflection_text:
                        reflections[label] = reflection_text
                        await self._emit("agent_reflection", {
                            "agent": label,
                            "reflection": reflection_text,
                            "cumulative_reward": own_reward,
                            "theoretical_joint_max": theoretical_joint_max,
                        })

        # Add reflections to summary if any were generated
        if reflections:
            summary["reflections"] = reflections

        await self._emit("game_complete", summary)
        return summary
