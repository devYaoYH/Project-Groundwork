"""Owner-scoped adapters for the existing research viewer's API contracts.

Installed declarations are shared; experimental dimensions and evidence are
read only after ownership is checked, from that experiment's private artifacts.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from a2a_engine.provenance import configured_roles, pinned_participants
from a2a_engine.registry import get_environment_spec
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from a2a_engine.stream_projection import project_events_to_trace, projection_summary
from local_stack.control_plane import ControlPlane
from local_stack.retries import retry_options
from cloud_platform.frozen import open_frozen_control


class Viewer:
    def __init__(self, control):
        self.control = control

    @contextmanager
    def catalog(self):
        yield self.control.catalogue

    @contextmanager
    def private_snapshot(self, record, key="snapshot_key"):
        # Caller has checked ownership. Never construct a normal ControlPlane
        # here: its local-server startup recovery would mark launches abandoned.
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "private.sqlite"
            database.write_bytes(self.control.artifacts.get(record[key]))
            cp = open_frozen_control(database, workspace=self.control.workspace,
                                     results_dir=Path(directory) / "results")
            yield cp

    def environments(self, owner):
        counts = Counter(record["environment_id"] for record in self.control.repo.list("experiments", owner_id=owner))
        with self.catalog() as cp:
            return [{**entry, "experiment_count": counts[entry["environment_id"]]} for entry in cp.environment_summaries()]

    def environment(self, identifier, operation="detail", query=None, body=None):
        with self.catalog() as cp:
            if operation == "workflow":
                return cp.environment_workflow(identifier)
            if operation == "items":
                return cp.environment_items(identifier, limit=int((query or {}).get("limit", 100)), cursor=(query or {}).get("cursor"))
            if operation == "oracle":
                return cp.run_oracle(identifier, str((body or {}).get("item_id") or ""))
            return cp.environment_detail(identifier)

    def environment_asset(self, identifier, digest):
        with self.catalog() as cp:
            return cp.environment_asset(identifier, digest)

    def agent_pool(self):
        with self.catalog() as cp:
            payload = cp.agent_pool()
        # Control service deliberately receives no provider credentials. The
        # shared pool describes choices, not the worker's secret environment.
        for agent in payload.get("agents", []):
            agent["credential_present"] = None
        return payload

    def validate(self, body):
        with self.catalog() as cp:
            return cp.validate_design_text(release_id=str(body.get("release_id") or ""), design_text=str(body.get("design_text") or ""))

    def experiment(self, identifier, owner):
        record = self.control.owned("experiments", identifier, owner)
        with self.private_snapshot(record) as cp:
            dimensions = cp.experiment_detail(identifier)
        episodes = self.control.repo.list("episodes", owner_id=owner, experiment_id=identifier)
        attempts = self.control.repo.list("attempts", owner_id=owner, experiment_id=identifier)
        cells = dimensions["cells"]
        planned = json.loads(self.control.artifacts.get(record["frozen_plan_key"])) if record.get("frozen_plan_key") else []
        if not cells and planned:
            counts = Counter(config["provenance"]["cell_id"] for config in planned)
            cells = [{"cell_id": key, "levels": {}, "episodes_planned": count} for key, count in sorted(counts.items())]
            dimensions["cells"] = cells
            if planned and not dimensions["roster"]:
                dimensions["roster"] = [{**entry, "role": None} for entry in pinned_participants(planned[0])]
        planned_ids = defaultdict(set)
        for config in planned:
            planned_ids[config["provenance"]["cell_id"]].add(config["episode_id"])
        latest_attempts = {}
        for attempt in attempts:
            key = (attempt.get("cell_id"), attempt["episode_id"])
            previous = latest_attempts.get(key)
            if previous is None or (attempt["attempt"], attempt["id"]) > (previous["attempt"], previous["id"]):
                latest_attempts[key] = attempt
        latest_traces = {}
        for trace in episodes:
            key = (trace["episode_id"], trace["attempt"])
            previous = latest_traces.get(key)
            if previous is None or (trace.get("created_at") or "", trace["episode_uid"]) > (previous.get("created_at") or "", previous["episode_uid"]):
                latest_traces[key] = trace
        evidence = []
        for cell in cells:
            # A retry replaces its logical observation, including when the
            # latest retry failed. Older traces remain in the user's corpus.
            rows = [latest_attempts[(cell["cell_id"], episode_id)] for episode_id in planned_ids[cell["cell_id"]]
                    if (cell["cell_id"], episode_id) in latest_attempts]
            complete = [latest_traces[(row["episode_id"], row["attempt"])] for row in rows
                        if row["status"] == "COMPLETED" and latest_traces.get((row["episode_id"], row["attempt"]), {}).get("status") == "COMPLETED"]
            metrics = defaultdict(list)
            for row in complete:
                for name, value in (row.get("metrics") or {}).items():
                    if isinstance(value, bool) or isinstance(value, (int, float)) and math.isfinite(value):
                        metrics[name].append(value)
            summaries = []
            for name, values in sorted(metrics.items()):
                if all(isinstance(value, bool) for value in values):
                    summaries.append({"name": name, "kind": "boolean", "n": len(values), "true_count": sum(values), "false_count": len(values) - sum(values)})
                else:
                    numeric = [float(value) for value in values if not isinstance(value, bool)]
                    if numeric and len(numeric) == len(values):
                        summary = {"name": name, "kind": "number", "n": len(numeric), "mean": statistics.fmean(numeric), "min": min(numeric), "max": max(numeric)}
                        if len(numeric) > 1:
                            summary["stddev"] = statistics.stdev(numeric)
                        summaries.append(summary)
            status_counts = Counter(row["status"] for row in rows)
            if cell["episodes_planned"] > len(rows):
                status_counts["NOT_STARTED"] = cell["episodes_planned"] - len(rows)
            evidence.append({"cell_id": cell["cell_id"], "levels": cell["levels"], "planned_replicas": cell["episodes_planned"],
                             "completed_replicas": len(complete),
                             "status_counts": dict(status_counts), "metric_summaries": summaries})
        launches = self.control.repo.list("launches", owner_id=owner, experiment_id=identifier)
        launches.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        return {**dimensions, "experiment": record, "cell_evidence": evidence,
                "retry_options": retry_options(launches, attempts),
                "launches": [self.launch(launch["id"], owner) for launch in launches]}

    @staticmethod
    def progress(attempts):
        grouped = defaultdict(list)
        for attempt in attempts:
            grouped[attempt.get("cell_id", "")].append(attempt)
        def counts(rows):
            return {"planned": len(rows), "completed": sum(row["status"] == "COMPLETED" for row in rows),
                    "failed": sum(row["status"] in {"FAILED", "UNREPORTED", "INTERRUPTED", "CANCELLED"} for row in rows)}
        return {**counts(attempts), "by_cell": [{"cell_id": key, **counts(rows)} for key, rows in sorted(grouped.items())]}

    def launch(self, identifier, owner):
        record = self.control.owned("launches", identifier, owner)
        if "live_logs" not in record and self.control.repo.backend == "gcp":
            record = {**record, "live_logs": False}
        detail = {}
        # The worker publishes a bounded, provisional view while it runs. The
        # canonical immutable result always wins once final evidence is ready.
        key = record.get("detail_key") or record.get("live_detail_key")
        if key:
            detail = json.loads(self.control.artifacts.get(key))
        attempts = detail.get("attempts")
        if attempts is None:
            attempts = [row for row in self.control.repo.list("attempts", owner_id=owner, experiment_id=record["experiment_id"]) if row["launch_id"] == identifier]
        for attempt in attempts:
            attempt.setdefault("episode_uid", None)
            attempt.setdefault("redis_stream", f"a2a:launch:{identifier}:episode:{attempt['episode_id']}")
        logs = detail.get("runner_logs", [])
        return {**record, "launch": record, "attempts": attempts,
                "progress": detail.get("progress") or record.get("progress") or self.progress(attempts),
                "runner_logs": logs, "provisional": bool(key and not record.get("detail_key")),
                "live_updated_at": record.get("live_updated_at"),
                "logs_truncated": bool(detail.get("logs_truncated", False)),
                "log_start_id": logs[0]["id"] if logs else None}

    def episodes(self, owner, query):
        allowed = {"experiment_id", "experiment_name", "cell_id", "environment_id", "episode_id", "release_id", "item_id", "status"}
        multiple = {"environment_id", "status"}
        filters = {}
        for key in allowed:
            values = query.getlist(key)
            if not values:
                continue
            if any(not value for value in values) or key not in multiple and len(values) > 1:
                raise ValueError(f"Invalid {key} filter")
            filters[key] = values if key in multiple else values[0]
        needle = query.get("q", "").strip().lower()
        if needle:
            filters["q"] = query["q"]
        limit = int(query.get("limit", "50"))
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = query.get("cursor", "0")
        if not cursor.isdigit():
            raise ValueError("cursor must be a non-negative integer")
        offset = int(cursor)
        rows = self.control.repo.list("episodes", owner_id=owner)
        rows.sort(key=lambda row: (row.get("created_at") or "", row.get("episode_uid") or ""), reverse=True)
        def matches(row, skip=None):
            for key, values in filters.items():
                if key in {"q", skip}:
                    continue
                if row.get(key) not in (values if isinstance(values, list) else [values]):
                    return False
            return not needle or all(word in " ".join(str(row.get(key) or "").lower() for key in ("episode_id", "experiment_name", "experiment_id", "environment_id")) for word in re.findall(r"\w+", needle))
        filtered = [row for row in rows if matches(row)]
        facets = {}
        for key, name in (("environment_id", "environments"), ("status", "statuses")):
            counts = Counter(row[key] for row in rows if row.get(key) and matches(row, skip=key))
            facets[name] = [{"value": value, "count": count} for value, count in sorted(counts.items())]
        return {"episodes": [{**row, "stopped": bool(row.get("stopped", row.get("status") == "STOPPED"))} for row in filtered[offset:offset + limit]],
                "next_cursor": str(offset + limit) if len(filtered) > offset + limit else None, "filters": filters, "facets": facets}

    def raw_episode(self, identifier, owner):
        row = self.control.owned("episodes", identifier, owner)
        return json.loads(self.control.artifacts.get(row["artifact_json_key"]))

    def episode_asset(self, identifier, digest, owner):
        from a2a_engine.content import verify_image
        from a2a_engine.episode_media import episode_image
        row = self.control.owned("episodes", identifier, owner)
        image = episode_image(self.raw_episode(identifier, owner), digest)
        payload = self.control.artifacts.get(f"launches/{row['launch_id']}/assets/{image.sha256}")
        return verify_image(image, payload), image.mime_type

    def episode(self, identifier, owner):
        trace = self.raw_episode(identifier, owner)
        config = trace.get("config") or {}
        participants = (config.get("provenance") or {}).get("participants") or pinned_participants(config)
        lanes = [{"participant_id": entry["participant_id"], "kind": entry.get("kind", "llm"), "binding": entry.get("binding"), "role": entry.get("role")} for entry in participants if isinstance(entry, dict) and entry.get("participant_id")]
        row = self.control.owned("episodes", identifier, owner)
        experiment = self.control.owned("experiments", row["experiment_id"], owner)
        with self.private_snapshot(experiment) as cp:
            roles = {entry["participant_id"]: entry.get("role") for entry in cp.participant_roster(experiment["id"])}
        configured = configured_roles(config)
        for lane in lanes:
            if not lane.get("role") and configured.get(lane["participant_id"]):
                lane["role"] = configured[lane["participant_id"]]
            if roles.get(lane["participant_id"]):
                lane["role"] = roles[lane["participant_id"]]
        index_label = None
        try:
            declaration = get_environment_spec(row["environment_id"]).declaration
            index_label = next((measure.index_label for measure in declaration.measures if measure.grain == "sequence" and measure.index_label), None)
        except (KeyError, AttributeError):
            pass
        return {**trace, "episode": trace, "lanes": lanes, "index_label": index_label, "cursor_max": len(trace.get("events") or [])}

    def artifacts(self, identifier, owner):
        episode = self.control.owned("episodes", identifier, owner)
        launch = self.control.owned("launches", episode["launch_id"], owner)
        launch = {**launch, "result_snapshot_key": launch.get("result_snapshot_key") or f"launches/{launch['id']}/trace.sqlite"}
        with self.private_snapshot(launch, "result_snapshot_key") as cp:
            values = [artifact.model_dump(mode="json") for artifact in SQLiteEpisodeStore(path=cp.path).get_derived_artifacts(identifier)]
        return {"episode_uid": identifier, "artifacts": values}

    def observability(self, identifier, owner):
        row = self.control.owned("episodes", identifier, owner)
        trace = self.raw_episode(identifier, owner)
        observability = trace.get("observability") or {}
        trace_id = observability.get("otel_trace_id")
        spans = []
        key = f"launches/{row['launch_id']}/results/otel-spans.jsonl"
        if trace_id and key in self.control.artifacts.keys(f"launches/{row['launch_id']}/results/"):
            for line in self.control.artifacts.get(key).decode().splitlines():
                try:
                    span = json.loads(line)
                except ValueError:
                    continue
                if (span.get("context") or {}).get("trace_id") == trace_id:
                    spans.append(span)
        spans.sort(key=lambda span: span.get("start_time") or "")
        return {"episode_uid": identifier, "observability": observability, "span_count": len(spans), "spans": spans}

    def stream(self, identifier, owner, projected=False):
        match = re.fullmatch(r"a2a:launch:([^:]+):episode:(.+)", identifier)
        if not match:
            raise KeyError("stream not found")
        launch_id, episode_id = match.groups()
        launch = self.launch(launch_id, owner)
        attempt = next((row for row in launch["attempts"] if row["episode_id"] == episode_id), None)
        if not attempt:
            raise KeyError("stream not found")
        entries = []
        for key in self.control.artifacts.keys(f"launches/{launch_id}/events/"):
            if len(entries) >= 5000:
                raise ValueError("Live event projection limit exceeded; download the experiment export")
            value = json.loads(self.control.artifacts.get(key))
            if value.get("episode_id") == episode_id:
                entries.append(value)
        if not projected:
            return {"stream": identifier, "events": entries, "available": bool(entries)}
        if not entries:
            raise KeyError("stream has no durable events yet")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("\n".join(json.dumps(entry) for entry in entries))
            trace = project_events_to_trace(path)
        trace.observability["event_sink"] = f"launches/{launch_id}/events/"
        return {**trace.model_dump(mode="json"), "projection": projection_summary(trace)}
