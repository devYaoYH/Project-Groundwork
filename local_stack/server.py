#!/usr/bin/env python3
"""Serve the local A2A corpus, generic trace viewer, and Calendar leaderboard.

HTTP uses only the standard library. Trace, artifact, and rating access goes
through the engine's SQLite store so the local control plane exercises the same
contracts as a later shared-storage deployment.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from a2a_engine.registry import discover_games, get_game_spec
from a2a_engine.ratings import rebuild_rating_snapshot
from a2a_engine.storage.sqlite import SQLiteTraceStore
from a2a_engine.redis_stream import RedisStreams, decode_stream_events
from a2a_engine.stream_projection import project_stream_to_trace, projection_summary
try:  # Works both as ``python local_stack/server.py`` and as a package import.
    from local_stack.control_plane import ControlPlane
except ModuleNotFoundError:  # pragma: no cover - exercised by the Compose entrypoint
    from control_plane import ControlPlane


def sse_frame(event: dict) -> str:
    """Render one control-plane event as an *unnamed* SSE frame.

    A named frame (``event: <kind>``) is delivered only to a listener
    registered for that exact name, so naming these would mean every newly
    added control-plane event kind is silently dropped by existing clients
    until someone remembers to extend a whitelist. The kind travels inside the
    payload instead, and consumers filter on it.
    """
    return f"id: {int(event['id'])}\ndata: {json.dumps(event)}\n\n"


class LocalStackHandler(BaseHTTPRequestHandler):
    database = Path("/data/a2a_traces.db")
    otel_file = Path("/data/otel-spans.jsonl")
    static_dir = Path("a2a-viewer")
    control_database = Path("/data/a2a_control.db")
    workspace = Path.cwd()
    _control_plane: ControlPlane | None = None

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._json(self._health())
        if parsed.path == "/api/releases":
            control = self._control()
            by_game = control.available_experiments()
            return self._json({"releases": [
                {**release.__dict__, "experiments": by_game.get(release.game_name, [])}
                for release in control.releases()
            ]})
        if parsed.path == "/api/agent-pool":
            return self._json(self._control().agent_pool())
        if parsed.path == "/api/experiment-config":
            wanted = parse_qs(parsed.query).get("path", [""])[0]
            try:
                control = self._control()
                payload = control.read_experiment_config(wanted)
                payload.update(control.experiment_agents(wanted))
                return self._json(payload)
            except (ValueError, OSError, KeyError) as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path == "/api/experiments":
            return self._json({"experiments": [experiment.__dict__ for experiment in self._control().experiments()]})
        if parsed.path == "/api/rollouts":
            return self._json({"rollouts": [rollout.__dict__ for rollout in self._control().rollouts()]})
        if parsed.path.startswith("/api/rollouts/") and parsed.path.endswith("/events"):
            rollout_id = unquote(parsed.path.removeprefix("/api/rollouts/").removesuffix("/events").rstrip("/"))
            return self._sse_rollout_events(rollout_id)
        if parsed.path.startswith("/api/rollouts/"):
            rollout_id = unquote(parsed.path.removeprefix("/api/rollouts/"))
            try:
                return self._json(self._control().rollout_detail(rollout_id))
            except KeyError:
                return self._json({"error": "rollout not found"}, 404)
        if parsed.path.startswith("/api/streams/") and parsed.path.endswith("/trace"):
            stream = unquote(parsed.path.removeprefix("/api/streams/").removesuffix("/trace"))
            payload, status = self._stream_trace(stream)
            return self._json(payload, status)
        if parsed.path.startswith("/api/streams/"):
            stream = unquote(parsed.path.removeprefix("/api/streams/"))
            return self._json(self._stream_events(stream))
        if parsed.path == "/api/traces":
            return self._json({"traces": self._trace_summaries()})
        if parsed.path.startswith("/api/traces/") and parsed.path.endswith("/observability"):
            game_id = unquote(parsed.path.removeprefix("/api/traces/").removesuffix("/observability"))
            payload = self._observability(game_id)
            return self._json(payload if payload is not None else {"error": "trace not found"}, 200 if payload else 404)
        if parsed.path.startswith("/api/traces/") and parsed.path.endswith("/artifacts"):
            game_id = unquote(parsed.path.removeprefix("/api/traces/").removesuffix("/artifacts"))
            payload = self._artifacts(game_id)
            return self._json(payload if payload is not None else {"error": "trace not found"}, 200 if payload else 404)
        if parsed.path.startswith("/api/traces/"):
            game_id = unquote(parsed.path.removeprefix("/api/traces/"))
            trace = self._trace(game_id)
            return self._json(trace if trace is not None else {"error": "trace not found"}, 200 if trace else 404)
        if parsed.path == "/api/leaderboards":
            return self._json({
                "leaderboards": [{
                    "game_name": "calendar",
                    "href": "/api/leaderboards/calendar",
                    "kind": "openskill",
                    "note": "Calendar is the only shipped game with a rating-event adapter.",
                }],
            })
        if parsed.path == "/api/leaderboards/calendar":
            return self._json(self._calendar_leaderboard())
        self._static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        try:
            body = self._request_json()
            if parsed.path == "/api/experiments":
                experiment = self._control().create_experiment(
                    yaml_path=str(body.get("yaml_path") or ""),
                    release_id=body.get("release_id"),
                    name=body.get("name"),
                )
                return self._json(experiment.__dict__, 201)
            if parsed.path == "/api/rollouts":
                rollout = self._control().launch_rollout(
                    str(body.get("experiment_id") or ""),
                    max_parallelism=int(body.get("max_parallelism") or 1),
                    smoke_test=bool(body.get("smoke_test", False)),
                )
                return self._json(rollout.__dict__, 202)
            if parsed.path.startswith("/api/rollouts/") and parsed.path.endswith("/cancel"):
                rollout_id = unquote(parsed.path.removeprefix("/api/rollouts/").removesuffix("/cancel").rstrip("/"))
                return self._json(self._control().cancel_rollout(rollout_id).__dict__)
        except (KeyError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep Compose logs useful while avoiding a line for every static asset.
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    @classmethod
    def _store(cls) -> SQLiteTraceStore:
        return SQLiteTraceStore(path=cls.database)

    @classmethod
    def _control(cls) -> ControlPlane:
        if cls._control_plane is None or cls._control_plane.path != cls.control_database:
            cls._control_plane = ControlPlane(
                cls.control_database, workspace=cls.workspace, trace_database=cls.database,
            )
        return cls._control_plane

    @classmethod
    def _health(cls) -> dict[str, object]:
        try:
            count = sum(1 for _ in cls._store().iter_traces())
            return {
                "ok": True,
                "database": "ready" if cls.database.exists() else "waiting for first run",
                "trace_count": count,
            }
        except Exception as exc:
            return {"ok": False, "database": "unreadable", "error": str(exc)}

    @classmethod
    def _trace_summaries(cls) -> list[dict[str, object]]:
        traces = list(cls._store().iter_traces())
        return [{
            "game_id": trace.game_id,
            "game_name": trace.config.game_name,
            "experiment_name": trace.config.experiment_name,
            "experiment_run_id": trace.config.experiment_run_id,
            "environment_id": trace.environment.id if trace.environment else None,
            "environment_revision": trace.environment.revision if trace.environment else None,
            "episode_id": trace.episode.id if trace.episode else None,
            "batch_label": trace.config.extra.get("batch_label"),
            "run_idx": trace.config.extra.get("run_idx"),
            "started_at": trace.started_at,
            "ended_at": trace.ended_at,
            "stopped": trace.stopped,
            "metrics": trace.metrics,
            "created_at": trace.started_at,
        } for trace in reversed(traces)]

    @classmethod
    def _trace(cls, game_id: str) -> dict[str, object] | None:
        trace = cls._store().get_trace(game_id)
        return trace.model_dump(mode="json") if trace is not None else None

    @classmethod
    def _observability(cls, game_id: str) -> dict[str, object] | None:
        """Return the local OTel projection correlated to one persisted trace.

        The SQLite trace remains the source of truth. JSONL is intentionally a
        local, append-only projection: an interrupted last line is ignored and
        a missing exporter simply yields no spans for an otherwise valid trace.
        """
        trace = cls._store().get_trace(game_id)
        if trace is None:
            return None
        observability = dict(trace.observability)
        trace_id = observability.get("otel_trace_id")
        spans: list[dict[str, object]] = []
        if trace_id and cls.otel_file.is_file():
            with cls.otel_file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        span = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    context = span.get("context") if isinstance(span, dict) else None
                    if isinstance(context, dict) and context.get("trace_id") == trace_id:
                        spans.append(span)
        spans.sort(key=lambda span: str(span.get("start_time") or ""))
        return {
            "game_id": game_id,
            "observability": observability,
            "span_count": len(spans),
            "spans": spans,
        }

    @classmethod
    def _artifacts(cls, game_id: str) -> dict[str, object] | None:
        store = cls._store()
        if store.get_trace(game_id) is None:
            return None
        return {
            "game_id": game_id,
            "artifacts": [artifact.model_dump(mode="json") for artifact in store.get_derived_artifacts(game_id)],
        }

    @classmethod
    def _calendar_leaderboard(cls) -> dict[str, object]:
        try:
            discover_games()
            adapter = get_game_spec("calendar").rating_adapter
            if adapter is None:
                raise RuntimeError("Calendar does not register a rating adapter")
            materialization = rebuild_rating_snapshot(cls._store(), adapter)
        except Exception as exc:
            return {"game_name": "calendar", "error": f"rating adapter unavailable: {exc}", "leaderboard": []}
        payload = materialization.snapshot.model_dump(mode="json")
        # ``RatingSnapshot`` deliberately stores canonical player state. The
        # browser needs its derived, sorted projection as well.
        payload["leaderboard"] = materialization.snapshot.leaderboard()
        return payload

    @staticmethod
    def _stream_events(stream: str) -> dict[str, object]:
        url = os.environ.get("A2A_REDIS_URL")
        if not url:
            return {"stream": stream, "events": [], "available": False,
                    "error": "Redis is not configured for this local stack"}
        try:
            entries = RedisStreams(url).xrange(stream)
            return {"stream": stream, "events": decode_stream_events(entries), "available": True}
        except Exception as exc:
            return {"stream": stream, "events": [], "available": False, "error": str(exc)}

    @classmethod
    def _stream_trace(cls, stream: str) -> tuple[dict[str, object], int]:
        """Project a live stream into the trace shape a game viewer consumes.

        An episode still running has no persisted trace, so this is what lets
        one viewer render a finished and an in-flight episode the same way.
        """
        events = cls._stream_events(stream)
        if not events.get("available"):
            return {"error": events.get("error", "stream unavailable")}, 503
        try:
            trace = project_stream_to_trace(events["events"], stream=stream)
        except ValueError as exc:
            return {"error": str(exc)}, 404
        payload = trace.model_dump(mode="json")
        payload["projection"] = projection_summary(trace)
        return payload, 200

    def _sse_rollout_events(self, rollout_id: str) -> None:
        try:
            self._control().rollout(rollout_id)
        except KeyError:
            return self._json({"error": "rollout not found"}, 404)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cursor = 0
        # Browsers reconnect automatically after this short bounded request;
        # SSE therefore needs no server-side client registry for local runs.
        for _ in range(80):
            events = self._control().events(rollout_id, after_id=cursor)
            try:
                for event in events:
                    cursor = int(event["id"])
                    self.wfile.write(sse_frame(event).encode())
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.25)

    def _request_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, indent=2, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Directory names differ from game names (``word_guess`` -> ``word-guess``),
    # so the served set is declared rather than derived.
    GAME_DIRS = ("calendar", "negotiation", "buyer-seller", "word-guess")

    def _game_root(self, relative: str, prefix: str, suffix: str) -> tuple[Path, str] | None:
        _, game_name, *parts = relative.split("/")
        if game_name not in self.GAME_DIRS:
            return None
        return (self.workspace / "games" / game_name / suffix).resolve(), "/".join(parts)

    def _static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        if relative.startswith("game-replays/"):
            resolved = self._game_root(relative, "game-replays/", "replay")
        elif relative.startswith("game-assets/"):
            # The games ship their own viewers (Calendar's trace viewer,
            # Negotiation's web app). Serving each game's directory lets a
            # replay page load that game's real visualisation instead of a
            # generic event dump.
            resolved = self._game_root(relative, "game-assets/", "")
        else:
            resolved = None

        if relative.startswith(("game-replays/", "game-assets/")):
            if resolved is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            root, relative = resolved
            relative = relative or "index.html"
        else:
            root = self.static_dir.resolve()
        path = (root / relative).resolve()
        if root not in path.parents and path != root:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/data/a2a_traces.db")
    parser.add_argument("--otel-file", default="/data/otel-spans.jsonl")
    parser.add_argument("--static-dir", default="a2a-viewer")
    parser.add_argument("--control-database", default="/data/a2a_control.db")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    LocalStackHandler.database = Path(args.database)
    LocalStackHandler.otel_file = Path(args.otel_file)
    LocalStackHandler.static_dir = Path(args.static_dir)
    LocalStackHandler.control_database = Path(args.control_database)
    LocalStackHandler.workspace = Path(args.workspace).resolve()
    LocalStackHandler._control_plane = None
    server = ThreadingHTTPServer((args.host, args.port), LocalStackHandler)
    print(f"Local A2A stack: http://{args.host}:{args.port}  database={args.database}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
