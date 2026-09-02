from calendar_game.ratings import (
    CalendarRatingAdapter,
    CalendarLegacyScoreMarginMetricExtractor,
    SCORE_MARGIN_RATING_VARIANT,
    extract_calendar_rating_event,
    make_calendar_vps_artifact,
)


def test_legacy_score_margin_extractor_preserves_maximizing_order():
    trace = {
        "game_id": "legacy-score-margin",
        "config": {
            "game_name": "calendar",
            "num_agents": 2,
            "agents": [{"model": "model-a"}, {"model": "model-b"}],
        },
        "metrics": {"scheduled_only_oracle_assignments": {}},
        "final_state": {
            "rating_context": {
                "calendars": [
                    [{"errand_id": 1, "cost": 1}, None],
                    [None, None],
                ],
                "meetings": [{"id": 1, "participants": [0, 1]}],
            },
            "contribution_scores": [
                {"agent_id": 0, "coordination_rate": 1.0},
                {"agent_id": 1, "coordination_rate": 1.0},
            ],
        },
        "events": [{
            "type": "batch_applied",
            "data": {
                "agent_id": 0,
                "actions": [{"type": "reschedule", "item_id": 1}],
            },
        }],
    }

    result = CalendarLegacyScoreMarginMetricExtractor().extract(trace, {})

    assert result is not None
    assert result.values["score_margin"] == {"agent_0": -1.0, "agent_1": -0.0}
    assert result.metadata["compatibility"] == "pre-v1 calendar.rating.v1"


def test_score_margin_variant_counts_varied_errand_units_against_oracle():
    trace = {
        "game_id": "g1",
        "config": {
            "game_name": "calendar",
            "num_agents": 2,
            "task_path": "tasks/calbench_90_varied.jsonl",
            "task_id": "synthetic_varied",
            "agents": [
                {"model": "model-a"},
                {"model": "model-b"},
            ],
        },
        "metrics": {
            "per_agent_scheduled_only_excess_burden": [7, 0],
            "scheduled_only_oracle_assignments": {"1": 0},
        },
        "final_state": {
            "contribution_scores": [
                {"agent_id": 0, "coordination_rate": 1.0},
                {"agent_id": 1, "coordination_rate": 1.0},
            ],
            "calendars": [
                [None, {"errand_id": 1, "cost": 100}, {"errand_id": 2, "cost": 10}],
                [None, None, None],
            ],
        },
        "events": [
            {
                "type": "batch_applied",
                "data": {
                    "agent_id": 0,
                    "actions": [
                        {"type": "reschedule", "item_id": 1, "from_slot": 0, "to_slot": 1},
                        {"type": "reschedule", "item_id": 2, "from_slot": 2, "to_slot": 1},
                    ],
                },
            }
        ],
    }
    task_scenario = {
        "calendars": [
            [{"errand_id": 1, "cost": 100}, None, {"errand_id": 2, "cost": 10}],
            [None, None, None],
        ],
        "meetings": [{"id": 1, "participants": [0, 1]}],
    }

    default_event = extract_calendar_rating_event(trace, task_scenario=task_scenario)
    margin_event = extract_calendar_rating_event(
        trace,
        rating_variant=SCORE_MARGIN_RATING_VARIANT,
        task_scenario=task_scenario,
    )

    assert default_event.metric_scores["excess_cost"] == {"agent_0": 7.0, "agent_1": 0.0}
    assert margin_event.metric_scores["excess_cost"] == {"agent_0": 2.0, "agent_1": 0.0}
    assert margin_event.metadata["raw_metric_scores"]["excess_cost"] == {"agent_0": 7.0, "agent_1": 0.0}
    assert margin_event.metadata["metric_tie_tolerances"]["excess_cost"] == 2.0


def test_score_margin_variant_caps_uniform_excess_cost():
    trace = {
        "game_id": "g2",
        "config": {
            "game_name": "calendar",
            "num_agents": 2,
            "task_path": "tasks/calbench_90_uniform.jsonl",
            "task_id": "synthetic_uniform",
            "agents": [
                {"model": "model-a"},
                {"model": "model-b"},
            ],
        },
        "metrics": {
            "per_agent_scheduled_only_excess_burden": [9, 0],
            "scheduled_only_oracle_assignments": {},
        },
        "final_state": {
            "contribution_scores": [
                {"agent_id": 0, "coordination_rate": 1.0},
                {"agent_id": 1, "coordination_rate": 1.0},
            ],
            "calendars": [[None], [None]],
        },
        "events": [
            {
                "type": "batch_applied",
                "data": {
                    "agent_id": 0,
                    "actions": [
                        {"type": "reschedule", "item_id": item_id, "from_slot": 0, "to_slot": 1}
                        for item_id in range(1, 8)
                    ],
                },
            }
        ],
    }
    task_scenario = {
        "calendars": [
            [{"errand_id": item_id, "cost": 1} for item_id in range(1, 8)],
            [None for _ in range(7)],
        ],
        "meetings": [],
    }

    event = extract_calendar_rating_event(
        trace,
        rating_variant=SCORE_MARGIN_RATING_VARIANT,
        task_scenario=task_scenario,
    )

    assert event.metric_scores["excess_cost"]["agent_0"] == 5.0
    assert event.metadata["metric_tie_tolerances"]["excess_cost"] == 1.0


def test_rating_event_uses_explicit_rating_player_id():
    trace = {
        "game_id": "game-1",
        "config": {
            "game_name": "calendar",
            "num_agents": 2,
            "agents": [
                {
                    "type": "dspy",
                    "model": "publishers/google/models/gemini-3-flash-preview",
                    "rating_player_id": "gemini-3-flash-preview-redteam-c006",
                },
                {"type": "llm", "model": "publishers/google/models/gemini-3-flash-preview"},
            ],
        },
        "metrics": {
            "coordination_rate": 1.0,
            "per_agent_scheduled_only_excess_burden": [0.0, 1.0],
        },
        "final_state": {
            "contribution_scores": [
                {"agent_id": 0, "coordination_rate": 1.0},
                {"agent_id": 1, "coordination_rate": 0.5},
            ],
        },
    }

    event = extract_calendar_rating_event(trace)

    assert event is not None
    assert event.participants[0].player_id == "gemini-3-flash-preview-redteam-c006"
    assert event.participants[1].player_id == "publishers/google/models/gemini-3-flash-preview"


def test_registered_adapter_uses_digest_bound_vps_artifact_without_task_file():
    trace = {
        "game_id": "game-2",
        "config": {
            "game_name": "calendar",
            "num_agents": 2,
            "agents": [{"model": "model-a"}, {"model": "model-b"}],
        },
        "metrics": {"per_agent_scheduled_only_excess_burden": [0.0, 1.0]},
        "final_state": {
            "rating_context": {"calendars": [[None], [None]], "meetings": []},
            "contribution_scores": [
                {"agent_id": 0, "coordination_rate": 1.0},
                {"agent_id": 1, "coordination_rate": 0.5},
            ],
        },
        "events": [],
    }
    artifact = make_calendar_vps_artifact(trace, {0: 0.1, 1: 1.2})

    event = CalendarRatingAdapter().extract(trace, {artifact.key: artifact})

    assert event is not None
    assert event.metric_scores["excess_vps"] == {"agent_0": 0.1, "agent_1": 1.2}
