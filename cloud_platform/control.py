"""Hosting adapter around the existing scientific experiment compiler.

Firestore documents own lifecycle/tenancy; immutable SQLite files are compiler
inputs and execution artifacts, never a shared cloud database.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
import yaml
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from local_stack.control_plane import ControlPlane, Launch
from a2a_engine.experiment import expand_cells, load_experiment
from a2a_engine.registry import get_environment_spec
from a2a_engine.provenance import pinned_participants
from cloud_platform.budget import apply_policy
from cloud_platform.storage import Repository, ArtifactStore

MAX_EXPORT_LAUNCHES = 20
MAX_EXPORT_ARTIFACTS = 10000
MAX_EXPORT_BYTES = 100 * 1024 * 1024


def reject_credentials(value, seen=None):
    """Platform-funded designs cannot carry credentials into frozen evidence."""
    forbidden = {"api_key", "secret", "access_token", "refresh_token", "client_secret", "password"}
    seen = seen if seen is not None else set()
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            return
        seen.add(id(value))
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in forbidden:
                raise ValueError("Credentials must be supplied by the platform, not experiment designs")
            reject_credentials(child, seen)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child, seen)


def now():
    return datetime.now(UTC).isoformat()


class PlanOnly:
    def launch(self, *args, **kwargs):
        pass

    def cancel(self, *args, **kwargs):
        pass


def snapshot(path):
    # SQLite backup includes WAL commits and produces a standalone artifact.
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "snapshot.db"
        with sqlite3.connect(path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination.read_bytes()


class PlatformControl:
    def __init__(self, repo=None, artifacts=None, workspace=None):
        self.repo = repo or Repository()
        self.artifacts = artifacts or ArtifactStore()
        self.workspace = Path(workspace or os.getenv("PLATFORM_WORKSPACE", Path.cwd())).resolve()
        self.source_sha = os.getenv("PLATFORM_SOURCE_SHA", "local")
        self.image_digest = os.getenv("PLATFORM_IMAGE_DIGEST", "local")
        self.job_name = os.getenv("PLATFORM_JOB_NAME", "")

    def compiler(self, directory, content=None):
        path = Path(directory) / "compiler.db"
        if content:
            path.write_bytes(content)
        cp = ControlPlane(path, workspace=self.workspace, results_dir=Path(directory) / "results")
        cp.launcher = PlanOnly()
        return cp

    def releases(self):
        with tempfile.TemporaryDirectory() as directory:
            cp = self.compiler(directory)
            templates = cp.available_experiments()
            return [{**asdict(release), "experiments": templates.get(release.environment_id, []),
                     "source_sha": self.source_sha, "image_digest": self.image_digest,
                     "job_name": self.job_name} for release in cp.releases()]

    def owned(self, kind, identifier, owner_id):
        value = self.repo.get(kind, identifier)
        # Identical response for absent and another user's records.
        if not value or value.get("owner_id") != owner_id:
            raise KeyError("record not found")
        return value

    def create_experiment(self, owner_id, body):
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("Name must contain 1–120 characters")
        release_id = body.get("release_id")
        releases = {r["id"]: r for r in self.releases()}
        if release_id not in releases:
            raise ValueError("Choose an installed release")
        release = releases[release_id]
        design_text = body.get("design_text")
        yaml_path = body.get("yaml_path")
        if design_text is None and yaml_path not in release["experiments"]:
            raise ValueError("Choose a checked-in experiment template")
        if design_text is not None and (not isinstance(design_text, str) or len(design_text) > 200000):
            raise ValueError("Design document is too large")
        if design_text is not None:
            try:
                reject_credentials(yaml.safe_load(design_text))
            except yaml.YAMLError as exc:
                raise ValueError("Design YAML is invalid") from exc
        with tempfile.TemporaryDirectory() as directory:
            cp = self.compiler(directory)
            cp.seed_installed_releases()
            exp = cp.create_experiment(name=name, release_id=release_id,
                                       design_text=design_text, yaml_path=yaml_path)
            if exp.design_text:
                exp = cp.lock_experiment(exp.id, design_sha256=exp.design_sha256)
                configs = cp._design_episode_configs(exp, mode="live")
            else:
                # Resolve sidecars, presets and agent pool once. The resulting
                # configs are portable across control-service releases and have
                # the same explicit identity contract as authored designs.
                from expt_runner.run_experiment import _make_run_context
                spec = load_experiment(self.workspace / exp.yaml_path)
                configs = []
                hooks = {exp.environment_id: get_environment_spec(exp.environment_id).resolve_config}
                for cell, resolved in expand_cells(spec, resolve_config=hooks):
                    if len(configs) + cell.count > 1000:
                        raise ValueError("An experiment is limited to 1000 planned episodes")
                    for index in range(cell.count):
                        config = _make_run_context(exp.name, cell.label, {"seed": 0, **resolved}, index,
                                                   dry_run=False)["config"]
                        # Namespace logical identity by experiment rather than
                        # a reusable template/name shared by different owners.
                        config["episode_id"] = f"{exp.id}.{cell.label}.{index}"
                        config["provenance"].update(experiment_id=exp.id, episode_id=config["episode_id"],
                                                    experiment_name=exp.name)
                        configs.append(config)
            if not configs or len(configs) > 1000:
                raise ValueError("An experiment must contain 1–1000 planned episodes")
            adaptations = {}
            if design_text is None:
                # Checked-in templates also serve the standalone external-seat
                # runner. Hosted execution owns agent lifetime, so those trusted
                # declarations become child processes of the episode worker.
                for config in configs:
                    for index, agent in enumerate(config.get("agents", [])):
                        if agent.get("runtime") == "external":
                            participant = str(agent.get("id") or agent.get("name") or f"participant_{index}")
                            adaptations[participant] = {"participant_id": participant,
                                                        "from_runtime": "external", "to_runtime": "local_process"}
                            agent["runtime"] = "local_process"
            policy = apply_policy(configs)
            policy["runtime_adaptations"] = [adaptations[key] for key in sorted(adaptations)]
            for config in configs:
                config["provenance"]["participants"] = pinned_participants(config)
            if exp.design_text:
                # Effective platform limits are part of the resolved execution
                # plan; preserve the authored roster/design separately.
                grouped = {}
                for config in configs:
                    grouped.setdefault(config["provenance"]["cell_id"], []).append(config)
                with cp._session() as db:
                    for cell_id, cell_configs in grouped.items():
                        db.execute("UPDATE cells SET episode_configs=? WHERE experiment_id=? AND cell_id=?",
                                   (json.dumps(cell_configs, sort_keys=True), exp.id, cell_id))
            data = {**asdict(exp), "owner_id": owner_id, "source_sha": self.source_sha,
                    "image_digest": self.image_digest, "job_name": self.job_name,
                    "status": "READY", "launch_ordinal": 0, "platform_policy": policy}
            key = f"experiments/{exp.id}/compiler.db"
            self.artifacts.put(key, snapshot(cp.path))
            data["snapshot_key"] = key
            data["frozen_plan_key"] = f"experiments/{exp.id}/frozen-plan.json"
            self.artifacts.put(data["frozen_plan_key"], json.dumps(configs, sort_keys=True).encode())
            # Retain exact input separately for a portable joined export.
            input_text = exp.design_text or (self.workspace / exp.yaml_path).read_text()
            self.artifacts.put(f"experiments/{exp.id}/input.yaml", input_text.encode())
            self.repo.create("experiments", exp.id, data)
            return data

    def detail(self, identifier, owner_id):
        exp = self.owned("experiments", identifier, owner_id)
        return {"experiment": exp, "launches": self.repo.list("launches", owner_id=owner_id,
                                                              experiment_id=identifier)}

    def launch(self, identifier, owner_id, body):
        exp = self.owned("experiments", identifier, owner_id)
        mode = body.get("mode", "smoke")
        if mode not in {"smoke", "live", "dry_run"}:
            raise ValueError("mode must be live, smoke, or dry_run")
        parallelism = int(body.get("max_parallelism", 1))
        if not 1 <= parallelism <= 2:
            raise ValueError("Episode parallelism must be between 1 and 2")
        configs = json.loads(self.artifacts.get(exp["frozen_plan_key"]))
        if mode == "smoke":
            seen = set()
            configs = [config for config in configs if not
                       (config["provenance"]["cell_id"] in seen or seen.add(config["provenance"]["cell_id"]))]
        if len(configs) > 100:
            raise ValueError("A launch is limited to 100 episodes")
        ordinal = self.repo.next_attempt(identifier)
        for config in configs:
            config["provenance"]["attempt"] = ordinal
        with tempfile.TemporaryDirectory() as directory:
            # Materialize frozen dimensions without loading current release
            # declarations or resolving the old experiment against new code.
            cp = ControlPlane.__new__(ControlPlane)
            cp.path = Path(directory) / "compiler.db"
            cp.path.write_bytes(self.artifacts.get(exp["snapshot_key"]))
            cp.trace_database = cp.path
            cp.workspace = self.workspace
            cp.results_dir = Path(directory) / "results"
            cp._init_db()
            frozen_experiment = cp.experiment(identifier)
            launch_id = str(uuid.uuid4())
            execution_path = cp._write_execution_plan(launch_id, frozen_experiment, configs)
            launch = Launch(id=launch_id, experiment_id=identifier, status="QUEUED", mode=mode,
                            max_parallelism=parallelism, trace_database=str(cp.path),
                            execution_path=execution_path, created_at=now())
            with cp._session() as db:
                db.execute("INSERT INTO launches (id,experiment_id,status,max_parallelism,trace_database,"
                           "mode,execution_path,created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (launch.id, identifier, launch.status, parallelism, str(cp.path), mode,
                            execution_path, launch.created_at))
                db.executemany("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                    (str(uuid.uuid4()), launch.id, config["episode_id"], config["provenance"]["cell_id"],
                     config["provenance"]["episode_idx"], ordinal, "QUEUED", None, None, None, None)
                    for config in configs])
                cp._event(db, launch.id, "launch.queued", {"episode_count": len(configs), "mode": mode})
            detail = cp.launch_detail(launch.id)
            plan = Path(launch.execution_path or self.workspace / exp["yaml_path"]).read_bytes()
            prefix = f"launches/{launch.id}"
            self.artifacts.put(f"{prefix}/input.db", snapshot(cp.path))
            self.artifacts.put(f"{prefix}/plan.yaml", plan)
            data = {**asdict(launch), "owner_id": owner_id, "source_sha": exp["source_sha"],
                    "image_digest": exp["image_digest"], "job_name": exp["job_name"],
                    "source_snapshot_key": f"{prefix}/input.db", "snapshot_key": f"{prefix}/input.db",
                    "plan_key": f"{prefix}/plan.yaml", "episode_count": len(detail["attempts"]),
                    "artifact_prefix": prefix, "dispatch_status": "PENDING"}
            # Plan files exist before admission; rejected launches remain unreferenced.
            self.repo.admit_launch(data)
            for attempt in detail["attempts"]:
                self.repo.create("attempts", attempt["id"], {**attempt, "owner_id": owner_id,
                                                             "experiment_id": identifier})
            return data

    def export(self, identifier, owner_id):
        exp = self.owned("experiments", identifier, owner_id)
        launches = self.repo.list("launches", owner_id=owner_id, experiment_id=identifier)
        if len(launches) > MAX_EXPORT_LAUNCHES:
            raise ValueError(f"An export is limited to {MAX_EXPORT_LAUNCHES} launches")
        if any(row["status"] in {"QUEUED", "RUNNING", "DISPATCHED", "DISPATCHING"} for row in launches):
            raise ValueError("Wait for active launches to finish before exporting their artifacts")
        metadata = {"experiment": exp, "launches": launches,
                    "attempts": self.repo.list("attempts", owner_id=owner_id, experiment_id=identifier),
                    "episodes": self.repo.list("episodes", owner_id=owner_id, experiment_id=identifier),
                    "exported_at": now(), "schema_version": 1}
        metadata_bytes = json.dumps(metadata, indent=2).encode()
        metric_csv = io.StringIO()
        writer = csv.writer(metric_csv)
        writer.writerow(["experiment_id", "launch_id", "episode_uid", "status", "metrics_json"])
        for episode in metadata["episodes"]:
            writer.writerow([identifier, episode["launch_id"], episode.get("episode_uid", episode.get("id")),
                             episode.get("status"), json.dumps(episode.get("metrics", {}), sort_keys=True)])
        metric_bytes = metric_csv.getvalue().encode()
        prefixes = [f"experiments/{identifier}/"] + [f"launches/{launch['id']}/" for launch in launches]
        keys = []
        total_size = len(metadata_bytes) + len(metric_bytes)
        for prefix in prefixes:
            for key in self.artifacts.keys(prefix):
                keys.append(key)
                if len(keys) > MAX_EXPORT_ARTIFACTS:
                    raise ValueError(f"An export is limited to {MAX_EXPORT_ARTIFACTS} artifacts")
                total_size += self.artifacts.size(key)
                if total_size > MAX_EXPORT_BYTES:
                    raise ValueError("An export is limited to 100 MiB of uncompressed evidence")
        if total_size > MAX_EXPORT_BYTES:
            raise ValueError("An export is limited to 100 MiB of uncompressed evidence")
        content = io.BytesIO()
        checksums = {}
        with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
            def add(key, data):
                checksums[key] = hashlib.sha256(data).hexdigest()
                archive.writestr(key, data)
            add("metadata.json", metadata_bytes)
            add("metrics.csv", metric_bytes)
            for key in keys:
                add(key, self.artifacts.get(key))
            archive.writestr("checksums.json", json.dumps(checksums, indent=2))
        export_id = str(uuid.uuid4())
        key = f"exports/{export_id}/experiment.zip"
        self.artifacts.put(key, content.getvalue())
        record = {"id": export_id, "owner_id": owner_id, "experiment_id": identifier,
                  "artifact_key": key, "created_at": now(), "status": "COMPLETED",
                  "download_url": f"/api/exports/{export_id}/download"}
        self.repo.create("exports", export_id, record)
        return record
