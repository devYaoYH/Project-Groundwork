"""Read-only wire verification of an existing launch's private SSE log stream.

Local: python deploy/verify_live_logs.py --launch-id EXISTING_ID --user Alice
Hosted: add --url https://HOST --cookie-file /private/operator-session.json

The optional operator-supplied JSON file maps cookie names to string values.
It must have mode 0600 (or more restrictive). This script never extracts browser
credentials, creates experiments, starts agents, or prints cookies/log contents.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
from urllib.parse import quote, urlsplit

import requests

ACTIVE = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLING"}


def frames(lines):
    """Decode SSE incrementally, retaining comment-only heartbeat frames."""
    item = {"comments": [], "data": []}
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line:
            if item["comments"] or item["data"] or "retry" in item:
                item["data"] = "\n".join(item["data"])
                yield item
            item = {"comments": [], "data": []}
            continue
        if line.startswith(":"):
            item["comments"].append(line[1:].lstrip(" "))
            continue
        key, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if key == "data":
            item["data"].append(value)
        elif key in {"event", "id", "retry"}:
            item[key] = value


def require_status(response, expected, check):
    if response.status_code != expected:
        raise RuntimeError(f"{check}: expected HTTP {expected}, received {response.status_code}")


def observe(session, url, path, *, cursor=None, query_cursor=False, seconds=10, first_frame_timeout=10):
    """Read network chunks immediately; never consume response.text/content."""
    headers = {"Accept": "text/event-stream", "Accept-Encoding": "identity"}
    if cursor is not None and not query_cursor:
        headers["Last-Event-ID"] = str(cursor)
    started = time.monotonic()
    with session.get(url + path, headers=headers, stream=True, allow_redirects=False,
                     timeout=(30, first_frame_timeout)) as response:
        require_status(response, 200, "private stream")
        if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
            raise RuntimeError("Stream does not use text/event-stream")
        cache = response.headers.get("Cache-Control", "").lower()
        if "no-store" not in cache or "no-transform" not in cache:
            raise RuntimeError("Stream does not disable storage and transformation")
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise RuntimeError("Stream is unexpectedly compressed")
        header_time = time.monotonic()
        events, identifiers, progress, terminal = [], [], False, False
        logs_after_snapshot = 0
        first_time = None
        status = None
        exhausted = True
        for item in frames(response.iter_lines(chunk_size=1)):
            now = time.monotonic()
            if first_time is None:
                first_time = now
                if "connected" not in item["comments"] or item.get("retry") != "2000":
                    raise RuntimeError("Stream did not immediately send its connection/retry frame")
                if now - header_time > first_frame_timeout:
                    raise RuntimeError("Initial SSE frame was buffered or delayed")
            kind = item.get("event")
            if kind:
                try:
                    payload = json.loads(item["data"])
                except (ValueError, TypeError) as error:
                    raise RuntimeError("Stream contains invalid JSON event data") from error
                events.append(kind)
                if kind == "auth.expired":
                    raise RuntimeError("Operator session expired during stream verification")
                if kind == "runner.log":
                    try:
                        identifier = int(item["id"])
                    except (KeyError, ValueError) as error:
                        raise RuntimeError("Runner log has no numeric resume identifier") from error
                    if not isinstance(payload, dict) or int(payload.get("id", -1)) != identifier:
                        raise RuntimeError("Runner log payload and SSE identifier disagree")
                    if identifier <= (identifiers[-1] if identifiers else (cursor or 0)):
                        raise RuntimeError("Runner logs replayed duplicates or arrived out of order")
                    identifiers.append(identifier)
                    if progress:
                        logs_after_snapshot += 1
                elif kind == "launch.progress":
                    if not isinstance(payload, dict) or not {"progress", "attempts", "provisional", "live_updated_at", "log_start_id"} <= payload.keys():
                        raise RuntimeError("Launch progress event lacks its documented fields")
                    progress = True
                elif kind == "launch.status":
                    status = payload.get("status") if isinstance(payload, dict) else None
                    if not isinstance(status, str) or not status:
                        raise RuntimeError("Launch status event lacks a valid status")
                    if not progress:
                        raise RuntimeError("Launch status arrived before its progress snapshot")
                    terminal = status not in ACTIVE
            if now - header_time >= seconds and not terminal:
                exhausted = False
                break
        if first_time is None or not progress or status is None:
            raise RuntimeError("Stream ended without its connection, progress, and status frames")
        if terminal and (not exhausted or events[-1] != "launch.status"):
            raise RuntimeError("Terminal status did not close the stream after final evidence")
        return {"status": status, "terminal_closed": terminal and exhausted,
                "log_ids": identifiers, "event_count": len(events),
                "logs_after_initial_snapshot": logs_after_snapshot,
                "first_frame_after_headers_seconds": round(first_time - header_time, 3),
                "connection_seconds": round(header_time - started, 3)}


def verify_launch(url, launch_id, owner, *, other_owner=None, seconds=10,
                  first_frame_timeout=10):
    """Reuse explicit authenticated sessions; every request is a read."""
    url = url.rstrip("/")
    path = f"/api/launches/{quote(launch_id, safe='')}/events"
    with requests.Session() as anonymous:
        with anonymous.get(url + path, timeout=30, allow_redirects=False, stream=True) as response:
            require_status(response, 401, "anonymous stream denial")
    if other_owner is not None:
        with other_owner.get(url + path, timeout=30, allow_redirects=False, stream=True) as response:
            require_status(response, 404, "cross-owner stream denial")
    initial = observe(owner, url, path, seconds=seconds, first_frame_timeout=first_frame_timeout)
    if not initial["log_ids"]:
        raise RuntimeError("Selected launch has no published logs; choose one with recorded output")
    # Replay a suffix rather than only the empty tail, proving the resume cursor
    # affects actual delivered logs on a completed launch as well as a live one.
    cursor = initial["log_ids"][len(initial["log_ids"]) // 2]
    expected = set(identifier for identifier in initial["log_ids"] if identifier > cursor)
    resumed = observe(owner, url, path, cursor=cursor, seconds=seconds,
                      first_frame_timeout=first_frame_timeout)
    query = observe(owner, url, path + f"?after_id={cursor}", cursor=cursor, query_cursor=True, seconds=seconds,
                    first_frame_timeout=first_frame_timeout)
    if initial["terminal_closed"]:
        if set(resumed["log_ids"]) != expected or set(query["log_ids"]) != expected:
            raise RuntimeError("Completed launch cursor replay did not return the exact remaining logs")
        if not resumed["terminal_closed"] or not query["terminal_closed"]:
            raise RuntimeError("Completed launch resumed streams did not close")
    return {"launch_id": launch_id, "status": initial["status"],
            "initial_log_count": len(initial["log_ids"]), "resume_cursor": cursor,
            "header_resume_log_count": len(resumed["log_ids"]),
            "query_resume_log_count": len(query["log_ids"]),
            "first_frame_after_headers_seconds": initial["first_frame_after_headers_seconds"],
            "terminal_closed": initial["terminal_closed"], "anonymous_denial": "passed",
            "cross_owner_denial": "passed" if other_owner is not None else "not exercised",
            "new_experiments_or_model_calls": 0,
            "logs_after_initial_snapshot": initial["logs_after_initial_snapshot"],
            "live_arrival": "observed" if initial["logs_after_initial_snapshot"] else "not exercised"}


def cookie_session(path):
    path = Path(path)
    with path.open() as source:
        metadata = os.fstat(source.fileno())
        if stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > 65536:
            raise RuntimeError("Cookie file must be private (mode 0600 or stricter) and at most 64 KiB")
        cookies = json.load(source)
    if not isinstance(cookies, dict) or not cookies or not all(
            isinstance(name, str) and isinstance(value, str) and name and value
            for name, value in cookies.items()):
        raise RuntimeError("Cookie file must map cookie names to nonempty strings")
    session = requests.Session()
    session.cookies.update(cookies)
    return session


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--launch-id", required=True)
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--user", default=None, help="Local development user only")
    auth.add_argument("--cookie-file", help="Private operator-supplied JSON cookie map")
    parser.add_argument("--other-cookie-file", help="Optional second owner's private cookie map")
    parser.add_argument("--seconds", type=int, default=10, help="Maximum observation per active connection")
    parser.add_argument("--first-frame-timeout", type=int, default=10)
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        parser.error("URL must be an origin without credentials, path, query, or fragment")
    if parsed.scheme not in {"http", "https"} or not local and parsed.scheme != "https":
        parser.error("Hosted verification requires HTTPS")
    if not 1 <= args.seconds <= 120 or not 1 <= args.first_frame_timeout <= 30:
        parser.error("seconds must be 1..120 and first-frame-timeout 1..30")
    if not local and not args.cookie_file:
        parser.error("Hosted verification requires an explicitly supplied private cookie file")
    try:
        if args.cookie_file:
            owner = cookie_session(args.cookie_file)
        else:
            from verify_local import local_session
            owner, _ = local_session(args.url, args.user or "Alice")
        other = cookie_session(args.other_cookie_file) if args.other_cookie_file else None
        try:
            result = verify_launch(args.url, args.launch_id, owner, other_owner=other,
                                   seconds=args.seconds, first_frame_timeout=args.first_frame_timeout)
            print(json.dumps(result, indent=2))
        finally:
            owner.close()
            if other is not None:
                other.close()
        return 0
    except RuntimeError as error:
        print(f"Live log verification failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, requests.RequestException) as error:
        # Never include HTTP bodies, request headers, cookies, or raw log output.
        print(f"Live log verification failed ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
