from __future__ import annotations

import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.provenance import build_provenance
from a2a_engine.schemas import Event, ParticipantBinding, EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore

# The local control plane is a runnable service, not a separately installed
# package. Add the repository root when pytest imports this test by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.server import LocalStackHandler, sse_frame
from local_stack.control_plane import ControlPlane


def _request(server: ThreadingHTTPServer, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1])
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_local_stack_accepts_head_for_static_and_api_routes(tmp_path):
    static_dir = tmp_path / "static"
    environment_dir = static_dir / "environments"
    environment_dir.mkdir(parents=True)
    page = b"<!doctype html><title>Environment catalog</title>"
    (environment_dir / "index.html").write_bytes(page)

    class TestHandler(LocalStackHandler):
        pass

    TestHandler.database = tmp_path / "a2a.db"
    TestHandler.static_dir = static_dir
    TestHandler.workspace = tmp_path
    TestHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        status, headers, body = _request(server, "HEAD", "/environments/")
        assert status == 200
        assert headers["Content-Type"] == "text/html"
        assert headers["Content-Length"] == str(len(page))
        assert body == b""

        status, headers, body = _request(server, "HEAD", "/api/health")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert int(headers["Content-Length"]) > 0
        assert body == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_environment_catalog_migrates_a_legacy_control_database(tmp_path):
    workspace = Path(__file__).resolve().parents[2]
    database = tmp_path / "a2a.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE releases (
                id TEXT PRIMARY KEY, environment_id TEXT NOT NULL UNIQUE,
                package TEXT, source_ref TEXT NOT NULL, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES releases(id),
                yaml_path TEXT NOT NULL, config_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        db.execute(
            "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?)",
            ("calendar", "calendar", "calendar_environment", "local-workspace", "{}", "now"),
        )
        db.execute(
            "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-calendar", "legacy", "calendar", "calendar.yaml", "digest", "now"),
        )

    class TestHandler(LocalStackHandler):
        pass

    TestHandler.database = database
    TestHandler.static_dir = tmp_path / "static"
    TestHandler.workspace = workspace
    TestHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        ready = threading.Barrier(3)

        def request(path: str) -> tuple[int, dict[str, str], bytes]:
            ready.wait()
            return _request(server, "GET", path)

        with ThreadPoolExecutor(max_workers=2) as executor:
            catalog_future = executor.submit(request, "/api/environments")
            concurrent_catalog_future = executor.submit(request, "/api/environments")
            ready.wait()
            status, _headers, body = catalog_future.result()
            concurrent_status, _concurrent_headers, concurrent_body = concurrent_catalog_future.result()

        assert status == 200
        catalog = json.loads(body)
        calendar = next(
            environment for environment in catalog["environments"]
            if environment["environment_id"] == "calendar"
        )
        assert calendar["experiment_count"] == 1
        assert concurrent_status == 200
        assert json.loads(concurrent_body)["environments"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    with sqlite3.connect(database) as db:
        environment_id = db.execute(
            "SELECT environment_id FROM experiments WHERE id = ?", ("legacy-calendar",)
        ).fetchone()[0]
    assert environment_id == "calendar"


def test_sqlite_control_plane_lists_traces_and_rebuilds_calendar_ratings(tmp_path):
    config = EpisodeConfigBase(
        environment_id="calendar",
        num_agents=2,
        agents=[ParticipantBinding(model="model-a"), ParticipantBinding(model="model-b")],
        experiment_name="local-test",
        episode_id="local-test.cell.0",
    )
    trace = EpisodeTrace(
        episode_uid="calendar-run",
        config=config,
        metrics={
            "coordination_rate": 0.8,
            "per_agent_excess_burden": [1.0, 2.0],
        },
        final_state={},
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="local-test",
        cell_id="cell", episode_idx=0, episode_uid=trace.episode_uid,
    )
    manifest.environment_id = "calendar"
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, manifest)

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        assert LocalStackHandler._health()["episode_count"] == 1
        page = LocalStackHandler._episode_page({})
        assert page["episodes"][0]["episode_uid"] == trace.episode_uid
        assert page["next_cursor"] is None
        assert LocalStackHandler._trace(trace.episode_uid)["metrics"] == trace.metrics

        board = LocalStackHandler._calendar_leaderboard()
        assert board["metadata"]["rating_event_count"] == 1
        assert [row["player_id"] for row in board["leaderboard"]] == ["model-a", "model-b"]
    finally:
        LocalStackHandler.database = previous


def test_control_plane_correlates_local_otel_spans_by_trace_id(tmp_path):
    config = EpisodeConfigBase(environment_id="word_guess", num_agents=2)
    trace = EpisodeTrace(
        episode_uid="otel-run",
        config=config,
        observability={"otel_trace_id": "a" * 32, "otel_root_span_id": "b" * 16},
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="otel-test",
        cell_id="cell", episode_idx=0, episode_uid=trace.episode_uid,
    )
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, manifest)
    otel_file = tmp_path / "spans.jsonl"
    otel_file.write_text(
        json.dumps({"name": "other", "context": {"trace_id": "c" * 32}})
        + "\n"
        + json.dumps({
            "name": "environment word_guess",
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
        payload = LocalStackHandler._observability(trace.episode_uid)
        assert payload is not None
        assert payload["observability"] == trace.observability
        assert payload["span_count"] == 1
        assert payload["spans"][0]["name"] == "environment word_guess"
    finally:
        LocalStackHandler.database = previous_database
        LocalStackHandler.otel_file = previous_otel_file


def test_control_plane_exposes_derived_artifacts_without_mutating_the_trace(tmp_path):
    config = EpisodeConfigBase(environment_id="word_guess", num_agents=2)
    trace = EpisodeTrace(episode_uid="artifact-run", config=config, metrics={"score": 1.0})
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="artifact-test",
        cell_id="cell", episode_idx=0, episode_uid=trace.episode_uid,
    )
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, manifest)
    store.put_derived_artifact(DerivedArtifact(
        episode_uid=trace.episode_uid, kind="derived_metrics.test", version="v1",
        trace_digest=trace_digest(trace), payload={"values": {"quality": 0.75}},
    ))

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        payload = LocalStackHandler._artifacts(trace.episode_uid)
        assert payload and payload["artifacts"][0]["payload"]["values"]["quality"] == 0.75
        assert LocalStackHandler._trace(trace.episode_uid)["metrics"] == {"score": 1.0}
    finally:
        LocalStackHandler.database = previous


def test_local_control_plane_registers_a_reviewed_experiment_and_tracks_attempts(tmp_path):
    """M1 records remain separate from episodes and are launcher-independent."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    experiment = control.create_experiment(
        release_id="word_guess", yaml_path="games/word-guess/experiments/example.yaml",
    )

    class CompletingLauncher:
        def launch(self, launch, _experiment, on_line, *, smoke_test=False):
            assert smoke_test is True
            control._mark_launch_started(launch.id)
            on_line(f"      OK   {experiment.name}.dog.0  [word_guess] -> sqlite:///dog")
            on_line(f"      OK   {experiment.name}.apple.0  [word_guess] -> sqlite:///apple")
            control._finish_launch(launch.id, 0)

        def cancel(self, _launch_id):
            return False

    control.launcher = CompletingLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    detail = control.launch_detail(launch.id)
    assert detail["launch"]["status"] == "COMPLETED"
    # Runner output is retained for diagnostics, but it is not evidence that a
    # trace committed. The terminal identity join correctly leaves these fake
    # runner-only reports visible as gaps.
    assert {attempt["status"] for attempt in detail["attempts"]} == {"UNREPORTED"}
    assert {attempt["episode_uri"] for attempt in detail["attempts"]} == {"sqlite:///dog", "sqlite:///apple"}
    assert all(attempt["episode_uid"] is None for attempt in detail["attempts"])
    assert [event["payload"]["line"] for event in detail["runner_logs"]] == [
        f"      OK   {experiment.name}.dog.0  [word_guess] -> sqlite:///dog",
        f"      OK   {experiment.name}.apple.0  [word_guess] -> sqlite:///apple",
    ]
    assert all(attempt["redis_stream"].startswith(f"a2a:launch:{launch.id}:") for attempt in detail["attempts"])


def test_experiment_detail_response_includes_empty_locked_cell_evidence(tmp_path):
    """The read model accompanies the immutable cell plan before any run exists."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    design = """schema_version: 1
release: buyer_seller@v1
parameters:
  seller_cost: {randomize: true}
  buyer_value: {pin: 30}
  num_items: {pin: 3}
  discount_factor: {pin: 0.5}
units:
  episodes_per_cell: 1
roster:
  - id: seller
    kind: scripted
    binding: seller-baseline
  - id: buyer
    kind: scripted
    binding: buyer-baseline
seed:
  root: 41
"""
    experiment = control.create_experiment(
        name="Evidence response", release_id="buyer_seller", design_text=design,
    )
    locked = control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    payload = control.experiment_detail(locked.id)

    assert payload["cell_evidence"] == [{
        "cell_id": payload["cells"][0]["cell_id"],
        "levels": payload["cells"][0]["levels"],
        "planned_replicas": 1,
        "completed_replicas": 0,
        "status_counts": {"NOT_STARTED": 1},
        "metric_summaries": [],
    }]


def _buyer_seller_control(tmp_path):
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    experiment = control.create_experiment(
        release_id="buyer_seller",
        yaml_path="games/buyer-seller/experiments/example.yaml",
    )
    return control, experiment


def test_smoke_launch_plans_only_the_runs_the_runner_executes(tmp_path):
    """A smoke run covers one run per cell, so it must not queue cell.count."""
    control, experiment = _buyer_seller_control(tmp_path)

    class RecordingLauncher:
        def __init__(self):
            self.command_smoke = None

        def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
            self.command_smoke = smoke_test
            control._mark_launch_started(launch.id)

        def cancel(self, _launch_id):
            return False

    control.launcher = RecordingLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    attempts = control.launch_detail(launch.id)["attempts"]

    # example.yaml declares 3 + 3 + 2 + 3 runs across four cells.
    assert len(attempts) == 4
    assert {attempt["episode_idx"] for attempt in attempts} == {0}
    assert {attempt["cell_id"] for attempt in attempts} == {
        "wide_surplus", "narrow_surplus", "no_gains_control", "monotonic",
    }

    live = control.launch_experiment(experiment.id, smoke_test=False)
    assert len(control.launch_detail(live.id)["attempts"]) == 11


def test_launch_does_not_complete_episodes_without_persisted_trace(tmp_path):
    """A stdout report is provenance, not proof that the trace committed."""
    control, experiment = _buyer_seller_control(tmp_path)

    class PartialLauncher:
        def launch(self, launch, _experiment, on_line, *, smoke_test=False):
            control._mark_launch_started(launch.id)
            on_line(
                f"      OK   {experiment.name}.wide_surplus.0  [buyer_seller] -> sqlite:///wide"
            )
            control._finish_launch(launch.id, 0)

        def cancel(self, _launch_id):
            return False

    control.launcher = PartialLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    detail = control.launch_detail(launch.id)

    by_episode = {attempt["episode_id"]: attempt for attempt in detail["attempts"]}
    reported = by_episode[f"{experiment.name}.wide_surplus.0"]
    assert reported["status"] == "UNREPORTED"
    assert reported["episode_uri"] == "sqlite:///wide"

    silent = by_episode[f"{experiment.name}.monotonic.0"]
    assert silent["status"] == "UNREPORTED"
    assert silent["episode_uri"] is None


def test_failed_episodes_keep_their_own_runner_diagnostic(tmp_path):
    """A launch-wide exit code cannot say which episode broke; the line can."""
    control, experiment = _buyer_seller_control(tmp_path)

    class FailingLauncher:
        def launch(self, launch, _experiment, on_line, *, smoke_test=False):
            control._mark_launch_started(launch.id)
            on_line(
                f"      OK   {experiment.name}.wide_surplus.0  [buyer_seller] -> sqlite:///wide"
            )
            on_line(
                f"      FAIL {experiment.name}.monotonic.0  [buyer_seller]: ValueError: bad price"
            )
            on_line(
                f"2026-08-31 10:00:00,000 ERROR expt_runner: fail {experiment.name}.narrow_surplus.0: TimeoutError"
            )
            control._finish_launch(launch.id, 1)

        def cancel(self, _launch_id):
            return False

    control.launcher = FailingLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    detail = control.launch_detail(launch.id)

    assert detail["launch"]["status"] == "FAILED"
    by_episode = {attempt["episode_id"]: attempt for attempt in detail["attempts"]}
    assert by_episode[f"{experiment.name}.monotonic.0"]["error"] == "ValueError: bad price"
    assert by_episode[f"{experiment.name}.narrow_surplus.0"]["error"] == "TimeoutError"
    assert by_episode[f"{experiment.name}.wide_surplus.0"]["status"] == "FAILED"


def test_sse_frames_stay_unnamed_so_new_event_kinds_reach_existing_clients(tmp_path):
    """A named SSE frame only reaches a listener registered for that name."""
    control, experiment = _buyer_seller_control(tmp_path)

    class NoisyLauncher:
        def launch(self, launch, _experiment, on_line, *, smoke_test=False):
            control._mark_launch_started(launch.id)
            on_line(f"      FAIL {experiment.name}.monotonic.0  [buyer_seller]: boom")
            control._finish_launch(launch.id, 0)

        def cancel(self, _launch_id):
            return False

    control.launcher = NoisyLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)

    events = control.events(launch.id, after_id=0)
    kinds = {event["kind"] for event in events}
    # These kinds postdate the browser client, which is exactly the case a
    # name whitelist would have swallowed.
    assert {"episode.failed", "launch.unreported_episodes"} <= kinds

    for event in events:
        frame = sse_frame(event)
        assert "\nevent:" not in frame and not frame.startswith("event:")
        body = [line for line in frame.splitlines() if line.startswith("data: ")]
        assert len(body) == 1
        # Every kind must be recoverable from the payload alone.
        assert json.loads(body[0].removeprefix("data: "))["kind"] == event["kind"]


def test_unknown_and_mismatched_releases_report_different_problems(tmp_path):
    """An absent release used to be reported as a environment mismatch, which hides
    the far more common cause: the environment never reached the registry."""
    import pytest

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)

    with pytest.raises(ValueError, match="no release registered as 'nonesuch'"):
        control.create_experiment(
            release_id="nonesuch",
            yaml_path="games/word-guess/experiments/example.yaml",
        )

    with pytest.raises(ValueError, match="is for environment 'calendar'.*declares 'word_guess'"):
        control.create_experiment(
            release_id="calendar",
            yaml_path="games/word-guess/experiments/example.yaml",
        )


def test_every_release_offers_configs_it_can_actually_run(tmp_path):
    """The cold-start form pairs a release with a config, so on a fresh install
    the first submission must succeed rather than report a environment mismatch."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    control.seed_installed_releases()

    by_game = control.available_experiments()
    releases = control.releases()
    assert releases, "expected the workspace games to register releases"

    for release in releases:
        paths = by_game.get(release.environment_id) or []
        assert paths, f"release {release.id} offers no experiment configuration"
        # Registering the first offered config is exactly what the form does.
        experiment = control.create_experiment(
            release_id=release.id, yaml_path=paths[0], name=f"cold-start-{release.environment_id}",
        )
        assert experiment.environment_id == release.environment_id


def test_environment_surface_exposes_declarations_items_and_oracle_results(tmp_path):
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)

    environments = control.environment_summaries()
    assert {environment["environment_id"] for environment in environments} == {
        "buyer_seller", "calendar", "negotiation", "word_guess",
    }

    detail = control.environment_detail("calendar")
    assert detail["parameters"]
    assert detail["measures"]
    assert "item_policy:" in detail["declaration_yaml"]

    items = control.environment_items("calendar", limit=1)
    assert len(items["items"]) == 1
    assert items["item_bank_sha256"] == detail["release"]["item_bank_sha256"]

    result = control.run_oracle("calendar", items["items"][0]["item_id"])
    assert result["oracle_version"] == "cp-sat-v1"
    assert result["result"] == items["items"][0]["oracle_result"]


def test_worked_examples_are_offered_before_research_configs(tmp_path):
    """Calendar and Negotiation ship dozens of configs; a newcomer should land
    on the small credential-free one, not whichever sorts first."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    by_game = control.available_experiments()

    for environment in ("calendar", "negotiation"):
        first = Path(by_game[environment][0]).stem
        assert "smoke" in first or "example" in first, f"{environment} leads with {first!r}"


def test_compose_passes_provider_credentials_to_both_services(tmp_path):
    """The viewer spawns the runner subprocess, so a live launch launched from
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


def test_live_and_smoke_launches_plan_different_episode_counts(tmp_path):
    """The UI's run-mode toggle has to reach the plan, not just the CLI flag."""
    control, experiment = _buyer_seller_control(tmp_path)

    class NullLauncher:
        def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
            control._mark_launch_started(launch.id)

        def cancel(self, _launch_id):
            return False

    control.launcher = NullLauncher()
    smoke = control.launch_experiment(experiment.id, smoke_test=True)
    live = control.launch_experiment(experiment.id, smoke_test=False)

    smoke_attempts = control.launch_detail(smoke.id)["attempts"]
    live_attempts = control.launch_detail(live.id)["attempts"]

    assert len(smoke_attempts) == 4    # one run per cell
    assert len(live_attempts) == 11    # each cell's declared count


def _fake_stream_events(entries):
    """Patch the Redis read so the projection endpoint is testable offline."""
    return {"stream": "s", "events": entries, "available": True}


def test_stream_trace_endpoint_projects_a_viewer_ready_document(monkeypatch):
    """Both games' viewers consume a trace document, so the endpoint has to
    emit one for a stream that has no persisted trace yet."""
    entries = [
        {"environment_id": "calendar", "episode_id": "e.b.0", "stream_id": "1-0",
         "event": {"type": "game_start", "timestamp": "2026-08-31T12:00:00Z",
                   "data": {"num_agents": 5, "episode_uid": "cal-1"}}},
        {"environment_id": "calendar", "episode_id": "e.b.0", "stream_id": "2-0",
         "event": {"type": "game_end", "timestamp": "2026-08-31T12:01:00Z",
                   "data": {"coordination_rate": 1.0}}},
    ]
    monkeypatch.setattr(LocalStackHandler, "_stream_events",
                        staticmethod(lambda stream: _fake_stream_events(entries)))

    payload, status = LocalStackHandler._stream_trace("s")

    assert status == 200
    assert payload["config"]["environment_id"] == "calendar"
    assert len(payload["events"]) == 2
    assert payload["projection"]["partial"] is False
    assert payload["final_state"] == {"coordination_rate": 1.0}


def test_stream_trace_endpoint_marks_an_unfinished_episode_partial(monkeypatch):
    """A researcher watching a live launch must not be shown an in-flight
    episode as though it had produced a result."""
    entries = [
        {"environment_id": "negotiation", "episode_id": "e.b.0", "stream_id": "1-0",
         "event": {"type": "game_start", "timestamp": "2026-08-31T12:00:00Z", "data": {}}},
        {"environment_id": "negotiation", "episode_id": "e.b.0", "stream_id": "2-0",
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
    """The replay pages drive each environment's own vendored visualisation; a moved
    or missing asset would leave a blank page rather than an error."""
    workspace = Path(__file__).resolve().parents[2]

    assert (workspace / "games/calendar/tasks/viewer.html").is_file()
    assert (workspace / "games/negotiation/webapp/static/js/live.js").is_file()

    # The replay page imports this symbol; keeping it exported keeps replay and
    # the live view on one renderer.
    live = (workspace / "games/negotiation/webapp/static/js/live.js").read_text()
    assert "export function handleEvent(" in live

    for environment in ("calendar", "negotiation"):
        assert (workspace / "games" / environment / "replay" / "index.html").is_file()


def test_embedded_replay_hides_its_own_chrome_with_css_not_just_hidden():
    """The `hidden` attribute loses to the page's own display rules, which is
    how a chrome-free embed silently keeps its toolbar."""
    workspace = Path(__file__).resolve().parents[2]
    page = (workspace / "games/negotiation/replay/index.html").read_text()

    assert "body.embedded .replay-bar" in page, "embed mode must hide chrome via CSS"
    assert 'params.get("embed")' in page


def test_every_environment_replay_page_resolves_and_is_self_contained():
    """The hand-rolled viewer is gone, so a replay page may not link into it.

    These pages are served out of the environment's own directory. A stylesheet
    or module reached from the retired hand-rolled viewer would 404 at exactly
    the moment someone opened a replay to debug something else.
    """
    workspace = Path(__file__).resolve().parents[2]

    for environment in ("calendar", "negotiation", "buyer-seller", "word-guess"):
        page = workspace / "games" / environment / "replay" / "index.html"
        assert page.is_file()
        source = page.read_text()
        assert "/css/styles.css" not in source
        assert "/replays/" not in source
        assert "/control.html" not in source
        # Vendored assets are served under the environment prefix, never the
        # retired game- one.
        assert "/game-assets/" not in source and "/game-replays/" not in source


def test_replay_pages_that_own_a_cursor_accept_one_from_a_host_frame():
    """The specialised-viewer seam: one scrubber drives the standard lane view
    and the environment's own rendering together.

    Calendar is deliberately excluded. Its page hands the stream to Calendar's
    own viewer and navigates away, so there is no cursor left to move and an
    inert listener would only claim otherwise.
    """
    workspace = Path(__file__).resolve().parents[2]

    for environment in ("negotiation", "buyer-seller", "word-guess"):
        sources = [
            path.read_text()
            for path in (workspace / "games" / environment / "replay").iterdir()
            if path.suffix in {".html", ".js"}
        ]
        joined = "\n".join(sources)
        assert 'event.data?.type === "cursor"' in joined, environment
        assert "function seek(" in joined, environment


def test_static_replay_and_asset_roots_are_each_confined_to_their_own_tree(tmp_path):
    """Three roots are served; none of them may be escaped."""
    workspace = Path(__file__).resolve().parents[2]
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_bytes(b"<!doctype html><title>Control plane</title>")
    secret = tmp_path / "secret.txt"
    secret.write_text("not servable")

    class TestHandler(LocalStackHandler):
        pass

    TestHandler.database = tmp_path / "a2a.db"
    TestHandler.static_dir = static_dir
    TestHandler.workspace = workspace
    TestHandler._control_plane = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        for environment in ("calendar", "negotiation", "buyer-seller", "word-guess"):
            status, _headers, body = _request(server, "GET", f"/environment-replays/{environment}/")
            assert status == 200, environment
            assert b"replay" in body.lower()

        status, _headers, _body = _request(
            server, "GET", "/environment-assets/negotiation/webapp/static/js/live.js")
        assert status == 200

        # An unserved environment name is a 404, not a path to somewhere else.
        assert _request(server, "GET", "/environment-replays/etc/")[0] == 404

        for escape in (
            "/environment-replays/calendar/../../../secret.txt",
            "/environment-assets/calendar/../../../secret.txt",
            "/../secret.txt",
        ):
            assert _request(server, "GET", escape)[0] == 403, escape
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_experiment_config_endpoint_only_serves_offered_configurations(tmp_path):
    """Reviewing a config in the browser must not become a way to read any
    file in the workspace that happens to end in .yaml."""
    import pytest

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)

    payload = control.read_experiment_config("games/negotiation/experiments/smoke_local.yaml")
    assert payload["content"].startswith("name: negotiation_smoke")
    assert len(payload["sha256"]) == 64

    for denied in ("docker-compose.yml", "../../etc/passwd", "experiments/storage.yaml"):
        with pytest.raises(ValueError, match="not an offered experiment configuration"):
            control.read_experiment_config(denied)


def test_offered_configurations_are_rescanned_not_cached(tmp_path):
    """A config dropped into a environment's experiments directory has to appear
    without restarting the control plane."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    added = workspace / "games/word-guess/experiments/_rescan_probe.yaml"
    added.write_text(
        "name: rescan_probe\n"
        "defaults: {environment_id: word_guess, num_agents: 2, max_turns: 4}\n"
        "cells:\n  - label: probe\n    count: 1\n    config: {secret_word: kite, seed: 99}\n"
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


def test_episode_detail_carries_the_lanes_and_cursor_the_browser_cannot_derive(tmp_path):
    """One episode, plus the pinned roster and the release's index label.

    Lanes come from provenance because that is the durable copy: the roster is
    frozen per experiment and the trace records it, so the lane view does not
    depend on the control plane still holding the experiment.
    """
    config = EpisodeConfigBase(
        environment_id="negotiation",
        num_agents=2,
        agents=[ParticipantBinding(model="model-a"), ParticipantBinding(model="model-b")],
        experiment_name="lanes-test",
        episode_id="lanes-test.cell.0",
    )
    payload = config.model_dump()
    payload["provenance"] = build_provenance(
        config=payload, experiment_name="lanes-test", cell_id="cell", episode_idx=0,
    )
    config = EpisodeConfigBase(**payload)
    trace = EpisodeTrace(
        episode_uid="lanes-run",
        config=config,
        events=[
            Event(type="phase_start", data={"round": 1}),
            Event(type="cheap_talk", data={"speaker": "participant_0", "text": "hello", "round": 1}),
        ],
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="lanes-test",
        cell_id="cell", episode_idx=0, episode_uid=trace.episode_uid,
    )
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, manifest)

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        detail = LocalStackHandler._episode_detail(trace.episode_uid)
        assert detail is not None
        assert detail["episode"]["episode_uid"] == trace.episode_uid
        assert [lane["participant_id"] for lane in detail["lanes"]] == ["participant_0", "participant_1"]
        assert [lane["binding"] for lane in detail["lanes"]] == ["model-a", "model-b"]
        # The scrubber's bound is the event count, not a derived guess.
        assert detail["cursor_max"] == 2
        # No shipped release declares a sequence-grained measure yet, so no
        # environment supplies an index. Absent asserts nothing false.
        assert detail["index_label"] is None
        assert LocalStackHandler._episode_detail("no-such-episode") is None
    finally:
        LocalStackHandler.database = previous


def test_episode_detail_survives_a_trace_with_no_events_and_no_provenance(tmp_path):
    """An attempt that died before its first turn is a state, not a gap."""
    config = EpisodeConfigBase(environment_id="word_guess", num_agents=1)
    trace = EpisodeTrace(episode_uid="empty-run", config=config, events=[])
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="empty-test",
        cell_id="cell", episode_idx=0, episode_uid=trace.episode_uid,
    )
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, manifest)

    previous = LocalStackHandler.database
    try:
        LocalStackHandler.database = store.path
        detail = LocalStackHandler._episode_detail(trace.episode_uid)
        assert detail == {
            "episode": detail["episode"],
            "lanes": [],
            "index_label": None,
            "cursor_max": 0,
        }
    finally:
        LocalStackHandler.database = previous


def test_index_label_comes_from_a_declared_sequence_measure(monkeypatch):
    """The environment's own word for position inside an episode.

    An uninstalled or unknown environment resolves to ``None`` rather than
    raising: a trace produced elsewhere must still open.
    """
    from a2a_engine.declaration import MeasureConfig

    class FakeSpec:
        declaration = type("Declaration", (), {"measures": [
            MeasureConfig(name="joint_reward", producer="environment"),
            MeasureConfig(name="round_reward", producer="derived", extractor="x",
                          grain="sequence", index_label="round"),
        ]})()

    monkeypatch.setattr("local_stack.server.discover_environments", lambda: None)
    monkeypatch.setattr("local_stack.server.get_environment_spec", lambda name: FakeSpec())
    assert LocalStackHandler._index_label("negotiation") == "round"

    monkeypatch.setattr(
        "local_stack.server.get_environment_spec",
        lambda name: (_ for _ in ()).throw(KeyError(name)),
    )
    assert LocalStackHandler._index_label("not_installed") is None
    assert LocalStackHandler._index_label("") is None


def test_experiment_agents_reports_models_and_credential_state(tmp_path, monkeypatch):
    """A live launch fails inside an HTTP client when a key is absent, so the
    line-up and its credential state have to be inspectable before launch."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
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
    """The environment supplies its own defaults there, so claiming readiness would
    assert a credential state nothing has checked."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
    report = control.experiment_agents("games/calendar/experiments/typed_local_smoke.yaml")

    assert report["declares_agents"] is False
    assert report["ready_for_live"] is None
    assert report["readiness_note"]


def test_agents_without_a_model_need_no_credential(tmp_path):
    """Heuristic and scripted agents call no provider."""
    workspace = Path(__file__).resolve().parents[2]
    control = ControlPlane(tmp_path / "a2a.db", workspace=workspace)
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
        def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
            control._mark_launch_started(launch.id)

        def cancel(self, _launch_id):
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
        launch = control.launch_experiment(experiment.id, smoke_test=True)
        try:
            launcher.launch(control.launch(launch.id), experiment, lambda line: None)
        except RuntimeError:
            pass
    finally:
        subprocess.Popen = original

    command = captured["command"]
    assert "--results-dir" in command
    results = command[command.index("--results-dir") + 1]
    assert results.endswith("/results")
    assert not results.startswith(str(control.workspace)), "artifacts must not land in the repo"
