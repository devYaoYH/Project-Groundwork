"""Scientific V2 controls, privacy, route diversity, and persisted evidence."""

import json
from math import dist
from pathlib import Path

import pytest
from a2a_engine.compiler import compile as compile_design
from a2a_engine.content import ImagePart
from a2a_engine.design import Design
from a2a_engine.environment import load_release_declaration
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from map_director.declaration import BANK, BANK_V2, DECLARATION, DECLARATION_V1
from map_director.environment import MapDirectorConfig, MapDirectorEnvironment, resolve_config
from map_director.maps import local, resample, render_map, world
from map_director.maps_v2 import affine_residual, ordered_passages, score

TASKS = [task for item in BANK_V2.items for task in item.raw["tasks"]]


def route_for(task, fraction=0.5):
    points = {entry["label"]: entry["at"] for entry in task["follower_landmarks"]}
    route = [points["START cabin"]] + [[points[a][i] + fraction * (points[b][i] - points[a][i])
              for i in range(2)] for a, b in task["gate_pairs"]] + [points["FINISH chapel"]]
    return [local(point, task["follower_frame"]) for point in route]


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task["item_id"])
def test_jitter_has_independent_bounded_shifts_and_no_exact_affine_alignment(task):
    # Recreate geometry from explicit seeds without changing packaged font/image bytes.
    import runpy
    author = runpy.run_path(str(Path(__file__).parents[1] / "scripts/build_bank_v2.py"))
    for role in ("giver", "follower"):
        landmarks, shifts = author["shifted"](task["noise"][role + "_seed"], task["noise"]["radius"])
        for actual, expected in zip(landmarks, task[role + "_landmarks"]):
            assert actual["label"] == expected["label"]
            assert actual["at"] == pytest.approx(expected["at"], abs=1e-10)
        for actual, expected in zip(shifts, task["noise"][role + "_shifts"]):
            assert actual["label"] == expected["label"]
            assert actual["delta"] == pytest.approx(expected["delta"], abs=1e-10)
        assert all(0 < dist([0, 0], shift["delta"]) <= task["noise"]["radius"] for shift in shifts)
    assert task["noise"]["giver_seed"] != task["noise"]["follower_seed"]
    assert affine_residual(task["giver_landmarks"], task["follower_landmarks"]) > 1
    assert task["reference_route"] != task["target_route"]


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task["item_id"])
def test_distinct_between_landmark_routes_both_succeed(task):
    first, second = route_for(task, .4), route_for(task, .6)
    assert first != second
    for route in (first, second):
        outcome = score(route, task, submitted=True)
        assert outcome["success"] and outcome["gates_passed"] == 3
        assert outcome["oracle_version"] == "ordered-gates-corridor-v2"
        landmarks = [entry["at"] for entry in task["follower_landmarks"]]
        assert all(min(dist(world(point, task["follower_frame"]), landmark) for landmark in landmarks) > 2
                   for point in route[1:-1])
        assert all(dist(world(point, task["follower_frame"]), center) > 1 for point, center in zip(route[1:-1], task["reference_route"][1:-1]))
        assert score([local(point, task["follower_frame"]) for point in resample([world(p, task["follower_frame"]) for p in route], .7)],
                     task, submitted=True)["success"]


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task["item_id"])
def test_shortcuts_reversal_non_submission_and_missing_routes_fail(task):
    correct = route_for(task)
    for route, submitted in (([correct[0], correct[-1]], True), (list(reversed(correct)), True),
                             (correct, False), ([], True)):
        assert not score(route, task, submitted=submitted)["success"]
    # An excessive loop is rejected before the distance DP and never successful.
    long_route = [correct[0], *([correct[-1], correct[0]] * 30), correct[-1]]
    outcome = score(long_route, task, submitted=True)
    assert not outcome["success"] and outcome["route_error"] is None


def test_gate_membership_and_traversal_order_are_independent_checks():
    task = {"follower_frame": {"scale": 1, "offset": [0, 0]},
            "reference_route": [[0, 0], [0, 10], [0, 20]],
            "ordered_gates": [[[-5, 10], [5, 10]]],
            "tolerances": {"frechet": 8, "endpoint": 3, "length_ratio": 1.6, "gate_half_depth": 1.5}}
    missed = score([[0, 0], [8, 10], [0, 20]], task, submitted=True)
    assert missed["route_error"] <= 8 and missed["endpoint_error"] == 0
    assert not missed["success"] and missed["gates_passed"] == 0
    assert score([[0, 0], [2, 9], [2, 11], [0, 20]], task, submitted=True)["success"]
    # A passage can be crossed inside a segment or touched while turning.
    assert ordered_passages([[0, 0], [0, 20]], task["ordered_gates"], 1.5) == 1
    assert ordered_passages([[0, 0], [0, 9], [0, 0]], task["ordered_gates"], 1.5) == 1
    gates = [[[-22, 10], [-18, 10]], [[18, 20], [22, 20]]]
    wrong_order = [[-20, 0], [20, 20], [-20, 10], [20, 30]]
    assert ordered_passages(wrong_order, gates, 1.5) == 1


def test_version_selection_and_cross_bank_rejection():
    assert DECLARATION.version == "v2" and DECLARATION_V1.version == "v1"
    assert MapDirectorConfig().task_version == "v1"
    assert MapDirectorConfig(task_version="v2").item_id == "jitter_4_seed_17"
    assert resolve_config({"task_version": "v2"})["item_bank_sha256"] == BANK_V2.item_bank_sha256
    for version, digest in (("v1", BANK_V2.item_bank_sha256), ("v2", BANK.item_bank_sha256)):
        with pytest.raises(ValueError, match="digest mismatch"):
            resolve_config({"task_version": version, "item_bank_sha256": digest})
    with pytest.raises(ValueError, match="jitter_radius"):
        resolve_config({"task_version": "v2", "jitter_radius": 99})
    with pytest.raises(ValueError, match="noise_seed"):
        resolve_config({"task_version": "v2", "noise_seed": 99})
    v1 = load_release_declaration(Path(__file__).parents[1] / "environments/map_director_tiny_v1.yaml")
    v2 = load_release_declaration(Path(__file__).parents[1] / "environments/map_director_tiny_v2.yaml")
    assert v1.item_policy.item_bank_sha256 == BANK.item_bank_sha256
    assert v2.item_policy.item_bank_sha256 == BANK_V2.item_bank_sha256
    assert v2.parameters == DECLARATION.parameters and v2.measures == DECLARATION.measures
    matched = BANK.get("displaced_control_pair").raw["tasks"][1]
    assert affine_residual(matched["giver_landmarks"], matched["follower_landmarks"]) == 0


@pytest.mark.parametrize("item", BANK_V2.items, ids=lambda item: item.item_id)
def test_two_round_play_retains_seats_and_persists_exact_final_drawings(item, tmp_path):
    game = MapDirectorEnvironment({"task_version": "v2", "item_id": item.item_id, "asset_root": str(tmp_path / "runtime")}, dry_run=True)
    giver, follower = game.giver, game.follower
    trace = game.run()
    assert trace.metrics["success"] == [True, True]
    assert trace.metrics["gates_passed"] == [3, 3]
    assert [o["round"] for o in giver.observations] == [1, 2]
    assert [o["round"] for o in follower.observations] == [1, 2]
    assert giver.observations[0]["content"][1]["sha256"] != giver.observations[1]["content"][1]["sha256"]
    assert "single affine" in game.system_prompts["giver"]
    store = SQLiteEpisodeStore(path=tmp_path / "evidence" / "a2a.db")
    manifest = EpisodeManifest.from_run(config=trace.config.model_dump(), experiment_name="v2", cell_id=item.item_id,
                                       episode_idx=0, episode_uid=trace.episode_uid)
    store.put_episode(trace, manifest)
    loaded = store.get_episode(trace.episode_uid)
    assert loaded.config.item_bank_sha256 == BANK_V2.item_bank_sha256
    assert loaded.config.noise_seed == item.params["noise_seed"]
    finals = [event for event in loaded.events if event.type == "round_end"]
    assert len(finals) == 2
    for event, task in zip(finals, item.raw["tasks"]):
        assert event.data["speaker"] == "follower" and event.data["visibility"] == "private"
        image = ImagePart.model_validate(event.data["content"][1])
        assert (tmp_path / "evidence" / "assets" / image.sha256).read_bytes() == render_map(
            task["follower_landmarks"], task["follower_frame"],
            [world(point, task["follower_frame"]) for point in event.data["route"]], draft=True, direction_arrows=True)
    for observation in giver.observations + follower.observations:
        serialized = json.dumps({**observation, "content": [observation["content"][0]]})
        for forbidden in ("reference_route", "target_route", "ordered_gates", "gate_pairs", "giver_frame",
                          "follower_frame", "landmarks", "noise_seed", "tolerances", "oracle_result"):
            assert forbidden not in serialized


def test_compiled_v2_catalogue_selects_all_pinned_rows_and_rejects_v1_bank(tmp_path):
    design = Design.model_validate({
        "release": "map_director@v2",
        "parameters": {"discrepancy": {"pin": "independent_jitter"}, "jitter_radius": {"factor": [4, 8]},
                       "noise_seed": {"factor": [17, 41]}, "max_turns": {"pin": 3}},
        "units": {"episodes_per_cell": 1},
        "roster": [{"id": role, "role": role, "kind": "scripted", "binding": "baseline",
                    "capabilities": {"input": ["text", "image"], "evidence": "v2-fixture"}}
                   for role in ("giver", "follower")],
        "seed": {"mode": "derived", "root": 3},
    })
    plan = compile_design(design, DECLARATION, BANK_V2, experiment_id="v2", experiment_name="compiled_v2")
    assert plan.episodes_planned == 4
    for index, cell in enumerate(plan.cells):
        config = {**cell.episodes[0].config, "asset_root": str(tmp_path / str(index))}
        assert config["task_version"] == "v2"
        trace = MapDirectorEnvironment(config, dry_run=True).run()
        assert trace.metrics["success"] == [True, True]
