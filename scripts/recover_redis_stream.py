#!/usr/bin/env python3
"""Recover a partial trace artifact from an A2A Redis event stream.

This does not pretend a crashed episode completed successfully.  It preserves
the event evidence with ``stopped: true`` so a researcher can inspect/replay it
and decide whether to retry the episode.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from a2a_engine.redis_stream import RedisStreams, decode_stream_events
from a2a_engine.stream_projection import project_stream_to_trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--redis-url", default=os.environ.get("A2A_REDIS_URL"))
    parser.add_argument("--environment-name", help="only needed for legacy streams without environment_id")
    args = parser.parse_args(argv)
    if not args.redis_url:
        parser.error("--redis-url or A2A_REDIS_URL is required")
    decoded = decode_stream_events(RedisStreams(args.redis_url).xrange(args.stream))
    # One projection serves both the browser viewer and this recovery path, so
    # a recovered artifact and a live replay can never disagree about what the
    # stream said.
    trace = project_stream_to_trace(decoded, stream=args.stream, environment_id=args.environment_id)
    trace.observability["recovery"] = "redis_stream"
    trace.observability["recovered_at"] = datetime.now(UTC).isoformat()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
