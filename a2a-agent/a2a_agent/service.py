"""Private provisioning watcher for the local two-service /2 deployment."""

import argparse
import asyncio
import json
import os
import stat
import time
from pathlib import Path

from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.network import NetworkPolicy
from .mcp_client import execute
from .runtime_v2 import AgentRuntimeV2
from .scripted import ScriptedPolicy


class ProbePolicy(ScriptedPolicy):
    def __init__(self, directory, network):
        super().__init__()
        self.directory = directory
        self.network = network

    async def calls(self, invocation):
        if invocation.environment_id != "probe":
            return super().calls(invocation)
        if invocation.kind != "turn":
            return []
        await execute(invocation, [("env", "worker_probe", {}), ("env", "record_probe", {"value": "staged"})],
                      network=self.network)
        while not (self.directory / "release").exists():
            await asyncio.sleep(0.02)
        return []


def read_descriptor(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError("unsafe provisioning descriptor")
        descriptor = json.load(handle)
    if descriptor["expires_at"] <= time.time():
        raise ValueError("expired provisioning descriptor")
    return descriptor


async def watch(runtime, directory, callback_url):
    seen = set()
    while not runtime.stopping:
        existing = set(directory.glob("*.json"))
        seen.intersection_update(existing)
        for path in sorted(existing - seen):
            seen.add(path)
            try:
                descriptor = read_descriptor(path)
                policy = ProbePolicy(directory, runtime.network)
                if descriptor.get("environment_id") == "word_guess":
                    from word_guess.runtime_strategy import WordGuessStrategy
                    policy = WordGuessStrategy()
                await runtime.join(descriptor["join_url"], callback_url, descriptor["join_ticket"],
                                   policy=policy)
            except Exception:
                # Never expose admission bodies, ticket-bearing paths, URLs or provider errors.
                print("Agent provisioning rejected", flush=True)
        runtime._expire()
        await asyncio.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--environment-origin", required=True)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--provisioning-dir", type=Path, default=Path("/provision"))
    args = parser.parse_args()
    network = NetworkPolicy(advertised_origin=args.origin, allowed_origins=[args.origin, args.environment_origin])
    runtime = AgentRuntimeV2(network=network)
    with LoopbackServer(runtime.app, port=args.port, bind_host="0.0.0.0", network=network) as io:
        io.run(watch(runtime, args.provisioning_dir, args.origin), timeout=None)


if __name__ == "__main__":
    main()
