import json
from pathlib import Path
import pytest
from a2a_engine.content import contains_image, provider_messages
from a2a_engine.items import ItemBank
from a2a_engine.workflow import export_program
from map_director.environment import MapDirectorEnvironment, MapDirectorConfig
from map_director.declaration import BANK, DECLARATION
from map_director.maps import local, score
from map_director.agents import MapDirectorAgent


@pytest.fixture
def game(tmp_path):
    return MapDirectorEnvironment(
        {"asset_root": str(tmp_path / "assets")}, dry_run=True
    )


def state(round_index=0):
    return {
        "round_index": round_index,
        "turn": 0,
        "route": [],
        "incoming": "hello",
        "submitted": False,
    }


def test_two_round_smoke_and_context(game):
    giver, follower = game.giver, game.follower
    trace = game.run()
    assert trace.metrics["success"] == [True, True]
    assert trace.metrics["route_error"] == [0.0, 0.0]
    assert game.giver.agent is giver and game.follower.agent is follower
    assert [x["round"] for x in giver.observations] == [1, 2]
    assert [x["round"] for x in follower.observations] == [1, 2]
    assert (
        trace.final_state["rounds"][0]["item_id"]
        != trace.final_state["rounds"][1]["item_id"]
    )
    assert all(
        "data" not in part
        for event in trace.events
        if event.type == "observation"
        for part in event.data["content"]
    )


def test_private_projection_native_images(game):
    s = state()
    g = game.observation(s, "giver")
    f = game.observation(s, "follower")
    assert contains_image(g["content"]) and contains_image(f["content"])
    assert g["content"][1]["sha256"] != f["content"][1]["sha256"]
    for observation in (g, f):
        text = (
            json.dumps({k: v for k, v in observation.items() if k != "content"})
            + " "
            + observation["content"][0]["text"]
        )
        for private in (
            "target_route",
            "giver_frame",
            "follower_frame",
            "landmarks",
            "tolerances",
            "missing_renamed",
            "oracle",
        ):
            assert private not in text
    assert "route" not in g
    converted = provider_messages([{"role": "user", "content": g["content"]}], "openai")
    assert converted[0]["content"][1]["type"] == "image_url"
    assert converted[0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_private_validation_feedback(game):
    s = {**state(), "validation_feedback": "Invalid syntax"}
    assert "validation_feedback" not in game.observation(s, "giver")
    assert game.observation(s, "follower")["validation_feedback"] == "Invalid syntax"


def test_overlay_is_private_and_persisted(game):
    s = {**state(), "route": [[25, 35], [25, 95], [85, 95], [85, 155], [145, 155]]}
    original = game.observation(state(), "follower")["content"][1]["sha256"]
    overlay = game.observation(s, "follower")["content"][1]["sha256"]
    assert overlay != original and overlay in game.assets
    assert (
        game.observation(s, "giver")["content"][1]["sha256"]
        == game.tasks[0]["giver_image"]["sha256"]
    )


def test_oracle_does_not_move_target_to_displaced_landmark():
    task = BANK.get("displaced_control_pair").raw["tasks"][0]
    exact = [local(p, task["follower_frame"]) for p in task["target_route"]]
    assert score(exact, task, submitted=True)["success"]
    wrong = [
        local(p, task["follower_frame"])
        for p in [[10, 10], [10, 50], [50, 50], [60, 80], [90, 90]]
    ]
    assert not score(wrong, task, submitted=True)["success"]


def test_oracle_order_submission_and_detour(game):
    task = game.tasks[0]
    exact = [local(p, task["follower_frame"]) for p in task["target_route"]]
    assert not score(exact, task, submitted=False)["success"]
    assert not score(exact[::-1], task, submitted=True)["success"]
    detour = exact[:2] + [exact[0]] + exact[1:]
    assert not score(detour, task, submitted=True)["success"]


@pytest.mark.parametrize(
    "route",
    [
        [[True, 35], [25, 95]],
        [[25, 35], [float("nan"), 95]],
        [[25, 35], [999, 95]],
        [[25, 35]],
        "bad",
    ],
)
def test_action_validation_preserves_draft(game, route):
    previous = [[25, 35], [25, 95]]
    result, submitted, feedback = game.validate_action(
        {"route": route, "submit": True}, {**state(), "route": previous}
    )
    assert result == previous and not submitted and feedback


def test_empty_submit_rejected(game):
    _, submitted, feedback = game.validate_action({"submit": True}, state())
    assert not submitted and feedback


def test_budget_and_no_correctness_feedback(game):
    class NoSubmit:
        name = "follower"

        async def act(self, observation, tools):
            return {"text": "Need more time", "submit": False}

    game.follower = NoSubmit()
    game.config.max_turns = 2
    trace = game.run()
    assert trace.metrics["success"] == [False, False]
    assert trace.metrics["turns_used"] == [2, 2]
    assert len([x for x in trace.events if x.type == "message"]) == 8


def test_model_history_retains_native_prior_round_images():
    class Client:
        pass

    agent = MapDirectorAgent(Client(), role="giver")
    image = BANK.get("discrepant_pair").raw["tasks"][0]["giver_image"]
    first = {"content": [{"type": "text", "text": "Round 1"}, image]}
    agent.build_messages(first, {})
    agent.parse_response('{"text":"route instructions"}', first, {})
    second = {"content": [{"type": "text", "text": "Round 2"}, image]}
    messages = agent.build_messages(second, {})
    assert len(messages) == 4
    assert messages[1]["content"][0]["text"] == "Round 1"
    assert messages[2]["role"] == "assistant"
    assert messages[3]["content"][0]["text"] == "Round 2"
    assert contains_image(messages[1]["content"]) and contains_image(
        messages[3]["content"]
    )


def test_export_is_pure_and_has_real_round_child(monkeypatch):
    monkeypatch.setattr(
        MapDirectorEnvironment,
        "__init__",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("construction during export")
        ),
    )
    document = export_program(MapDirectorEnvironment.build_program(), "map_director")
    assert document["status"] == "available"
    assert not document["workflow"]["diagnostics"]
    assert len(document["workflow"]["graphs"]) == 2


def test_bank_portability_and_tamper(tmp_path):
    packaged = BANK.package(tmp_path / "portable")
    assert packaged.item_bank_sha256 == BANK.item_bank_sha256
    asset = packaged.assets[0]
    path = packaged.asset_store.root / asset.sha256
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="digest mismatch"):
        ItemBank.load(packaged.path)


def test_declaration_parameters_are_real():
    for parameter in DECLARATION.parameters:
        assert parameter.name in MapDirectorConfig.model_fields
        if parameter.source == "item":
            BANK.attribute_levels(parameter.bank_key)
    assert DECLARATION.required_modalities == {"text", "image"}


def test_second_bank_session_scripted(tmp_path):
    trace = MapDirectorEnvironment(
        {"item_id": "displaced_control_pair", "asset_root": str(tmp_path / "assets")},
        dry_run=True,
    ).run()
    assert trace.metrics["success"] == [True, True]


def test_remote_staging_requires_exactly_one_action():
    from map_director.remote import stage_text, TextAction, MAP_TOOLS

    class Recorder:
        records = []

        def reject(self, code, text):
            from a2a_engine.remote.contract import ToolOutcome

            return ToolOutcome(status="rejected", resolves="immediate", code=code)

    recorder = Recorder()
    outcome, action = stage_text(
        MAP_TOOLS[0],
        TextAction(text="reply", route=[[25, 35], [25, 95]], submit=True),
        recorder,
    )
    assert outcome.status == "accepted" and outcome.resolves == "end_of_turn"
    assert action["submit"] and action["route"] == [[25.0, 35.0], [25.0, 95.0]]
    recorder.records = [{"action": action}]
    outcome, second = stage_text(MAP_TOOLS[0], TextAction(text="second"), recorder)
    assert outcome.status == "rejected" and second is None


@pytest.mark.asyncio
async def test_remote_strategy_persistent_registration_history():
    from types import SimpleNamespace
    from map_director.runtime_strategy import MapDirectorStrategy

    class FakeClient:
        capabilities = {"input": ["text", "image"], "evidence": "test-image-contract"}

        def __init__(self):
            self.messages = []

        def oneshot(self, messages):
            self.messages.append(messages)
            return '{"text":"new route"}'

    client = FakeClient()
    strategy = MapDirectorStrategy(client=client)
    await strategy.calls(
        SimpleNamespace(kind="register", observation={"role": "giver"})
    )
    image = BANK.get("discrepant_pair").raw["tasks"][0]["giver_image"]
    for round_no in [1, 2]:
        calls = await strategy.calls(
            SimpleNamespace(
                kind="turn",
                observation={
                    "round": round_no,
                    "content": [{"type": "text", "text": f"Round {round_no}"}, image],
                },
            )
        )
        assert calls == [("env", "submit_action", {"text": "new route"})]
    assert len(client.messages[1]) == 4
    assert client.messages[1][1]["content"][0]["text"] == "Round 1"
    assert contains_image(client.messages[1][3]["content"])


def test_compiler_selects_and_runs_each_pinned_bank_row(tmp_path):
    from a2a_engine.compiler import compile as compile_design
    from a2a_engine.design import Design

    design = Design.model_validate(
        {
            "release": "map_director@v1",
            "parameters": {
                "discrepancy": {
                    "factor": [item.params["discrepancy"] for item in BANK.items]
                },
                "max_turns": {"pin": 3},
            },
            "units": {"episodes_per_cell": 1},
            "roster": [
                {
                    "id": role,
                    "role": role,
                    "kind": "scripted",
                    "binding": "baseline",
                    "capabilities": {
                        "input": ["text", "image"],
                        "evidence": "map-script-v1",
                    },
                }
                for role in ("giver", "follower")
            ],
            "seed": {"mode": "derived", "root": 9},
        }
    )
    plan = compile_design(
        design, DECLARATION, BANK, experiment_id="test", experiment_name="compiled_maps"
    )
    assert plan.episodes_planned == 2
    observed = []
    for index, cell in enumerate(plan.cells):
        config = cell.episodes[0].config
        observed.append(config["item_id"])
        config["asset_root"] = str(tmp_path / str(index))
        trace = MapDirectorEnvironment(config, dry_run=True).run()
        assert trace.metrics["success"] == [True, True]
    assert observed == [item.item_id for item in BANK.items]


def test_digest_mismatch_is_not_silently_replaced():
    from map_director.environment import resolve_config

    with pytest.raises(ValueError, match="digest mismatch"):
        resolve_config({"item_bank_sha256": "0" * 64})


def test_typed_release_matches_installed_declaration():
    from a2a_engine.environment import load_release_declaration

    release = load_release_declaration(
        Path(__file__).parents[1] / "environments/map_director_tiny_v1.yaml"
    )
    assert release.parameters == DECLARATION.parameters
    assert release.roles == DECLARATION.roles
    assert release.required_modalities == DECLARATION.required_modalities
    assert release.item_policy.item_bank_sha256 == BANK.item_bank_sha256


@pytest.mark.parametrize("compiled", [False, True])
def test_default_visual_assets_survive_read_only_workspace_and_persistence(tmp_path, monkeypatch, compiled):
    import shutil
    from a2a_engine.episode_media import episode_images, read_episode_image
    from a2a_engine.manifest import EpisodeManifest
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore

    workspace = tmp_path / "readonly-workspace"
    workspace.mkdir(mode=0o555)
    monkeypatch.chdir(workspace)
    config = {"asset_root": str(BANK.asset_store.root)} if compiled else {}
    environment = MapDirectorEnvironment(config, dry_run=True)
    try:
        trace = environment.run()
        assert trace.metrics["success"] == [True, True]
        assert list(workspace.iterdir()) == []
        database = tmp_path / "sink" / "trace.db"
        store = SQLiteEpisodeStore(path=database)
        manifest = EpisodeManifest.from_run(config=trace.config.model_dump(), experiment_name="readonly",
            cell_id="map", episode_idx=0, episode_uid=trace.episode_uid)
        store.put_episode(trace, manifest)
        for image in episode_images(trace):
            payload, mime = read_episode_image(trace, image.sha256, database.parent / "assets")
            assert payload and mime == "image/png"
    finally:
        shutil.rmtree(environment.config.asset_root)
        workspace.chmod(0o755)
