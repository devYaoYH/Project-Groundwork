"""Small Redis Streams transport for local event recovery and replay.

It purposely implements only the RESP commands this project needs (`XADD` and
`XRANGE`) so the local runner does not gain a mandatory Redis Python dependency.
Redis is an operational event log: completed episodes and manifests remain the
canonical research record.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Event as ThreadEvent, Thread
from typing import Any
from urllib.parse import unquote, urlparse

from a2a_engine.schemas import Event


def _encode_command(parts: list[str]) -> bytes:
    output = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        encoded = str(part).encode("utf-8")
        output.extend((f"${len(encoded)}\r\n".encode(), encoded, b"\r\n"))
    return b"".join(output)


class RedisProtocolError(RuntimeError):
    pass


class RedisStreams:
    """Minimal, short-lived Redis connection client suitable for a local stack."""

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("Redis URL must be redis://host:port/db")
        self.host = parsed.hostname
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").removeprefix("/") or "0")
        self.password = unquote(parsed.password) if parsed.password else None
        self.ssl = parsed.scheme == "rediss"

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        args = ["XADD", stream, "*"]
        for key, value in fields.items():
            args.extend((key, value))
        value = self._command(args)
        return str(value)

    def xrange(self, stream: str, *, start: str = "-", end: str = "+") -> list[dict[str, Any]]:
        raw = self._command(["XRANGE", stream, start, end])
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[1], list):
                continue
            fields = {str(entry[1][index]): str(entry[1][index + 1]) for index in range(0, len(entry[1]) - 1, 2)}
            entries.append({"id": str(entry[0]), "fields": fields})
        return entries

    def expire(self, key: str, seconds: int) -> bool:
        return bool(self._command(["EXPIRE", key, str(seconds)]))

    def _command(self, args: list[str]) -> Any:
        sock = socket.create_connection((self.host, self.port), timeout=2)
        try:
            if self.ssl:
                import ssl
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            reader = sock.makefile("rb")
            if self.password:
                self._write(sock, reader, ["AUTH", self.password])
            if self.db:
                self._write(sock, reader, ["SELECT", str(self.db)])
            return self._write(sock, reader, args)
        finally:
            sock.close()

    @staticmethod
    def _write(sock: socket.socket, reader, args: list[str]) -> Any:
        sock.sendall(_encode_command(args))
        return _read_reply(reader)


def _read_reply(reader) -> Any:
    prefix = reader.read(1)
    if not prefix:
        raise RedisProtocolError("Redis closed the connection")
    line = reader.readline().rstrip(b"\r\n")
    if prefix == b"+":
        return line.decode()
    if prefix == b"-":
        raise RedisProtocolError(line.decode())
    if prefix == b":":
        return int(line)
    if prefix == b"$":
        size = int(line)
        if size < 0:
            return None
        payload = reader.read(size)
        reader.read(2)
        return payload.decode()
    if prefix == b"*":
        size = int(line)
        if size < 0:
            return None
        return [_read_reply(reader) for _ in range(size)]
    raise RedisProtocolError(f"unknown Redis RESP prefix {prefix!r}")


@dataclass(frozen=True)
class EventStreamConfig:
    stream: str
    episode_id: str
    environment_id: str
    launch_id: str | None = None
    schema_version: int = 1


class RedisEventPublisher:
    """Background, best-effort publisher that never interrupts environment execution."""

    def __init__(self, redis_url: str, config: EventStreamConfig) -> None:
        self._redis = RedisStreams(redis_url)
        self.config = config
        self._queue: Queue[Event | None] = Queue()
        self._stopped = ThreadEvent()
        self._worker = Thread(target=self._run, name="a2a-redis-event-publisher", daemon=True)
        self._worker.start()

    def publish(self, event: Event) -> None:
        if not self._stopped.is_set():
            self._queue.put(event)

    def flush(self, timeout: float = 2.0) -> None:
        deadline = __import__("time").monotonic() + timeout
        while not self._queue.empty() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)

    def close(self) -> None:
        self.flush()
        self._stopped.set()
        self._queue.put(None)
        self._worker.join(timeout=1)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                event = self._queue.get(timeout=0.1)
            except Empty:
                continue
            if event is None:
                return
            payload = event.model_dump(mode="json")
            try:
                self._redis.xadd(self.config.stream, {
                    "schema_version": str(self.config.schema_version),
                    "episode_id": self.config.episode_id,
                    "launch_id": self.config.launch_id or "",
                    "environment_id": self.config.environment_id,
                    "event": json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    "published_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                # The runner must retain the trace path even when an optional
                # operational stream is unavailable.  It can be reconstructed
                # from the final trace on successful completion.
                pass


def publisher_from_config(config: Any) -> RedisEventPublisher | None:
    """Build a publisher from resolved run config without persisting Redis credentials."""
    data = getattr(config, "model_extra", None) or {}
    stream = data.get("event_stream") if isinstance(data, dict) else None
    if not isinstance(stream, dict) or not stream.get("stream") or not stream.get("episode_id"):
        return None
    import os
    url = os.environ.get("A2A_REDIS_URL")
    if not url:
        return None
    return RedisEventPublisher(url, EventStreamConfig(
        stream=str(stream["stream"]), episode_id=str(stream["episode_id"]),
        environment_id=str(getattr(config, "environment_id")),
        launch_id=str(stream["launch_id"]) if stream.get("launch_id") else None,
    ))


def decode_stream_events(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return validated event payloads for a browser/API replay projection."""
    decoded: list[dict[str, Any]] = []
    for entry in entries:
        try:
            event = Event.model_validate_json(entry["fields"]["event"])
        except Exception:
            continue
        decoded.append({
            "stream_id": entry["id"], "episode_id": entry["fields"].get("episode_id"),
            "environment_id": entry["fields"].get("environment_id"),
            "event": event.model_dump(mode="json"),
        })
    return decoded
