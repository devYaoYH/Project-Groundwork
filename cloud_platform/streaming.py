"""Private, resumable launch log streams backed by worker publications."""
from __future__ import annotations

import asyncio
import json

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from cloud_platform.storage import ACTIVE


def frame(kind, payload, identifier=None):
    prefix = f"id: {identifier}\n" if identifier is not None else ""
    return f"{prefix}event: {kind}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def launch_events(viewer, identifier, owner, cursor=0, *, request=None,
                        authenticate=None, polls=60, interval=2):
    # Short sessions fit the hosted request timeout; the client reconnects with
    # its durable cursor. No connection is tied to a particular worker instance.
    yield ": connected\nretry: 2000\n\n"
    previous_progress = previous_status = None
    for _ in range(polls):
        if request is not None and await request.is_disconnected():
            return
        if authenticate is not None:
            try:
                authenticate(request)
            except HTTPException:
                yield frame("auth.expired", {"error": "Sign in to continue"})
                return
        detail = await run_in_threadpool(viewer.launch, identifier, owner)
        logs = detail["runner_logs"]
        for event in logs:
            if int(event["id"]) > cursor:
                cursor = int(event["id"])
                yield frame("runner.log", event, cursor)
        progress = {key: detail.get(key) for key in
                    ("progress", "attempts", "provisional", "live_updated_at", "log_start_id", "logs_truncated")}
        encoded_progress = json.dumps(progress, sort_keys=True)
        if encoded_progress != previous_progress:
            yield frame("launch.progress", progress)
            previous_progress = encoded_progress
        status = json.dumps(detail["launch"], sort_keys=True)
        if status != previous_status:
            # Terminal status comes after the final logs and progress so the
            # client can safely close without dropping the last output.
            yield frame("launch.status", detail["launch"])
            previous_status = status
        if detail["launch"]["status"] not in ACTIVE:
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(interval)
