"""Committed drawings remain inspectable even without another model turn."""

from a2a_engine.content import ImagePart
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from map_director.environment import MapDirectorEnvironment
from map_director.maps import local, render_map, world


def make_game(tmp_path, **config):
    return MapDirectorEnvironment(
        {"asset_root": str(tmp_path / "runtime-assets"), **config}, dry_run=True
    )


def image_of(event):
    image = event.data["content"][1]
    assert event.data["speaker"] == "follower"
    assert event.data["visibility"] == "private"
    assert event.data["text"]
    assert image["type"] == "image" and "data" not in image
    return image


def assert_image_matches(game, event):
    image = image_of(event)
    task = game.tasks[event.data["round"] - 1]
    assert game.store.read(ImagePart.model_validate(image)) == render_map(
        task["follower_landmarks"],
        task["follower_frame"],
        [world(point, task["follower_frame"]) for point in event.data["route"]],
        draft=True,
    )
    assert game.assets[image["sha256"]] == image
    return image


def test_first_turn_submission_has_final_private_image_and_persisted_bytes(tmp_path):
    game = make_game(tmp_path)
    giver, follower = game.giver, game.follower
    trace = game.run()
    drawings = [e for e in trace.events if e.type == "drawing"]
    finals = [e for e in trace.events if e.type == "round_end"]
    assert trace.metrics["turns_used"] == [1, 1]
    assert len(follower.observations) == 2
    for drawing, final, task in zip(drawings, finals, game.tasks):
        image = assert_image_matches(game, drawing)
        assert assert_image_matches(game, final) == image
        assert final.data["route"] == [
            local(point, task["follower_frame"]) for point in task["target_route"]
        ]
        assert final.data["submitted"] and final.data["success"]
        assert "Success" in final.data["text"]
        assert image in trace.config.image_assets
    assert [o["content"][1]["sha256"] for o in giver.observations] == [
        task["giver_image"]["sha256"] for task in game.tasks
    ]
    assert all("route" not in observation for observation in giver.observations)

    store = SQLiteEpisodeStore(path=tmp_path / "saved" / "a2a.db")
    manifest = EpisodeManifest.from_run(
        config=trace.config.model_dump(),
        experiment_name="drawing_replay",
        cell_id="baseline",
        episode_idx=0,
        episode_uid=trace.episode_uid,
    )
    store.put_episode(trace, manifest)
    loaded = store.get_episode(trace.episode_uid)
    assert loaded is not None
    for final in [e for e in loaded.events if e.type == "round_end"]:
        image = image_of(final)
        assert (tmp_path / "saved" / "assets" / image["sha256"]).read_bytes() == game.store.read(ImagePart.model_validate(image))


def test_revision_and_invalid_action_replay_only_committed_route(tmp_path):
    game = make_game(tmp_path, max_turns=3)

    class RevisingFollower:
        name = "follower"

        def __init__(self):
            self.observations = []

        async def act(self, observation, tools):
            self.observations.append(observation)
            task = game.tasks[observation["round"] - 1]
            exact = [local(point, task["follower_frame"]) for point in task["target_route"]]
            if observation["turn"] == 1:
                return {"text": "Drafting", "route": exact[:2], "submit": False}
            if observation["turn"] == 2:
                return {"text": "Bad revision", "route": [[999, 999]], "submit": True}
            return {"text": "Revised route", "route": exact, "submit": True}

    giver = game.giver
    follower = RevisingFollower()
    game.follower = follower
    trace = game.run()
    assert trace.metrics["success"] == [True, True]
    drawings = [e for e in trace.events if e.type == "drawing"]
    for start in (0, 3):
        first, rejected, revised = drawings[start : start + 3]
        assert_image_matches(game, first)
        assert image_of(first) == assert_image_matches(game, rejected)
        assert first.data["route"] == rejected.data["route"]
        assert rejected.data["validation"] and not rejected.data["submitted"]
        assert "Drawing rejected" in rejected.data["text"]
        assert image_of(revised) != image_of(first)
        assert revised.data["submitted"]
        assert_image_matches(game, revised)
        assert follower.observations[start + 2]["content"][1]["sha256"] == image_of(rejected)["sha256"]
    assert all(
        observation["content"][1]["sha256"] == game.tasks[observation["round"] - 1]["giver_image"]["sha256"]
        and "validation_feedback" not in observation
        for observation in giver.observations
    )


def test_incomplete_submission_has_visible_final_empty_map(tmp_path):
    game = make_game(tmp_path, max_turns=1)

    class EmptyFollower:
        name = "follower"

        async def act(self, observation, tools):
            return {"text": "Submitting without a drawing", "submit": True}

    game.follower = EmptyFollower()
    trace = game.run()
    assert trace.metrics["success"] == [False, False]
    for drawing, final in zip(
        [e for e in trace.events if e.type == "drawing"],
        [e for e in trace.events if e.type == "round_end"],
    ):
        assert drawing.data["validation"]
        assert final.data["route"] == [] and not final.data["submitted"]
        assert assert_image_matches(game, drawing) == assert_image_matches(game, final)
        assert "unsubmitted, 0 points" in final.data["text"]


def test_budget_exhaustion_shows_last_unsubmitted_draft(tmp_path):
    game = make_game(tmp_path, max_turns=1)

    class DraftFollower:
        name = "follower"

        async def act(self, observation, tools):
            task = game.tasks[observation["round"] - 1]
            return {
                "text": "Drawing the first segment",
                "route": [
                    local(point, task["follower_frame"])
                    for point in task["target_route"][:2]
                ],
                "submit": False,
            }

    game.follower = DraftFollower()
    trace = game.run()
    assert trace.metrics["success"] == [False, False]
    for final in [e for e in trace.events if e.type == "round_end"]:
        assert len(final.data["route"]) == 2
        assert not final.data["submitted"] and "unsubmitted, 2 points" in final.data["text"]
        assert_image_matches(game, final)
