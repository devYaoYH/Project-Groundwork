"""The dispatch boundary: what crosses it, what does not, and who settles it.

These are the branches the callback launcher never had.  ``cancel`` was
completely untested -- every stub hardcoded ``cancel() -> False``, so the
``CANCELLING`` transition had never executed -- and nothing re-validated the
plan's bytes between writing it and running it, because the staleness guard
ran before the write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from a2a_engine.agent_pool import load_agent_pool
from a2a_engine.artifacts import ArtifactRef, sha256_bytes
from a2a_engine.design import parse_design_text
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore

from local_stack.control_plane import ControlPlane, Launch, _roleless_design_sha256
from local_stack.launchers import ExecutionHandle, ExecutionStatus, LaunchInputRef
from local_stack.launchers.local_process import (
    PROVIDER_CREDENTIAL_NAMES,
    LocalProcessLauncher,
)
from local_stack.server import resolve_declared_credentials
from local_stack.tests.fakes import FakeLauncher

WORKSPACE = Path(__file__).resolve().parents[2]

BUYER_SELLER_DESIGN = (Path(__file__).parent / "buyer_seller_design_smoke.yaml").read_text()

# A role-less Word Guess roster is what the role-normalization backfill
# rewrites: the locked design's text and digest move while the frozen
# ``episode_configs[].provenance.design_sha256`` keeps the authored one. Roles
# are required of a design authored today, so the legacy shape can only be
# reached the way a real legacy row was -- by predating the requirement.
WORD_GUESS_DESIGN = """schema_version: 1
release: word_guess@v1
parameters:
  secret_word: {pin: dog}
  max_turns: {pin: 6}
units:
  episodes_per_cell: 1
roster:
  - id: guesser_1
    role: guesser
    kind: scripted
    binding: baseline
  - id: host_1
    role: host
    kind: scripted
    binding: baseline
seed:
  root: 41
"""

ROLELESS_WORD_GUESS_DESIGN = "\n".join(
    line for line in WORD_GUESS_DESIGN.splitlines() if "    role: " not in line
) + "\n"


def _control(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)


def _locked(control: ControlPlane, design_text: str, *, release_id: str, name: str):
    experiment = control.create_experiment(
        name=name, release_id=release_id, design_text=design_text,
    )
    return control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")


def _settle(control: ControlPlane, launch_id: str, *, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = control.launch(launch_id).status
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return status
        time.sleep(0.05)
    return control.launch(launch_id).status


def _make_legacy_roleless(control: ControlPlane, experiment_id: str) -> str:
    """Rewind a locked Word Guess experiment to the pre-``role`` shape.

    Roles are required of a design authored today, so the only honest way to
    produce the row the backfill exists for is to write the row a pre-``role``
    lock would have written: role-less text, the digest that text hashed to
    before the field existed, and frozen episode configs stamped with it.
    """
    roleless = parse_design_text(ROLELESS_WORD_GUESS_DESIGN)
    digest = _roleless_design_sha256(roleless)
    with control._session() as db:
        db.execute(
            "UPDATE experiments SET design_text = ?, design_sha256 = ?, config_sha256 = ? "
            "WHERE id = ?",
            (ROLELESS_WORD_GUESS_DESIGN, digest, digest, experiment_id),
        )
        db.execute(
            "UPDATE participants SET role = NULL WHERE experiment_id = ?", (experiment_id,)
        )
        for row in db.execute(
            "SELECT cell_id, episode_configs FROM cells WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchall():
            configs = json.loads(row["episode_configs"])
            for config in configs:
                config["provenance"]["design_sha256"] = digest
            db.execute(
                "UPDATE cells SET episode_configs = ? WHERE experiment_id = ? AND cell_id = ?",
                (json.dumps(configs, separators=(",", ":"), sort_keys=True),
                 experiment_id, row["cell_id"]),
            )
    return digest


def _persist_planned_episode(control: ControlPlane, config: dict, *, episode_uid: str) -> str:
    episode = EpisodeConfigBase.model_validate(config)
    trace = EpisodeTrace(episode_uid=episode_uid, config=episode)
    manifest = EpisodeManifest.from_run(
        config=episode.model_dump(), experiment_name=str(episode.experiment_name or ""),
        cell_id=str(config["provenance"]["cell_id"]),
        episode_idx=int(config["provenance"]["episode_idx"]), episode_uid=episode_uid,
    )
    manifest.episode_id = str(episode.episode_id)
    manifest.environment_id = str(episode.environment_id)
    manifest.attempt = int(config["provenance"]["attempt"])
    return SQLiteEpisodeStore(path=control.path).put_episode(trace, manifest)


# --- the credential boundary ------------------------------------------------


#: A pool small enough to reason about: two bindings that declare a credential,
#: one that needs none. Pointed at through ``A2A_AGENT_POOL`` so these tests
#: pin the *rule* rather than whichever providers the shipped pool binds today.
DECLARED_POOL = """
agents:
  gpt-mini:
    type: llm
    model: gpt-5-mini
    credential: OPENAI_API_KEY
  haiku:
    type: llm
    model: claude-haiku-4-5-20251001
    credential: ANTHROPIC_API_KEY
  heuristic:
    type: heuristic
"""


def _declared_pool(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "declared-agents.yaml"
    path.write_text(DECLARED_POOL)
    monkeypatch.setenv("A2A_AGENT_POOL", str(path))
    return path


def _bare_launch(tmp_path: Path) -> Launch:
    """A launch row, without the control plane it would normally come from.

    ``_environment`` reads three fields off it and nothing else, which is the
    point: the worker's environment is built from the launcher's own
    configuration, not from anything the launch carries.
    """
    return Launch(
        id="launch-under-test",
        experiment_id="experiment-under-test",
        status="QUEUED",
        max_parallelism=1,
        trace_database=str(tmp_path / "a2a.db"),
        created_at="2026-01-01T00:00:00+00:00",
        mode="smoke",
    )


def test_a_submitted_worker_receives_no_provider_credential(tmp_path, monkeypatch):
    """The *default* launcher forwards nothing.

    The only reason the viewer holds every provider key is structural: the old
    launcher handed ``dict(os.environ)`` to the child. An explicit allowlist
    makes that impossible rather than merely discouraged, and a launcher that
    was configured with no credentials forwards none however many are exported
    into this process."""
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Env boundary")
    control.launcher = FakeLauncher()
    launch = control.launch_experiment(locked.id, mode="smoke")

    for name in PROVIDER_CREDENTIAL_NAMES:
        monkeypatch.setenv(name, f"secret-{name}")
    monkeypatch.setenv("A2A_SOMETHING_UNDECLARED", "leaked")

    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        raise RuntimeError("stop before spawning")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    launcher = LocalProcessLauncher(control.workspace)
    with pytest.raises(RuntimeError):
        launcher.submit(control.launch(launch.id), LaunchInputRef("file:///plan.yaml", "abc"))

    env = captured["env"]
    assert not PROVIDER_CREDENTIAL_NAMES & set(env)
    assert not any("secret-" in value for value in env.values())
    # The allowlist is a list, not a denylist: an unrecognised variable is
    # dropped too, so adding a new provider key cannot leak by default.
    assert "A2A_SOMETHING_UNDECLARED" not in env


def test_a_credential_reaches_a_worker_only_by_being_named(tmp_path, monkeypatch):
    """The seam a symbolic credential reference replaces later: the worker's
    credential environment is supplied explicitly, never inherited."""
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Named creds")
    control.launcher = FakeLauncher()
    launch = control.launch_experiment(locked.id, mode="smoke")

    launcher = LocalProcessLauncher(
        control.workspace, credentials={"OPENAI_API_KEY": "sk-scoped"},
    )
    env = launcher._environment(control.launch(launch.id))
    assert env["OPENAI_API_KEY"] == "sk-scoped"
    assert "ANTHROPIC_API_KEY" not in env


def test_a_configured_launcher_forwards_exactly_the_declared_names(tmp_path, monkeypatch):
    """The other half of the invariant: a launcher the control plane configured
    forwards the pool's declared names and still drops everything else.

    ``required_credentials`` is what decides, so widening the forwarded set is
    an edit to the agent pool rather than an accident of what this process
    happens to have exported."""
    _declared_pool(tmp_path, monkeypatch)
    for name in PROVIDER_CREDENTIAL_NAMES:
        monkeypatch.setenv(name, f"secret-{name}")
    monkeypatch.setenv("A2A_SOMETHING_UNDECLARED", "leaked")

    credentials = resolve_declared_credentials(WORKSPACE)
    assert credentials == {
        "OPENAI_API_KEY": "secret-OPENAI_API_KEY",
        "ANTHROPIC_API_KEY": "secret-ANTHROPIC_API_KEY",
    }

    launcher = LocalProcessLauncher(WORKSPACE, credentials=credentials)
    env = launcher._environment(_bare_launch(tmp_path))

    assert env["OPENAI_API_KEY"] == "secret-OPENAI_API_KEY"
    assert env["ANTHROPIC_API_KEY"] == "secret-ANTHROPIC_API_KEY"
    # Exported here, declared by nothing: the allowlist did not quietly become
    # a passthrough just because it now forwards something.
    assert PROVIDER_CREDENTIAL_NAMES & set(env) == set(credentials)
    assert "A2A_SOMETHING_UNDECLARED" not in env


def test_an_unsatisfied_declared_credential_is_named_rather_than_forwarded_empty(
    tmp_path, monkeypatch,
):
    """Compose exports every provider variable with a ``${VAR:-}`` default, so
    an unset key is present-and-empty rather than absent. Forwarding that empty
    string would turn a gap reported by name into a 401 discovered mid-episode,
    so the forwarded set omits it and the preflight keeps naming it."""
    _declared_pool(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-present")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    pool = load_agent_pool(WORKSPACE / "experiments")
    assert pool.missing_credentials(sorted(pool.agents)) == ["ANTHROPIC_API_KEY"]

    credentials = resolve_declared_credentials(WORKSPACE)
    assert credentials == {"OPENAI_API_KEY": "sk-present"}

    launcher = LocalProcessLauncher(WORKSPACE, credentials=credentials)
    env = launcher._environment(_bare_launch(tmp_path))
    assert "ANTHROPIC_API_KEY" not in env


def test_the_shipped_pool_declares_every_credential_the_control_plane_forwards(monkeypatch):
    """Against the real pool rather than a fixture: whatever is forwarded is
    something a binding asked for, and a provider key the pool never mentions
    cannot reach a worker no matter what is exported here."""
    monkeypatch.delenv("A2A_AGENT_POOL", raising=False)
    for name in PROVIDER_CREDENTIAL_NAMES:
        monkeypatch.setenv(name, f"secret-{name}")

    pool = load_agent_pool(WORKSPACE / "experiments")
    declared = set(pool.required_credentials(sorted(pool.agents)))
    assert declared, "the shipped pool binds at least one credentialled agent"

    resolved = resolve_declared_credentials(WORKSPACE)
    assert set(resolved) == {name for name in declared if os.environ.get(name)}
    assert PROVIDER_CREDENTIAL_NAMES - declared, (
        "a provider name no binding declares is what proves the set is the "
        "pool's rather than the environment's"
    )
    assert not (PROVIDER_CREDENTIAL_NAMES - declared) & set(resolved)


# --- the launch input -------------------------------------------------------


def test_the_published_launch_input_is_the_plan_bound_to_its_own_digest(tmp_path):
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Plan artifact")
    launcher = FakeLauncher()
    control.launcher = launcher
    launch = control.launch_experiment(locked.id, mode="smoke")

    ref = launcher.last_input()
    published = control.artifacts.get(ref.uri)
    assert sha256_bytes(published) == ref.sha256 == launch.launch_input_sha256
    # The rendered plan is unchanged; it is now addressed rather than assumed.
    assert published == Path(str(launch.execution_path)).read_bytes()
    assert ref.uri == launch.launch_input_uri


def test_a_worker_refuses_a_launch_input_altered_after_publication(tmp_path):
    """Nothing re-validated the plan's bytes after writing it. Now the worker
    fails closed on identity instead of running a plan nobody reviewed."""
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Tampered plan")

    class TamperingLauncher(LocalProcessLauncher):
        def submit(self, launch, input_ref):
            path = Path(input_ref.uri.removeprefix("file://"))
            path.write_bytes(path.read_bytes() + b"\n# rewritten after publication\n")
            return super().submit(launch, input_ref)

    control.launcher = TamperingLauncher(control.workspace)
    launch = control.launch_experiment(locked.id, mode="smoke")

    assert _settle(control, launch.id) == "FAILED"
    detail = control.launch_detail(launch.id)
    assert {attempt["status"] for attempt in detail["attempts"]} == {"FAILED"}
    assert not control.launch_detail(launch.id)["progress"]["completed"]


def test_launch_input_mismatch_is_raised_before_anything_is_parsed():
    from expt_runner.run_experiment import LaunchInputMismatch, _resolve_launch_input

    with pytest.raises(LaunchInputMismatch):
        _resolve_launch_input(
            (WORKSPACE / "local_stack/tests/buyer_seller_design_smoke.yaml").as_uri(),
            "0" * 64,
        )


# --- settling from evidence -------------------------------------------------


def test_a_launch_settles_completed_without_the_launcher_reporting_anything(tmp_path):
    """Reconcile-only. The fake never calls back, and a launch whose episodes
    persisted still settles COMPLETED from the identity join alone."""
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Reconcile only")
    launcher = FakeLauncher()
    control.launcher = launcher
    launch = control.launch_experiment(locked.id, mode="smoke")
    assert control.launch(launch.id).status == "RUNNING"

    attempts = {
        attempt["episode_id"]: attempt["attempt"]
        for attempt in control.launch_detail(launch.id)["attempts"]
    }
    for index, config in enumerate(control._design_episode_configs(locked, mode="smoke")):
        config["provenance"] = {
            **config["provenance"], "attempt": attempts[config["episode_id"]],
        }
        _persist_planned_episode(control, config, episode_uid=f"reconciled-{index}")

    launcher.finish(launch.id)
    assert control.launch(launch.id).status == "COMPLETED"
    detail = control.launch_detail(launch.id)
    assert {attempt["status"] for attempt in detail["attempts"]} == {"COMPLETED"}
    assert detail["progress"]["completed"] == len(attempts)


def test_a_role_normalized_experiment_still_dispatches_and_settles(tmp_path):
    """A frozen ``provenance.design_sha256`` may legitimately differ from
    ``experiments.design_sha256``. The launch input is bound to the plan's own
    bytes, so nothing downstream assumes the two agree."""
    control = _control(tmp_path)
    locked = _locked(
        control, WORD_GUESS_DESIGN, release_id="word_guess", name="Roleless word guess",
    )
    authored_digest = _make_legacy_roleless(control, locked.id)

    # Constructing a control plane is what runs the normalization backfill.
    normalized_plane = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)
    normalized = normalized_plane.experiment(locked.id)
    assert normalized.design_sha256 != authored_digest
    assert normalized.role_normalization is not None
    frozen = normalized_plane._design_episode_configs(normalized, mode="smoke")
    assert {config["provenance"]["design_sha256"] for config in frozen} == {authored_digest}

    launch = normalized_plane.launch_experiment(normalized.id, mode="smoke")
    assert _settle(normalized_plane, launch.id) == "COMPLETED"
    assert normalized_plane.launch_detail(launch.id)["progress"]["completed"] == len(frozen)


# --- cancellation -----------------------------------------------------------


def test_cancel_terminates_a_real_worker_and_the_reconciler_settles_it(tmp_path):
    """The branch no test has ever executed: every stub returned False, so the
    CANCELLING transition had never run."""
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Cancellable")

    class SleepingWorkerLauncher(LocalProcessLauncher):
        """A real child process that outlives the request that cancels it."""

        def _command(self, launch, input_ref):
            return [self.python, "-c", "import time; time.sleep(120)"]

    control.launcher = SleepingWorkerLauncher(control.workspace)
    launch = control.launch_experiment(locked.id, mode="smoke")
    assert control.launch(launch.id).status == "RUNNING"

    cancelling = control.cancel_launch(launch.id)
    assert cancelling.status == "CANCELLING"

    assert _settle(control, launch.id) == "CANCELLED"
    detail = control.launch_detail(launch.id)
    # A cancelled attempt is still settled from evidence, so partial traces
    # would have been recovered rather than discarded.
    assert {attempt["status"] for attempt in detail["attempts"]} == {"CANCELLED"}


def test_cancel_reports_false_when_there_is_nothing_to_stop(tmp_path):
    launcher = LocalProcessLauncher(tmp_path)
    assert launcher.cancel(ExecutionHandle(backend="local_process", id="never-submitted")) is False
    assert launcher.describe(
        ExecutionHandle(backend="local_process", id="never-submitted")
    ) is ExecutionStatus.UNKNOWN


# --- the handle -------------------------------------------------------------


def test_the_execution_handle_survives_a_restart_as_a_persisted_row(tmp_path):
    control = _control(tmp_path)
    locked = _locked(control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="Handle")
    control.launcher = FakeLauncher()
    launch = control.launch_experiment(locked.id, mode="smoke")

    handle = ExecutionHandle.from_json(control.launch(launch.id).execution_handle)
    assert handle is not None and handle.backend == "fake" and handle.id == launch.id

    # A restarted process issues a fresh launcher, which knows nothing about
    # this handle -- UNKNOWN, which routes straight to the evidence join.
    restarted = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)
    assert restarted.launch(launch.id).status == "FAILED"


def test_a_malformed_handle_reads_as_absent_rather_than_raising(tmp_path):
    assert ExecutionHandle.from_json(None) is None
    assert ExecutionHandle.from_json("") is None
    assert ExecutionHandle.from_json("{not json") is None
    assert ExecutionHandle.from_json('{"id": "x"}') is None
    handle = ExecutionHandle(backend="b", id="i", detail={"pid": 1})
    assert ExecutionHandle.from_json(handle.to_json()) == handle


def test_a_legacy_yaml_experiment_references_its_workspace_file_by_digest(tmp_path):
    """A legacy experiment has no compiled plan to publish, so its launch input
    is the reviewed workspace file -- referenced in place, still digest-bound,
    because copying it would move it away from the sidecars it resolves
    against."""
    control = _control(tmp_path)
    experiment = control.create_experiment(
        release_id="buyer_seller",
        yaml_path="games/buyer-seller/experiments/example.yaml",
    )
    launcher = FakeLauncher()
    control.launcher = launcher
    launch = control.launch_experiment(experiment.id, smoke_test=True)

    ref = launcher.last_input()
    source = WORKSPACE / experiment.yaml_path
    assert ref == ArtifactRef(uri=source.as_uri(), sha256=sha256_bytes(source.read_bytes()))
    assert launch.launch_input_sha256 == ref.sha256


def test_a_read_during_dispatch_does_not_strand_the_launch_it_races(tmp_path):
    """The window between committing QUEUED and persisting the handle.

    ``launch_experiment`` commits the attempt rows before it submits, so for
    the width of one ``submit()`` the launch is QUEUED with no handle -- which
    is indistinguishable, from the row alone, from a launch whose dispatching
    process died. The server is threaded and every read reconciles, so a
    browser polling right after POST used to settle a launch that was in fact
    starting. A dispatch in flight is tracked in memory precisely because a
    restart clears it: after a restart a handle-less launch really is stranded.
    """
    observed: list[str] = []

    def read_from_another_thread(launch, _ref):
        # Exactly what a concurrent GET /api/launches/<id> does: a reconciling
        # read, issued while this very launch sits QUEUED with no handle.
        control.reconcile()
        observed.append(control.launch(launch.id).status)
        return None

    control = _control(tmp_path)
    locked = _locked(
        control, BUYER_SELLER_DESIGN, release_id="buyer_seller", name="racing",
    )
    control.launcher = FakeLauncher(on_submit=read_from_another_thread)
    launch = control.launch_experiment(locked.id, smoke_test=True)

    assert observed == ["QUEUED"], "a dispatch in flight must not settle"
    assert control.launch(launch.id).status == "RUNNING"

    # And the restart case still settles: a fresh control plane owns no
    # dispatch, so the same handle-less row is correctly stranded.
    stranded = control.launch_experiment(locked.id, smoke_test=True)
    with control._session() as db:
        db.execute(
            "UPDATE launches SET execution_handle = NULL, status = 'QUEUED' WHERE id = ?",
            (stranded.id,),
        )
    restarted = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)
    assert restarted.launch(stranded.id).status == "FAILED"
