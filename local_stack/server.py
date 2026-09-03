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
import threading
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from a2a_engine.registry import discover_environments, get_environment_spec
from a2a_engine.ratings import rebuild_rating_snapshot
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from a2a_engine.redis_stream import RedisStreams, decode_stream_events
from a2a_engine.stream_projection import project_stream_to_trace, projection_summary
from a2a_engine.design import DesignValidationError
try:  # Works both as ``python local_stack/server.py`` and as a package import.
    from local_stack.control_plane import ControlPlane, DesignDigestMismatch, OracleUnavailable
except ModuleNotFoundError:  # pragma: no cover - exercised by the Compose entrypoint
    from control_plane import ControlPlane, DesignDigestMismatch, OracleUnavailable


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
    # One database: the episode fact table and the control-plane dimensions
    # live in the same file, so every read below is a plain SQL query rather
    # than an application-side join across two of them.
    database = Path("/data/a2a.db")
    otel_file = Path("/data/otel-spans.jsonl")
    static_dir = Path("a2a-viewer")
    workspace = Path.cwd()
    _control_plane: ControlPlane | None = None
    _control_lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._json(self._health())
        if parsed.path == "/api/releases":
            control = self._control()
            by_game = control.available_experiments()
            return self._json({"releases": [
                {**release.__dict__, "experiments": by_game.get(release.environment_id, [])}
                for release in control.releases()
            ]})
        if parsed.path == "/api/environments":
            return self._json({"environments": self._control().environment_summaries()})
        if parsed.path.startswith("/api/environments/") and parsed.path.endswith("/items"):
            environment_id = unquote(
                parsed.path.removeprefix("/api/environments/").removesuffix("/items").rstrip("/")
            )
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
                cursor = query.get("cursor", [None])[0]
                return self._json(
                    self._control().environment_items(environment_id, limit=limit, cursor=cursor)
                )
            except (KeyError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path.startswith("/api/environments/"):
            environment_id = unquote(parsed.path.removeprefix("/api/environments/").rstrip("/"))
            try:
                return self._json(self._control().environment_detail(environment_id))
            except (KeyError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
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
        if parsed.path.startswith("/api/experiments/"):
            experiment_id = unquote(parsed.path.removeprefix("/api/experiments/").rstrip("/"))
            try:
                return self._json(self._control().experiment_detail(experiment_id))
            except KeyError:
                return self._json({"error": "experiment not found"}, 404)
        if parsed.path == "/api/launches":
            return self._json({"launches": [launch.__dict__ for launch in self._control().launches()]})
        if parsed.path.startswith("/api/launches/") and parsed.path.endswith("/events"):
            launch_id = unquote(parsed.path.removeprefix("/api/launches/").removesuffix("/events").rstrip("/"))
            return self._sse_launch_events(launch_id)
        if parsed.path.startswith("/api/launches/"):
            launch_id = unquote(parsed.path.removeprefix("/api/launches/"))
            try:
                return self._json(self._control().launch_detail(launch_id))
            except KeyError:
                return self._json({"error": "launch not found"}, 404)
        if parsed.path.startswith("/api/streams/") and parsed.path.endswith("/trace"):
            stream = unquote(parsed.path.removeprefix("/api/streams/").removesuffix("/trace"))
            payload, status = self._stream_trace(stream)
            return self._json(payload, status)
        if parsed.path.startswith("/api/streams/"):
            stream = unquote(parsed.path.removeprefix("/api/streams/"))
            return self._json(self._stream_events(stream))
        if parsed.path == "/api/episodes":
            try:
                return self._json(self._episode_page(parse_qs(parsed.query)))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        if parsed.path.startswith("/api/episodes/") and parsed.path.endswith("/observability"):
            episode_uid = unquote(parsed.path.removeprefix("/api/episodes/").removesuffix("/observability"))
            payload = self._observability(episode_uid)
            return self._json(payload if payload is not None else {"error": "trace not found"}, 200 if payload else 404)
        if parsed.path.startswith("/api/episodes/") and parsed.path.endswith("/artifacts"):
            episode_uid = unquote(parsed.path.removeprefix("/api/episodes/").removesuffix("/artifacts"))
            payload = self._artifacts(episode_uid)
            return self._json(payload if payload is not None else {"error": "trace not found"}, 200 if payload else 404)
        if parsed.path.startswith("/api/episodes/"):
            episode_uid = unquote(parsed.path.removeprefix("/api/episodes/"))
            trace = self._trace(episode_uid)
            return self._json(trace if trace is not None else {"error": "trace not found"}, 200 if trace else 404)
        if parsed.path == "/api/leaderboards":
            return self._json({
                "leaderboards": [{
                    "environment_id": "calendar",
                    "href": "/api/leaderboards/calendar",
                    "kind": "openskill",
                    "note": "Calendar is the only shipped environment with a rating-event adapter.",
                }],
            })
        if parsed.path == "/api/leaderboards/calendar":
            return self._json(self._calendar_leaderboard())
        self._static(parsed.path)

    def do_HEAD(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        try:
            body = self._request_json()
            if parsed.path.startswith("/api/environments/") and parsed.path.endswith("/oracle"):
                environment_id = unquote(
                    parsed.path.removeprefix("/api/environments/").removesuffix("/oracle").rstrip("/")
                )
                return self._json(
                    self._control().run_oracle(environment_id, str(body.get("item_id") or ""))
                )
            if parsed.path == "/api/experiments":
                experiment = self._control().create_experiment(
                    yaml_path=str(body["yaml_path"]) if body.get("yaml_path") else None,
                    release_id=body.get("release_id"),
                    name=body.get("name"),
                    design_text=str(body["design_text"]) if body.get("design_text") is not None else None,
                )
                return self._json(experiment.__dict__, 201)
            if parsed.path == "/api/designs/validate":
                return self._json(self._control().validate_design_text(
                    release_id=str(body.get("release_id") or ""),
                    design_text=str(body.get("design_text") or ""),
                ))
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith("/lock"):
                experiment_id = unquote(
                    parsed.path.removeprefix("/api/experiments/").removesuffix("/lock").rstrip("/")
                )
                experiment = self._control().lock_experiment(
                    experiment_id, design_sha256=str(body.get("design_sha256") or "")
                )
                return self._json(experiment.__dict__)
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith("/design"):
                experiment_id = unquote(
                    parsed.path.removeprefix("/api/experiments/").removesuffix("/design").rstrip("/")
                )
                experiment = self._control().update_experiment_design(
                    experiment_id, design_text=str(body.get("design_text") or "")
                )
                return self._json(experiment.__dict__, 201 if experiment.forked_from else 200)
            if parsed.path == "/api/launches":
                launch = self._control().launch_experiment(
                    str(body.get("experiment_id") or ""),
                    max_parallelism=int(body.get("max_parallelism") or 1),
                    smoke_test=bool(body.get("smoke_test", False)),
                    mode=str(body.get("mode") or "live"),
                    shard_index=(int(body["shard_index"]) if body.get("shard_index") is not None else None),
                    shard_count=(int(body["shard_count"]) if body.get("shard_count") is not None else None),
                )
                return self._json(launch.__dict__, 202)
            if parsed.path.startswith("/api/launches/") and parsed.path.endswith("/cancel"):
                launch_id = unquote(parsed.path.removeprefix("/api/launches/").removesuffix("/cancel").rstrip("/"))
                return self._json(self._control().cancel_launch(launch_id).__dict__)
        except OracleUnavailable as exc:
            return self._json({"error": str(exc)}, 409)
        except DesignDigestMismatch as exc:
            return self._json({"error": str(exc)}, 409)
        except DesignValidationError as exc:
            return self._json({"error": str(exc), "errors": [error.as_dict() for error in exc.errors]}, 400)
        except (KeyError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep Compose logs useful while avoiding a line for every static asset.
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    @classmethod
    def _store(cls) -> SQLiteEpisodeStore:
        return SQLiteEpisodeStore(path=cls.database)

    @classmethod
    def _control(cls) -> ControlPlane:
        if cls._control_plane is None or cls._control_plane.path != cls.database:
            with cls._control_lock:
                if cls._control_plane is None or cls._control_plane.path != cls.database:
                    # Constructing the control plane reconciles any launch left
                    # RUNNING by a previous process, so a restart resolves
                    # stranded launches instead of leaving them there forever.
                    control = ControlPlane(cls.database, workspace=cls.workspace)
                    control.seed_installed_releases()
                    cls._control_plane = control
        return cls._control_plane

    @classmethod
    def _health(cls) -> dict[str, object]:
        try:
            # Compose polls this every few seconds; counting rows must not
            # mean deserialising the whole corpus each time.
            count = cls._store().count_episodes()
            return {
                "ok": True,
                "database": "ready" if cls.database.exists() else "waiting for first run",
                "episode_count": count,
            }
        except Exception as exc:
            return {"ok": False, "database": "unreadable", "error": str(exc)}

    #: Query parameters that narrow the episode list. The store applies its own
    #: column whitelist on top, so an unknown key is dropped rather than
    #: interpolated; this list is what the browser is told it may ask for.
    EPISODE_FILTERS = (
        "experiment_id", "experiment_name", "cell_id", "environment_id",
        "episode_id", "release_id", "item_id", "status",
    )

    @classmethod
    def _episode_page(cls, query: dict[str, list[str]]) -> dict[str, object]:
        """One filtered, paginated page of the fact table.

        The unfiltered full scan this replaces deserialised every trace ever
        written on every request. Filters are pushed into SQL, where the
        promoted provenance columns are indexed.
        """
        filters = {
            key: query[key][0] for key in cls.EPISODE_FILTERS
            if query.get(key) and query[key][0] != ""
        }
        limit = int(query.get("limit", ["100"])[0])
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = query.get("cursor", [None])[0]
        episodes, next_cursor = cls._store().episode_summaries(
            filters, limit=limit, cursor=cursor,
        )
        return {"episodes": episodes, "next_cursor": next_cursor, "filters": filters}

    @classmethod
    def _trace(cls, episode_uid: str) -> dict[str, object] | None:
        trace = cls._store().get_episode(episode_uid)
        return trace.model_dump(mode="json") if trace is not None else None

    @classmethod
    def _observability(cls, episode_uid: str) -> dict[str, object] | None:
        """Return the local OTel projection correlated to one persisted trace.

        The SQLite trace remains the source of truth. JSONL is intentionally a
        local, append-only projection: an interrupted last line is ignored and
        a missing exporter simply yields no spans for an otherwise valid trace.
        """
        trace = cls._store().get_episode(episode_uid)
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
            "episode_uid": episode_uid,
            "observability": observability,
            "span_count": len(spans),
            "spans": spans,
        }

    @classmethod
    def _artifacts(cls, episode_uid: str) -> dict[str, object] | None:
        store = cls._store()
        if store.get_episode(episode_uid) is None:
            return None
        return {
            "episode_uid": episode_uid,
            "artifacts": [artifact.model_dump(mode="json") for artifact in store.get_derived_artifacts(episode_uid)],
        }

    @classmethod
    def _calendar_leaderboard(cls) -> dict[str, object]:
        try:
            discover_environments()
            adapter = get_environment_spec("calendar").rating_adapter
            if adapter is None:
                raise RuntimeError("Calendar does not register a rating adapter")
            materialization = rebuild_rating_snapshot(cls._store(), adapter)
        except Exception as exc:
            return {"environment_id": "calendar", "error": f"rating adapter unavailable: {exc}", "leaderboard": []}
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
        """Project a live stream into the trace shape a environment viewer consumes.

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

    def _sse_launch_events(self, launch_id: str) -> None:
        try:
            self._control().launch(launch_id)
        except KeyError:
            return self._json({"error": "launch not found"}, 404)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if self.command == "HEAD":
            return
        cursor = 0
        # Browsers reconnect automatically after this short bounded request;
        # SSE therefore needs no server-side client registry for local runs.
        for _ in range(80):
            events = self._control().events(launch_id, after_id=cursor)
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
        if self.command != "HEAD":
            self.wfile.write(body)

    # Directory names differ from environment names (``word_guess`` -> ``word-guess``),
    # so the served set is declared rather than derived.
    GAME_DIRS = ("calendar", "negotiation", "buyer-seller", "word-guess")

    def _game_root(self, relative: str, prefix: str, suffix: str) -> tuple[Path, str] | None:
        _, environment_id, *parts = relative.split("/")
        if environment_id not in self.GAME_DIRS:
            return None
        return (self.workspace / "games" / environment_id / suffix).resolve(), "/".join(parts)

    def _static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        if relative.startswith("game-replays/"):
            resolved = self._game_root(relative, "game-replays/", "replay")
        elif relative.startswith("game-assets/"):
            # The games ship their own viewers (Calendar's trace viewer,
            # Negotiation's web app). Serving each environment's directory lets a
            # replay page load that environment's real visualisation instead of a
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
        if path.is_dir():
            path = (path / "index.html").resolve()
            if root not in path.parents and path != root:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = None if self.command == "HEAD" else path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size if body is None else len(body)))
        self.end_headers()
        if body is not None:
            self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # ``--control-database`` is gone: the control plane and the episode store
    # are one file, which is what lets progress be a SQL join.
    parser.add_argument("--database", default="/data/a2a.db")
    parser.add_argument("--otel-file", default="/data/otel-spans.jsonl")
    parser.add_argument("--static-dir", default="a2a-viewer")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    LocalStackHandler.database = Path(args.database)
    LocalStackHandler.otel_file = Path(args.otel_file)
    LocalStackHandler.static_dir = Path(args.static_dir)
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
