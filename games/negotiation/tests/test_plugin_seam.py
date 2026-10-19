"""Seam tests: negotiation as an a2a-engine plugin.

This is the riskiest conversion in the merge. Negotiation previously ran through
an HTTP server and persisted a bespoke Firestore document; it now runs in-process
and emits a EpisodeTrace. These tests pin the translation layer — the config
projection, the event adapter, and the reproducibility of generated scenarios —
without needing API keys, a server, or a cloud backend.
"""

import asyncio

import pytest

import negotiation_game  # noqa: F401  (registers the environment)
from a2a_engine import EpisodeTrace, get_environment_spec
from a2a_engine.dataset import EpisodeDataset
from negotiation_game.backend.engine import GameConfig, GameMode
from negotiation_game.game import (
    ENGINE_PINNED_FIELDS,
    NegotiationConfig,
    NegotiationGame,
    _to_engine_config,
)


HEURISTIC = {
    "environment_id": "negotiation",
    "num_rounds": 2,
    "cheap_talk_turns": 2,
    "agents": [{"type": "heuristic"}, {"type": "heuristic"}],
}


def test_explicit_producer_metadata_and_legacy_callback_compatibility():
    from types import SimpleNamespace
    from negotiation_game.backend.engine import GameEngine

    game = NegotiationGame(HEURISTIC)
    agent = SimpleNamespace(_on_stream_start=None, last_thinking="Thought", last_reasoning="Reason")
    engine = GameEngine(GameConfig(), agent, agent)
    legacy = []

    async def observer(kind, data):
        legacy.append((kind, dict(data)))

    engine.on_event(observer)
    engine.on_event(game._on_engine_event, envelope_metadata=True)

    async def emit():
        engine._set_stream_callback(agent, "agent_a", 1, "decision")
        await agent._on_stream_start()
        await agent._on_stream_start()
        await engine._resolve_round(1, {}, {}, None, None, [])
        await engine._emit_thinking(agent, "agent_a", 1)
        await engine._emit_reasoning(agent, "agent_a", 1)
        await engine._emit("llm_streaming", {"synthetic": True})
        await engine._emit("unknown", {}, synthetic=True)

    asyncio.run(emit())
    assert [event.synthetic for event in game.events] == [True, True, True, False, False, False, True]
    assert [event.type for event in game.events[:3]] == ["llm_streaming", "project_auto_allocation", "project_auto_allocation"]
    assert all("synthetic" not in data for _, data in legacy[:5])
    assert game.events[5].data == {"synthetic": True}
    assert len(legacy) == len(game.events)


def test_run_preserves_semantic_events_without_synthetic_markers():
    trace = NegotiationGame(HEURISTIC, dry_run=True).run()
    assert any(event.synthetic for event in trace.events)
    for event in trace.events:
        assert event.synthetic == (event.type == "project_auto_allocation")
        assert "synthetic" not in event.data


# --- registration ------------------------------------------------------------


def test_game_registers_with_its_declarations():
    spec = get_environment_spec("negotiation")
    assert spec.cls is NegotiationGame
    assert spec.storage == {"backend": "sqlite"}
    assert spec.resolve_config is not None
    # A negotiation dry run uses heuristic agents, so it must not demand keys.
    assert spec.dry_run_checks_keys is False


# --- config projection -------------------------------------------------------


def test_every_shared_field_reaches_the_engine_config():
    """The projection is mechanical; a field silently dropped here changes the environment."""
    cfg = NegotiationConfig(
        environment_id="negotiation",
        num_rounds=7,
        cheap_talk_turns=4,
        enable_cheap_talk=False,
        maximize_joint=True,
        full_transparency=True,
        agent_budget=12.5,
        seed=3,
    )
    engine_cfg = _to_engine_config(cfg)

    assert isinstance(engine_cfg, GameConfig)
    assert engine_cfg.num_rounds == 7
    assert engine_cfg.cheap_talk_turns == 4
    assert engine_cfg.enable_cheap_talk is False
    assert engine_cfg.maximize_joint is True
    assert engine_cfg.full_transparency is True
    assert engine_cfg.agent_budget == 12.5
    assert engine_cfg.seed == 3


def test_shared_fields_are_not_silently_lost():
    """Guards against drift: any field on both sides must actually be copied."""
    shared = set(NegotiationConfig.model_fields) & set(GameConfig.__dataclass_fields__)

    cfg = NegotiationConfig(environment_id="negotiation")
    engine_cfg = _to_engine_config(cfg)
    for field in shared:
        if field == "mode":
            continue  # str -> enum, checked separately
        if field in ENGINE_PINNED_FIELDS:
            continue  # engine overrides these; see the pinning test below
        assert getattr(engine_cfg, field) == getattr(cfg, field), f"{field} not projected"


def test_engine_pinned_fields_are_reflected_back_into_the_trace_config():
    """GameConfig.__post_init__ overrides these no matter what the YAML says.

    If the trace recorded the *requested* value it would misreport the
    conditions the environment ran under — exactly the reproducibility failure the
    manifest contract is meant to prevent. So the environment syncs them back.
    """
    environment = NegotiationGame(
        config={**HEURISTIC, "visible_opponent_reward": True, "visible_utilities": False}
    )
    environment.run()

    assert environment.config.visible_opponent_reward is False
    assert environment.config.visible_utilities is True


def test_mode_string_becomes_the_engine_enum():
    assert _to_engine_config(NegotiationConfig(mode="shifting")).mode is GameMode.SHIFTING
    assert _to_engine_config(NegotiationConfig(mode="stable")).mode is GameMode.STABLE


def test_unknown_engine_fields_are_dropped_not_crashed():
    """Experiment YAML carries runner-only keys the engine dataclass rejects."""
    cfg = NegotiationConfig(
        environment_id="negotiation", experiment_name="e", episode_id="e.b.0", extra={"x": 1}
    )
    _to_engine_config(cfg)  # must not raise


# --- event adaptation --------------------------------------------------------


def test_cheap_talk_is_normalized_to_speaker_text():
    """The judge and EpisodeDataset key off {speaker, text}; the engine emits {speaker, message}."""
    environment = NegotiationGame(config=HEURISTIC)
    environment._record("cheap_talk", {"speaker": "agent_a", "message": "hello", "turn": 1})

    event = environment.events[0]
    assert event.data["speaker"] == "agent_a"
    assert event.data["text"] == "hello"
    # The original payload is preserved for environment-specific analysis.
    assert event.data["message"] == "hello"
    assert event.data["turn"] == 1


def test_non_message_events_are_passed_through_untouched():
    environment = NegotiationGame(config=HEURISTIC)
    environment._record("round_complete", {"round": 1, "overdrawn": True})
    assert environment.events[0].data == {"round": 1, "overdrawn": True}
    assert "text" not in environment.events[0].data


def test_message_event_without_text_is_not_fabricated():
    environment = NegotiationGame(config=HEURISTIC)
    environment._record("cheap_talk", {"speaker": "system", "turn": 2})
    assert "text" not in environment.events[0].data


def test_engine_callback_signature_matches_the_adapter():
    """The engine calls cb(event_type, data) positionally, not cb(dict)."""
    environment = NegotiationGame(config=HEURISTIC)
    asyncio.run(environment._on_engine_event("phase_start", {"phase": "decision"}))
    assert environment.events[0].type == "phase_start"
    assert environment.events[0].data == {"phase": "decision"}


# --- end to end (heuristic agents, no API keys) ------------------------------


@pytest.fixture(scope="module")
def trace():
    return NegotiationGame(config={**HEURISTIC, "seed": 11}).run()


def test_run_returns_a_valid_game_trace(trace):
    assert isinstance(trace, EpisodeTrace)
    assert trace.started_at and trace.ended_at
    assert trace.config.environment_id == "negotiation"


def test_trace_contains_lifecycle_and_talk_events(trace):
    types = {e.type for e in trace.events}
    assert "game_start" in types
    assert "cheap_talk" in types


def test_metrics_are_populated(trace):
    assert trace.metrics["num_rounds"] == 2
    assert trace.metrics["agent_a_total_reward"] is not None
    assert trace.metrics["agent_b_total_reward"] is not None


def test_fairness_is_none_rather_than_zero_when_undefined():
    """min/max is undefined if the leader scored nothing; 0.0 would be a lie."""
    from negotiation_game.game import _metrics_from_result

    m = _metrics_from_result(
        {"rounds": [], "agent_a_cumulative_reward": 0, "agent_b_cumulative_reward": 0}
    )
    assert m["fairness"] is None


def test_metrics_read_the_engines_actual_result_keys():
    """The engine emits agent_*_cumulative_reward, not agent_*_total_reward."""
    from negotiation_game.game import _metrics_from_result

    m = _metrics_from_result({
        "rounds": [{"overdrawn": True}, {"overdrawn": False}],
        "agent_a_cumulative_reward": 4.0,
        "agent_b_cumulative_reward": 8.0,
    })
    assert m["agent_a_total_reward"] == 4.0
    assert m["joint_reward"] == 12.0
    assert m["fairness"] == 0.5
    assert m["overdrawn_rounds"] == 1


def test_trace_is_readable_by_the_shared_dataset_layer(tmp_path, trace):
    """Cross-environment analysis is the point of the merge: one loader, both games."""
    from a2a_engine.tracing import write_episode

    trace.episode_uid = "neg-1"
    write_episode(trace, tmp_path, experiment_name="e")

    ds = EpisodeDataset.from_dir(tmp_path / "e")
    assert ds.to_episodes_df().shape[0] == 1
    messages = ds.to_messages_df()
    assert not messages.empty, "cheap talk must surface as messages"
    assert set(messages["speaker"]) <= {"agent_a", "agent_b", "system"}


# --- resolve_config reproducibility ------------------------------------------


def test_scenario_pool_config_renames_mc_ratio_without_solving():
    from negotiation_game.resolve import resolve_config

    out = resolve_config({"scenario_pool_path": "pool.json", "mc_ratio": 0.8})
    assert out["target_mc_ratio"] == 0.8
    assert "mc_ratio" not in out


def test_resolve_is_a_noop_without_mc_ratio():
    from negotiation_game.resolve import resolve_config

    cfg = {"environment_id": "negotiation", "num_rounds": 5}
    assert resolve_config(cfg) == cfg


def test_resolve_does_not_mutate_the_input():
    from negotiation_game.resolve import resolve_config

    cfg = {"scenario_pool_path": "p", "mc_ratio": 0.8}
    resolve_config(cfg)
    assert cfg == {"scenario_pool_path": "p", "mc_ratio": 0.8}


def test_mc_ratio_is_baked_into_the_config_so_the_run_is_reproducible(monkeypatch):
    """The reason resolve_config exists.

    The SA solver is stochastic. Under the old HTTP runner the generated scenario
    lived only in the request body, so a run could not be rebuilt from its YAML.
    Resolving into the config puts it in the trace.
    """
    import random
    from negotiation_game.backend import optimizer
    from negotiation_game.resolve import resolve_config

    # Exercise real generation and oracle computation, without an unbounded
    # stochastic search or optional naming's ADC/provider discovery.
    generate = optimizer.generate_scenario
    generated = []
    monkeypatch.setattr(optimizer, "random", random.Random(0))
    monkeypatch.setattr(optimizer, "name_projects", lambda projects: projects)

    def bounded_scenario(**settings):
        assert settings["target_mc"] == 0.8
        result = generate(**settings, max_iter=16)
        generated.append(result)
        return result

    monkeypatch.setattr(optimizer, "generate_scenario", bounded_scenario)
    out = resolve_config({
        "mc_ratio": 0.8,
        "resource_types": ["wood", "stone"],
        "resource_costs": {"wood": 1.0, "stone": 1.5},
        "resource_supply": {"wood": 6, "stone": 6},
        "agent_budget": 10,
    })
    assert len(generated) == 1
    assert out["agent_projects"] == generated[0]["agent_projects"]
    assert out["oracle_stats"] == generated[0]["oracle_stats"]
    assert out["agent_projects"] and out["oracle_stats"]
    assert out["requested_mc_ratio"] == 0.8
    assert "mc_ratio" not in out


# --- shared judge layer ------------------------------------------------------


def test_shared_judge_prompt_builds_from_a_negotiation_trace(tmp_path, trace):
    """The payoff of the merge: one judge prompt builder, both games.

    This only works because the event adapter normalizes cheap talk to
    {speaker, text} — the shape the environment-agnostic transcript renderer reads.
    """
    from a2a_judge import build_transcript_prompt
    from a2a_engine.tracing import write_episode

    trace.episode_uid = "neg-judge-1"
    write_episode(trace, tmp_path, experiment_name="e")

    ds = EpisodeDataset.from_dir(tmp_path / "e")
    messages = build_transcript_prompt(next(iter(ds)))

    assert messages and messages[0]["role"] == "system"
    body = messages[-1]["content"]
    assert "agent_a" in body, "the transcript must name its speakers"
