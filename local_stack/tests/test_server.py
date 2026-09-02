from __future__ import annotations

import json
import sys
from pathlib import Path

from a2a_engine.manifest import RunManifest
from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.schemas import AgentInfo, GameConfigBase, GameTraceBase
from a2a_engine.storage.sqlite import SQLiteTraceStore

# The local control plane is a runnable service, not a separately installed
# package. Add the repository root when pytest imports this test by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.server import LocalStackHandler, sse_frame
from local_stack.control_plane import ControlPlane


def test_sqlite_control_plane_lists_traces_and_rebuilds_calendar_ratings(tmp_path):
    config = GameConfigBase(
        game_name="calendar",
        num_agents=2,
        agents=[AgentInfo(model="model-a"), AgentInfo(model="model-b")],
        experiment_name="local-test",
        experiment_run_id="local-test.batch.0",
    )
    trace = GameTraceBase(
        game_id="calendar-run",
        config=config,
        metrics={
            "coordination_rate": 0.8,
            "per_agent_excess_burden": [1.0, 2.0],
        },
        final_state={},
    )
    manifest = RunManifest.from_run(
        config=config.model_dump(), experiment_name="local-test",
        batch_label="batch", run_idx=0, game_id=trace.game_id,
    )
    manifest.game_name = "calendar"
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    store.put_trace(trace, manifest)

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        assert LocalStackHandler._health()["trace_count"] == 1
        assert LocalStackHandler._trace_summaries()[0]["game_id"] == trace.game_id
        assert LocalStackHandler._trace(trace.game_id)["metrics"] == trace.metrics

        board = LocalStackHandler._calendar_leaderboard()
        assert board["metadata"]["rating_event_count"] == 1
        assert [row["player_id"] for row in board["leaderboard"]] == ["model-a", "model-b"]
    finally:
        LocalStackHandler.database = previous


def test_control_plane_correlates_local_otel_spans_by_trace_id(tmp_path):
    config = GameConfigBase(game_name="word_guess", num_agents=2)
    trace = GameTraceBase(
        game_id="otel-run",
        config=config,
        observability={"otel_trace_id": "a" * 32, "otel_root_span_id": "b" * 16},
    )
    manifest = RunManifest.from_run(
        config=config.model_dump(), experiment_name="otel-test",
        batch_label="batch", run_idx=0, game_id=trace.game_id,
    )
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    store.put_trace(trace, manifest)
    otel_file = tmp_path / "spans.jsonl"
    otel_file.write_text(
        json.dumps({"name": "other", "context": {"trace_id": "c" * 32}})
        + "\n"
        + json.dumps({
            "name": "game word_guess",
            "start_time": "2026-01-01T00:00:00Z",
            "context": {"trace_id": "a" * 32, "span_id": "b" * 16},
        })
        + "\n{incomplete"
    )

    previous_database = LocalStackHandler.database
    previous_otel_file = LocalStackHandler.otel_file
    try:
        LocalStackHandler.database = store.path
        LocalStackHandler.otel_file = otel_file
        payload = LocalStackHandler._observability(trace.game_id)
        assert payload is not None
        assert payload["observability"] == trace.observability
        assert payload["span_count"] == 1
        assert payload["spans"][0]["name"] == "game word_guess"
    finally:
        LocalStackHandler.database = previous_database
        LocalStackHandler.otel_file = previous_otel_file


def test_control_plane_exposes_derived_artifacts_without_mutating_the_trace(tmp_path):
    config = GameConfigBase(game_name="word_guess", num_agents=2)
    trace = GameTraceBase(game_id="artifact-run", config=config, metrics={"score": 1.0})
    manifest = RunManifest.from_run(
        config=config.model_dump(), experiment_name="artifact-test",
        batch_label="batch", run_idx=0, game_id=trace.game_id,
    )
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    store.put_trace(trace, manifest)
    store.put_derived_artifact(DerivedArtifact(
        game_id=trace.game_id, kind="derived_metrics.test", version="v1",
        trace_digest=trace_digest(trace), payload={"values": {"quality": 0.75}},
    ))

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        payload = LocalStackHandler._artifacts(trace.game_id)
        assert payload and payload["artifacts"][0]["payload"]["values"]["quality"] == 0.75
        assert LocalStackHandler._trace(trace.game_id)["metrics"] == {"score": 1.0}
    finally:
        LocalStackHandler.database = previous


def test_local_control_plane_registers_a_reviewed_experiment_and_tracks_attempts(tmp_path):
    """M1 records remain separate from traces and are launcher-independent."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    experiment = control.create_experiment(
        release_id="local-word_guess", yaml_path="games/word-guess/experiments/example.yaml",
    )

    class CompletingLauncher:
        def launch(self, rollout, _experiment, on_line, *, smoke_test=False):
            assert smoke_test is True
            control._mark_rollout_started(rollout.id)
            on_line(f"      OK   {experiment.name}.dog.0  [word_guess] -> sqlite:///dog")
            on_line(f"      OK   {experiment.name}.apple.0  [word_guess] -> sqlite:///apple")
            control._finish_rollout(rollout.id, 0)

        def cancel(self, _rollout_id):
            return False

    control.launcher = CompletingLauncher()
    rollout = control.launch_rollout(experiment.id, smoke_test=True)
    detail = control.rollout_detail(rollout.id)
    assert detail["rollout"]["status"] == "COMPLETED"
    assert {attempt["status"] for attempt in detail["episode_attempts"]} == {"COMPLETED"}
    assert {attempt["trace_uri"] for attempt in detail["episode_attempts"]} == {"sqlite:///dog", "sqlite:///apple"}
    assert all(attempt["redis_stream"].startswith(f"a2a:rollout:{rollout.id}:") for attempt in detail["episode_attempts"])


def _buyer_seller_control(tmp_path):
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    experiment = control.create_experiment(
        release_id="local-buyer_seller",
        yaml_path="games/buyer-seller/experiments/example.yaml",
    )
    return control, experiment


def test_smoke_rollout_plans_only_the_runs_the_runner_executes(tmp_path):
    """A smoke run covers one run per batch, so it must not queue batch.count."""
    control, experiment = _buyer_seller_control(tmp_path)

    class RecordingLauncher:
        def __init__(self):
            self.command_smoke = None

        def launch(self, rollout, _experiment, _on_line, *, smoke_test=False):
            self.command_smoke = smoke_test
            control._mark_rollout_started(rollout.id)

        def cancel(self, _rollout_id):
            return False

    control.launcher = RecordingLauncher()
    rollout = control.launch_rollout(experiment.id, smoke_test=True)
    attempts = control.rollout_detail(rollout.id)["episode_attempts"]

    # example.yaml declares 3 + 3 + 2 + 3 runs across four batches.
    assert len(attempts) == 4
    assert {attempt["run_idx"] for attempt in attempts} == {0}
    assert {attempt["batch_label"] for attempt in attempts} == {
        "wide_surplus", "narrow_surplus", "no_gains_control", "monotonic",
    }

    live = control.launch_rollout(experiment.id, smoke_test=False)
    assert len(control.rollout_detail(live.id)["episode_attempts"]) == 11


def test_rollout_does_not_complete_episodes_the_runner_never_reported(tmp_path):
    """A silent episode is an unreported gap, not a completed run."""
    control, experiment = _buyer_seller_control(tmp_path)

    class PartialLauncher:
        def launch(self, rollout, _experiment, on_line, *, smoke_test=False):
            control._mark_rollout_started(rollout.id)
            on_line(
                f"      OK   {experiment.name}.wide_surplus.0  [buyer_seller] -> sqlite:///wide"
            )
            control._finish_rollout(rollout.id, 0)

        def cancel(self, _rollout_id):
            return False

    control.launcher = PartialLauncher()
    rollout = control.launch_rollout(experiment.id, smoke_test=True)
    detail = control.rollout_detail(rollout.id)

    by_episode = {attempt["episode_id"]: attempt for attempt in detail["episode_attempts"]}
    reported = by_episode[f"{experiment.name}.wide_surplus.0"]
    assert reported["status"] == "COMPLETED"
    assert reported["trace_uri"] == "sqlite:///wide"

    silent = by_episode[f"{experiment.name}.monotonic.0"]
    assert silent["status"] == "UNREPORTED"
    assert silent["trace_uri"] is None


def test_failed_episodes_keep_their_own_runner_diagnostic(tmp_path):
    """A rollout-wide exit code cannot say which episode broke; the line can."""
    control, experiment = _buyer_seller_control(tmp_path)

    class FailingLauncher:
        def launch(self, rollout, _experiment, on_line, *, smoke_test=False):
            control._mark_rollout_started(rollout.id)
            on_line(
                f"      OK   {experiment.name}.wide_surplus.0  [buyer_seller] -> sqlite:///wide"
            )
            on_line(
                f"      FAIL {experiment.name}.monotonic.0  [buyer_seller]: ValueError: bad price"
            )
            on_line(
                f"2026-08-31 10:00:00,000 ERROR expt_runner: fail {experiment.name}.narrow_surplus.0: TimeoutError"
            )
            control._finish_rollout(rollout.id, 1)

        def cancel(self, _rollout_id):
            return False

    control.launcher = FailingLauncher()
    rollout = control.launch_rollout(experiment.id, smoke_test=True)
    detail = control.rollout_detail(rollout.id)

    assert detail["rollout"]["status"] == "FAILED"
    by_episode = {attempt["episode_id"]: attempt for attempt in detail["episode_attempts"]}
    assert by_episode[f"{experiment.name}.monotonic.0"]["error"] == "ValueError: bad price"
    assert by_episode[f"{experiment.name}.narrow_surplus.0"]["error"] == "TimeoutError"
    assert by_episode[f"{experiment.name}.wide_surplus.0"]["status"] == "COMPLETED"


def test_sse_frames_stay_unnamed_so_new_event_kinds_reach_existing_clients(tmp_path):
    """A named SSE frame only reaches a listener registered for that name."""
    control, experiment = _buyer_seller_control(tmp_path)

    class NoisyLauncher:
        def launch(self, rollout, _experiment, on_line, *, smoke_test=False):
            control._mark_rollout_started(rollout.id)
            on_line(f"      FAIL {experiment.name}.monotonic.0  [buyer_seller]: boom")
            control._finish_rollout(rollout.id, 0)

        def cancel(self, _rollout_id):
            return False

    control.launcher = NoisyLauncher()
    rollout = control.launch_rollout(experiment.id, smoke_test=True)

    events = control.events(rollout.id, after_id=0)
    kinds = {event["kind"] for event in events}
    # These kinds postdate the browser client, which is exactly the case a
    # name whitelist would have swallowed.
    assert {"episode.failed", "rollout.unreported_episodes"} <= kinds

    for event in events:
        frame = sse_frame(event)
        assert "\nevent:" not in frame and not frame.startswith("event:")
        body = [line for line in frame.splitlines() if line.startswith("data: ")]
        assert len(body) == 1
        # Every kind must be recoverable from the payload alone.
        assert json.loads(body[0].removeprefix("data: "))["kind"] == event["kind"]


def test_unknown_and_mismatched_releases_report_different_problems(tmp_path):
    """An absent release used to be reported as a game mismatch, which hides
    the far more common cause: the game never reached the registry."""
    import pytest

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )

    with pytest.raises(ValueError, match="no release registered as 'local-nonesuch'"):
        control.create_experiment(
            release_id="local-nonesuch",
            yaml_path="games/word-guess/experiments/example.yaml",
        )

    with pytest.raises(ValueError, match="is for game 'calendar'.*declares 'word_guess'"):
        control.create_experiment(
            release_id="local-calendar",
            yaml_path="games/word-guess/experiments/example.yaml",
        )


def test_every_release_offers_configs_it_can_actually_run(tmp_path):
    """The cold-start form pairs a release with a config, so on a fresh install
    the first submission must succeed rather than report a game mismatch."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    control.seed_installed_releases()

    by_game = control.available_experiments()
    releases = control.releases()
    assert releases, "expected the workspace games to register releases"

    for release in releases:
        paths = by_game.get(release.game_name) or []
        assert paths, f"release {release.id} offers no experiment configuration"
        # Registering the first offered config is exactly what the form does.
        experiment = control.create_experiment(
            release_id=release.id, yaml_path=paths[0], name=f"cold-start-{release.game_name}",
        )
        assert experiment.game_name == release.game_name


def test_worked_examples_are_offered_before_research_configs(tmp_path):
    """Calendar and Negotiation ship dozens of configs; a newcomer should land
    on the small credential-free one, not whichever sorts first."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    by_game = control.available_experiments()

    for game in ("calendar", "negotiation"):
        first = Path(by_game[game][0]).stem
        assert "smoke" in first or "example" in first, f"{game} leads with {first!r}"


def test_compose_passes_provider_credentials_to_both_services(tmp_path):
    """The viewer spawns the runner subprocess, so a live rollout launched from
    the browser needs the same credentials the runner service gets."""
    import yaml

    workspace = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((workspace / "docker-compose.yml").read_text())

    required = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"}
    for service in ("runner", "viewer"):
        environment = compose["services"][service]["environment"]
        missing = required - set(environment)
        assert not missing, f"{service} does not receive {sorted(missing)}"
        for key in required:
            # Defaulting to empty keeps the credential-free path working with
            # no .env file present.
            assert environment[key].endswith(":-}"), (
                f"{service}.{key} must default to empty, got {environment[key]!r}"
            )


def test_live_and_smoke_rollouts_plan_different_episode_counts(tmp_path):
    """The UI's run-mode toggle has to reach the plan, not just the CLI flag."""
    control, experiment = _buyer_seller_control(tmp_path)

    class NullLauncher:
        def launch(self, rollout, _experiment, _on_line, *, smoke_test=False):
            control._mark_rollout_started(rollout.id)

        def cancel(self, _rollout_id):
            return False

    control.launcher = NullLauncher()
    smoke = control.launch_rollout(experiment.id, smoke_test=True)
    live = control.launch_rollout(experiment.id, smoke_test=False)

    smoke_attempts = control.rollout_detail(smoke.id)["episode_attempts"]
    live_attempts = control.rollout_detail(live.id)["episode_attempts"]

    assert len(smoke_attempts) == 4    # one run per batch
    assert len(live_attempts) == 11    # each batch's declared count


def _fake_stream_events(entries):
    """Patch the Redis read so the projection endpoint is testable offline."""
    return {"stream": "s", "events": entries, "available": True}


def test_stream_trace_endpoint_projects_a_viewer_ready_document(monkeypatch):
    """Both games' viewers consume a trace document, so the endpoint has to
    emit one for a stream that has no persisted trace yet."""
    entries = [
        {"game_name": "calendar", "episode_id": "e.b.0", "stream_id": "1-0",
         "event": {"type": "game_start", "timestamp": "2026-08-31T12:00:00Z",
                   "data": {"num_agents": 5, "game_id": "cal-1"}}},
        {"game_name": "calendar", "episode_id": "e.b.0", "stream_id": "2-0",
         "event": {"type": "game_end", "timestamp": "2026-08-31T12:01:00Z",
                   "data": {"coordination_rate": 1.0}}},
    ]
    monkeypatch.setattr(LocalStackHandler, "_stream_events",
                        staticmethod(lambda stream: _fake_stream_events(entries)))

    payload, status = LocalStackHandler._stream_trace("s")

    assert status == 200
    assert payload["config"]["game_name"] == "calendar"
    assert len(payload["events"]) == 2
    assert payload["projection"]["partial"] is False
    assert payload["final_state"] == {"coordination_rate": 1.0}


def test_stream_trace_endpoint_marks_an_unfinished_episode_partial(monkeypatch):
    """A researcher watching a live rollout must not be shown an in-flight
    episode as though it had produced a result."""
    entries = [
        {"game_name": "negotiation", "episode_id": "e.b.0", "stream_id": "1-0",
         "event": {"type": "game_start", "timestamp": "2026-08-31T12:00:00Z", "data": {}}},
        {"game_name": "negotiation", "episode_id": "e.b.0", "stream_id": "2-0",
         "event": {"type": "cheap_talk", "timestamp": "2026-08-31T12:00:05Z",
                   "data": {"speaker": "agent_a", "message": "hi"}}},
    ]
    monkeypatch.setattr(LocalStackHandler, "_stream_events",
                        staticmethod(lambda stream: _fake_stream_events(entries)))

    payload, status = LocalStackHandler._stream_trace("s")

    assert status == 200
    assert payload["projection"]["partial"] is True
    assert payload["stopped"] is True
    assert payload["final_state"] == {}
    assert payload["ended_at"] is None


def test_stream_trace_endpoint_reports_unusable_streams(monkeypatch):
    monkeypatch.setattr(LocalStackHandler, "_stream_events",
                        staticmethod(lambda stream: {"available": False, "error": "no redis"}))
    assert LocalStackHandler._stream_trace("s")[1] == 503

    monkeypatch.setattr(LocalStackHandler, "_stream_events",
                        staticmethod(lambda stream: _fake_stream_events([])))
    assert LocalStackHandler._stream_trace("s")[1] == 404


def test_each_game_ships_the_viewer_its_replay_page_loads():
    """The replay pages drive each game's own vendored visualisation; a moved
    or missing asset would leave a blank page rather than an error."""
    workspace = Path(__file__).resolve().parents[2]

    assert (workspace / "games/calendar/tasks/viewer.html").is_file()
    assert (workspace / "games/negotiation/webapp/static/js/live.js").is_file()

    # The replay page imports this symbol; keeping it exported keeps replay and
    # the live view on one renderer.
    live = (workspace / "games/negotiation/webapp/static/js/live.js").read_text()
    assert "export function handleEvent(" in live

    for game in ("calendar", "negotiation"):
        assert (workspace / "games" / game / "replay" / "index.html").is_file()


def test_embedded_replay_hides_its_own_chrome_with_css_not_just_hidden():
    """The `hidden` attribute loses to the page's own display rules, which is
    how a chrome-free embed silently keeps its toolbar."""
    workspace = Path(__file__).resolve().parents[2]
    page = (workspace / "games/negotiation/replay/index.html").read_text()

    assert "body.embedded .replay-bar" in page, "embed mode must hide chrome via CSS"
    assert 'params.get("embed")' in page


def test_replay_shell_registers_a_viewer_for_every_served_game():
    """The shell picks a viewer from the projected game name; a game with no
    entry would render an empty stage rather than an error."""
    workspace = Path(__file__).resolve().parents[2]
    shell = (workspace / "a2a-viewer/js/replay.js").read_text()

    for game in ("calendar", "negotiation", "buyer_seller", "word_guess"):
        assert f"{game}:" in shell, f"replay shell has no viewer for {game}"


def test_experiment_config_endpoint_only_serves_offered_configurations(tmp_path):
    """Reviewing a config in the browser must not become a way to read any
    file in the workspace that happens to end in .yaml."""
    import pytest

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )

    payload = control.read_experiment_config("games/negotiation/experiments/smoke_local.yaml")
    assert payload["content"].startswith("name: negotiation_smoke")
    assert len(payload["sha256"]) == 64

    for denied in ("docker-compose.yml", "../../etc/passwd", "experiments/storage.yaml"):
        with pytest.raises(ValueError, match="not an offered experiment configuration"):
            control.read_experiment_config(denied)


def test_offered_configurations_are_rescanned_not_cached(tmp_path):
    """A config dropped into a game's experiments directory has to appear
    without restarting the control plane."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    added = workspace / "games/word-guess/experiments/_rescan_probe.yaml"
    added.write_text(
        "name: rescan_probe\n"
        "defaults: {game_name: word_guess, num_agents: 2, max_turns: 4}\n"
        "batches:\n  - label: probe\n    count: 1\n    config: {secret_word: kite, seed: 99}\n"
    )
    try:
        offered = control.available_experiments()["word_guess"]
        assert any(path.endswith("_rescan_probe.yaml") for path in offered)
    finally:
        added.unlink()

    assert not any(
        path.endswith("_rescan_probe.yaml")
        for path in control.available_experiments()["word_guess"]
    )


def test_control_ui_exposes_the_three_workflow_tabs_and_a_details_dialog():
    workspace = Path(__file__).resolve().parents[2]
    page = (workspace / "a2a-viewer/control.html").read_text()
    script = (workspace / "a2a-viewer/js/control.js").read_text()

    for panel in ("panel-configure", "panel-launch", "panel-rollouts"):
        assert f'id="{panel}"' in page
    assert "Registered environments" in page, "releases are presented as environments"
    assert "<dialog" in page

    # The trace viewer reads ?trace=<game_id>; ?game_id= would be a dead link.
    assert "/trace.html?trace=" in script
    # Rollout state advances in the runner, so the list cannot be a one-shot render.
    assert "schedulePoll" in script


def test_experiment_agents_reports_models_and_credential_state(tmp_path, monkeypatch):
    """A live rollout fails inside an HTTP client when a key is absent, so the
    line-up and its credential state have to be inspectable before launch."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    report = control.experiment_agents("games/word-guess/experiments/example.yaml")

    assert report["declares_agents"] is True
    assert report["ready_for_live"] is False
    assert report["missing_credentials"] == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    models = {agent["model"] for agent in report["agents"]}
    assert "gpt-4o-mini" in models
    assert {agent["provider"] for agent in report["agents"]} == {"openai", "anthropic"}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert control.experiment_agents("games/word-guess/experiments/example.yaml")["ready_for_live"] is True


def test_a_config_without_agents_reports_unknown_not_ready(tmp_path):
    """The game supplies its own defaults there, so claiming readiness would
    assert a credential state nothing has checked."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    report = control.experiment_agents("games/calendar/experiments/typed_local_smoke.yaml")

    assert report["declares_agents"] is False
    assert report["ready_for_live"] is None
    assert report["readiness_note"]


def test_agents_without_a_model_need_no_credential(tmp_path):
    """Heuristic and scripted agents call no provider."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(
        tmp_path / "control.db", workspace=workspace, trace_database=tmp_path / "traces.db",
    )
    report = control.experiment_agents("games/negotiation/experiments/smoke_local.yaml")

    assert report["ready_for_live"] is True
    assert all(agent["model"] is None for agent in report["agents"])
    assert all(agent["credential_env_var"] is None for agent in report["agents"])


def test_compose_mounts_the_workspace_so_host_edits_are_live():
    """Without this the workspace is only the copy baked into the image, and a
    config edited on the host silently does not appear until a rebuild."""
    import yaml

    workspace = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((workspace / "docker-compose.yml").read_text())

    for service in ("runner", "viewer"):
        volumes = compose["services"][service]["volumes"]
        assert "./:/workspace:ro" in volumes, f"{service} does not mount the workspace"
        assert "a2a-data:/data" in volumes


def test_launcher_keeps_run_artifacts_off_the_read_only_workspace(tmp_path):
    """A read-only mount makes the default ./results unwritable, so the runner
    has to be told where artifacts go."""
    control, experiment = _buyer_seller_control(tmp_path)

    captured = {}

    class CapturingLauncher:
        def launch(self, rollout, _experiment, _on_line, *, smoke_test=False):
            control._mark_rollout_started(rollout.id)

        def cancel(self, _rollout_id):
            return False

    from local_stack.control_plane import LocalLauncher

    launcher = LocalLauncher(control.workspace, control)
    original = __import__("subprocess").Popen

    def fake_popen(command, **kwargs):
        captured["command"] = command
        raise RuntimeError("stop before spawning")

    import subprocess
    subprocess.Popen = fake_popen
    try:
        control.launcher = CapturingLauncher()
        rollout = control.launch_rollout(experiment.id, smoke_test=True)
        try:
            launcher.launch(control.rollout(rollout.id), experiment, lambda line: None)
        except RuntimeError:
            pass
    finally:
        subprocess.Popen = original

    command = captured["command"]
    assert "--results-dir" in command
    results = command[command.index("--results-dir") + 1]
    assert results.endswith("/results")
    assert not results.startswith(str(control.workspace)), "artifacts must not land in the repo"
