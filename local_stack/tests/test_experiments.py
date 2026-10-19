"""Design records are locked, forked, and launched as fixed episode plans."""

from __future__ import annotations

import sqlite3
import sys
import time
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.control_plane import ControlPlane, DesignDigestMismatch, _roleless_design_sha256
from a2a_engine.design import parse_design_text
from a2a_engine.storage.schema import apply_schema


WORKSPACE = Path(__file__).resolve().parents[2]

DESIGN = """schema_version: 1
release: buyer_seller@v1
parameters:
  seller_cost: {randomize: true}
  buyer_value: {pin: 30}
  num_items: {pin: 3}
  discount_factor: {factor: [0.5, 1.0]}
units:
  episodes_per_cell: 1
roster:
  - id: seller
    role: seller
    kind: scripted
    binding: seller-baseline
  - id: buyer
    role: buyer
    kind: scripted
    binding: buyer-baseline
seed:
  root: 41
"""


class SilentLauncher:
    def __init__(self, control: ControlPlane) -> None:
        self.control = control

    def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
        self.control._mark_launch_started(launch.id)

    def cancel(self, _launch_id):
        return False


def control(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)


def create(plane: ControlPlane):
    return plane.create_experiment(
        name="Buyer seller pilot", release_id="buyer_seller", design_text=DESIGN
    )


def test_model_pool_bindings_resolve_before_lock_and_retain_the_frozen_route(tmp_path, monkeypatch):
    import yaml
    from a2a_engine.experiment import expand_cells, load_experiment
    from expt_runner.run_experiment import _check_api_keys

    pool_path = tmp_path / "agents.yaml"
    routing = {"type": "llm", "model": "deepseek/deepseek-v4.1-flash",
               "api_format": "openai", "api_base": "https://openrouter.ai/api/v1",
               "credential": "OPENROUTER_API_KEY", "temperature": 0.2, "max_tokens": 4096,
               "capabilities": {"input": ["text", "image"], "evidence": "test:model-contract"}}
    pool_path.write_text(yaml.safe_dump({"agents": {"ds-v4.1-flash": routing}}))
    monkeypatch.setenv("A2A_AGENT_POOL", str(pool_path))
    # Only credential preflight is exercised; no provider request is made.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-preflight-only")
    text = DESIGN.replace("kind: scripted", "kind: llm").replace(
        "seller-baseline", "ds-v4.1-flash").replace("buyer-baseline", "ds-v4.1-flash")
    plane = control(tmp_path)
    preview = plane.validate_design_text(release_id="buyer_seller", design_text=text)
    assert preview["valid"], preview["errors"]
    preview_agent = preview["plan"]["preview_episode_config"]["agents"][0]
    assert preview_agent["model"] == routing["model"]
    assert preview_agent["api_base"] == routing["api_base"]

    experiment = plane.create_experiment(name="Quick Play routing", release_id="buyer_seller", design_text=text)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    frozen = plane._design_episode_configs(locked, mode="live")
    for config in frozen:
        for agent in config["agents"]:
            assert agent["model"] == routing["model"]
            assert agent["api_base"] == routing["api_base"]
            assert agent["api_format"] == routing["api_format"]
            assert agent["capabilities"] == routing["capabilities"]
            assert agent["temperature"] == 0.2
            assert agent["max_tokens"] == 4096
            assert agent["credential"] == "OPENROUTER_API_KEY" and "api_key" not in agent
        _check_api_keys(config, mode="preflight")

    path = plane._write_execution_plan("routing-test", locked, frozen)
    runtime_configs = [config for _, config in expand_cells(load_experiment(path))]
    assert [config["agents"] for config in runtime_configs] == [config["agents"] for config in frozen]
    for config in runtime_configs:
        _check_api_keys(config, mode="preflight")
    original_hashes = frozen[0]["provenance"]["participants"]
    assert all(participant["binding"] == routing["model"] for participant in original_hashes)
    pool_path.write_text(yaml.safe_dump({"agents": {"ds-v4.1-flash": {**routing, "model": "changed-model"}}}))
    assert plane._design_episode_configs(locked, mode="live") == frozen
    assert locked.design_text == text


def test_lock_stores_verbatim_text_and_full_fixed_plan(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)

    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    assert locked.locked_at is not None
    assert locked.design_text == DESIGN
    detail = plane.experiment_detail(locked.id)
    assert len(detail["cells"]) == 2
    assert all(cell["episodes_planned"] == 1 for cell in detail["cells"])
    assert {participant["participant_id"]: participant["role"] for participant in detail["roster"]} == {
        "seller": "seller", "buyer": "buyer",
    }
    with sqlite3.connect(plane.path) as db:
        config = db.execute(
            "SELECT episode_configs FROM cells WHERE experiment_id = ?", (locked.id,)
        ).fetchone()[0]
    assert '"design_sha256"' in config
    assert '"item_attributes"' in config


def test_schema_migrates_and_backfills_only_compatible_locked_word_guess_rosters(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE participants (
            participant_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            binding TEXT,
            config_sha256 TEXT NOT NULL,
            PRIMARY KEY (participant_id, experiment_id)
        )""")
        apply_schema(db)
        experiment_columns = {row[1] for row in db.execute("PRAGMA table_info(experiments)")}
        assert "role" in {row[1] for row in db.execute("PRAGMA table_info(participants)")}
        assert {
            "authored_design_text", "authored_design_sha256", "authored_config_sha256",
            "role_normalization",
        } <= experiment_columns
        db.execute(
            "INSERT INTO releases (id, environment_id, source_ref) VALUES ('word_guess', 'word_guess', '')"
        )
        experiment_ids = (
            "compatible", "previously_backfilled", "unknown", "duplicate", "malformed", "incompatible",
            "partially_backfilled", "conflicting_backfill",
        )
        rosters = {
            "compatible": [("guesser_1", "scripted", "baseline"), ("host_1", "scripted", "baseline")],
            "previously_backfilled": [("guesser_1", "scripted", "baseline"), ("host_1", "scripted", "baseline")],
            "unknown": [("guesser_1", "scripted", "baseline"), ("observer_1", "scripted", "baseline")],
            "duplicate": [("guesser_1", "scripted", "baseline"), ("guesser_2", "scripted", "baseline")],
            "malformed": [("guesser_0", "scripted", "baseline"), ("host_1", "scripted", "baseline")],
            "incompatible": [("guesser_1", "unknown", "baseline"), ("host_1", "scripted", "baseline")],
            "partially_backfilled": [("guesser_1", "scripted", "baseline"), ("host_1", "scripted", "baseline")],
            "conflicting_backfill": [("guesser_1", "scripted", "baseline"), ("host_1", "scripted", "baseline")],
        }
        for experiment_id, roster in rosters.items():
            roster_text = "\n".join(
                f"  - id: {participant_id}\n    kind: scripted\n    binding: {binding}"
                for participant_id, _kind, binding in roster
            )
            design_text = f"""schema_version: 1
release: word_guess@v1
parameters:
  secret_word: {{pin: dog}}
  max_turns: {{pin: 6}}
units:
  episodes_per_cell: 1
roster:
{roster_text}
seed:
  root: 41
"""
            digest = _roleless_design_sha256(parse_design_text(design_text))
            db.execute(
                "INSERT INTO experiments (id, name, environment_id, release_id, yaml_path, config_sha256, design_text, design_sha256, locked_at, created_at) "
                "VALUES (?, ?, 'word_guess', 'word_guess', '', ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (experiment_id, experiment_id, digest, design_text, digest),
            )
            db.executemany(
                "INSERT INTO participants (participant_id, experiment_id, kind, binding, config_sha256) VALUES (?, ?, ?, ?, 'legacy-config')",
                [(participant_id, experiment_id, kind, binding) for participant_id, kind, binding in roster],
            )
            if experiment_id == "previously_backfilled":
                db.executemany(
                    "UPDATE participants SET role = ? WHERE experiment_id = ? AND participant_id = ?",
                    [("guesser", experiment_id, "guesser_1"), ("host", experiment_id, "host_1")],
                )
            if experiment_id == "partially_backfilled":
                db.execute(
                    "UPDATE participants SET role = 'guesser' WHERE experiment_id = ? AND participant_id = 'guesser_1'",
                    (experiment_id,),
                )
            if experiment_id == "conflicting_backfill":
                db.executemany(
                    "UPDATE participants SET role = ? WHERE experiment_id = ? AND participant_id = ?",
                    [("host", experiment_id, "guesser_1"), ("host", experiment_id, "host_1")],
                )
            db.execute(
                "INSERT INTO cells (cell_id, experiment_id, levels, episodes_planned, episode_configs, created_at) VALUES (?, ?, '{}', 1, ?, '2026-01-01T00:00:00Z')",
                (f"cell-{experiment_id}", experiment_id, json.dumps([{
                    "episode_id": f"{experiment_id}.cell.000",
                    "provenance": {"design_sha256": digest},
                }])),
            )

    ControlPlane(path, workspace=WORKSPACE)

    with sqlite3.connect(path) as db:
        roles = {
            experiment_id: dict(db.execute(
                "SELECT participant_id, role FROM participants WHERE experiment_id = ?", (experiment_id,)
            ))
            for experiment_id in experiment_ids
        }
        hashes = dict(db.execute(
            "SELECT participant_id, config_sha256 FROM participants WHERE experiment_id = 'compatible'"
        ))
        compatible = db.execute(
            "SELECT design_text, design_sha256, config_sha256, authored_design_text, "
            "authored_design_sha256, authored_config_sha256, role_normalization "
            "FROM experiments WHERE id = 'compatible'"
        ).fetchone()
        plan = db.execute(
            "SELECT episode_configs FROM cells WHERE experiment_id = 'compatible'"
        ).fetchone()[0]
        unsafe = {
            experiment_id: db.execute(
                "SELECT design_text, role_normalization FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            for experiment_id in experiment_ids if experiment_id not in {"compatible", "previously_backfilled"}
        }
    assert roles["compatible"] == {"guesser_1": "guesser", "host_1": "host"}
    assert roles["previously_backfilled"] == {"guesser_1": "guesser", "host_1": "host"}
    assert all(
        role is None
        for experiment_id in ("unknown", "duplicate", "malformed", "incompatible")
        for role in roles[experiment_id].values()
    )
    assert roles["partially_backfilled"] == {"guesser_1": "guesser", "host_1": None}
    assert roles["conflicting_backfill"] == {"guesser_1": "host", "host_1": "host"}
    assert hashes == {"guesser_1": "legacy-config", "host_1": "legacy-config"}
    normalized = parse_design_text(compatible[0])
    assert {participant.id: participant.role for participant in normalized.roster} == {
        "guesser_1": "guesser", "host_1": "host",
    }
    assert normalized.content_sha256() == compatible[1] == compatible[2]
    assert compatible[3] != compatible[0]
    assert _roleless_design_sha256(parse_design_text(compatible[3])) == compatible[4] == compatible[5]
    audit = json.loads(compatible[6])
    assert audit["prior"]["design_sha256"] == compatible[4]
    assert audit["normalized"]["design_sha256"] == compatible[1]
    assert json.loads(plan)[0]["provenance"]["design_sha256"] == compatible[4]
    with sqlite3.connect(path) as db:
        prior = db.execute(
            "SELECT design_text, role_normalization FROM experiments WHERE id = 'previously_backfilled'"
        ).fetchone()
    assert {participant.role for participant in parse_design_text(prior[0]).roster} == {"guesser", "host"}
    assert json.loads(prior[1])["version"] == "word_guess_role_normalization_v1"
    assert all(row[1] is None for row in unsafe.values())


def test_locked_design_requires_an_explicit_fork_before_editing(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    with pytest.raises(ValueError, match="fork it before editing"):
        plane.update_experiment_design(
            locked.id,
            design_text=DESIGN.replace(
                "seller_cost: {randomize: true}", "seller_cost: {pin: 8}"
            ),
        )

    fork = plane.fork_experiment_design(locked.id)

    assert fork.id != locked.id
    assert fork.forked_from == locked.id
    assert fork.locked_at is None
    assert fork.design_text == DESIGN
    assert plane.experiment(locked.id).design_text == DESIGN


def test_live_launch_requires_a_lock_but_smoke_and_dry_run_are_allowed(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    plane.launcher = SilentLauncher(plane)

    with pytest.raises(ValueError, match="locked preregistration"):
        plane.launch_experiment(experiment.id, mode="live")

    smoke = plane.launch_experiment(experiment.id, mode="smoke")
    dry_run = plane.launch_experiment(experiment.id, mode="dry_run")

    assert smoke.mode == "smoke"
    assert dry_run.mode == "dry_run"
    assert smoke.execution_path is not None
    assert Path(smoke.execution_path).is_file()


def test_launch_refuses_design_text_that_no_longer_matches_the_locked_digest(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")
    with sqlite3.connect(plane.path) as db:
        db.execute(
            "UPDATE experiments SET design_text = ? WHERE id = ?",
            (DESIGN.replace("1.0", "0.9"), locked.id),
        )

    with pytest.raises(DesignDigestMismatch):
        plane.launch_experiment(locked.id)


def test_locked_launch_writes_the_runner_config_with_planned_provenance(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")
    plane.launcher = SilentLauncher(plane)

    launch = plane.launch_experiment(locked.id, mode="smoke")
    plan = Path(launch.execution_path or "").read_text(encoding="utf-8")

    assert "_design_episode:" in plan
    assert "experiment_id:" in plan
    assert "item_attributes:" in plan
    assert plane.planned_episode_ids(launch.id)


def test_locked_smoke_launch_persists_compiled_provenance(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    launch = plane.launch_experiment(locked.id, mode="smoke")
    for _ in range(100):
        current = plane.launch(launch.id)
        if current.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.05)

    assert plane.launch(launch.id).status == "COMPLETED"
    detail = plane.launch_detail(launch.id)
    assert detail["progress"]["completed"] == 2
    episode_uid = detail["attempts"][0]["episode_uri"].split("#", 1)[1]
    trace = plane._store().get_episode(episode_uid)
    assert trace is not None
    provenance = trace.config.model_dump()["provenance"]
    assert provenance["experiment_id"] == locked.id
    assert provenance["design_sha256"] == locked.design_sha256
    assert set(provenance["item_attributes"]) == {"seller_cost"}


def test_experiment_credential_readiness_uses_selected_token_without_key_fallback(tmp_path, monkeypatch):
    import yaml
    monkeypatch.delenv("A2A_RUNTIME_MODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "different-account")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    path = tmp_path / "experiments" / "oauth.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({
        "name": "oauth-check", "defaults": {"environment_id": "word_guess", "num_agents": 2,
        "agents": [{"type": "llm", "model": "claude-haiku-5-5", "credential": "ANTHROPIC_AUTH_TOKEN"}] * 2},
        "cells": [{"label": "one", "count": 1}],
    }))
    plane = ControlPlane(tmp_path / "a2a.db", workspace=tmp_path)
    report = plane.experiment_agents("experiments/oauth.yaml")
    assert report["missing_credentials"] == ["ANTHROPIC_AUTH_TOKEN"]
    assert not report["ready_for_live"]
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "selected-account")
    report = plane.experiment_agents("experiments/oauth.yaml")
    assert report["missing_credentials"] == [] and report["ready_for_live"]
    assert all(agent["provider"] == "anthropic" for agent in report["agents"])
