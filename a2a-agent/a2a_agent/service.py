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
from a2a_engine.remote.joinlink import decode_join_link, FIELDS
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
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            raise ValueError("unsafe provisioning descriptor")
        payload = handle.read(16385)
        if len(payload.encode()) > 16384:
            raise ValueError("oversized provisioning descriptor")
        descriptor = json.loads(payload)
    if "join_link" in descriptor:
        decoded = decode_join_link(descriptor["join_link"])
        if any(key in descriptor and descriptor[key] != decoded[key] for key in FIELDS):
            raise ValueError("inconsistent provisioning descriptor")
        descriptor.update(decoded)
    if descriptor["expires_at"] <= time.time():
        raise ValueError("expired provisioning descriptor")
    return descriptor


def descriptor_policy(descriptor, directory, network):
    if "policy" not in descriptor and descriptor["environment_id"] in {"calendar", "probe"}:
        return ProbePolicy(directory, network)
    config = descriptor.get("policy", {"type": "scripted"})
    if not isinstance(config, dict) or set(config) - {"type", "model", "api_base", "api_format", "temperature", "max_tokens", "capabilities"}:
        raise ValueError("unsupported agent policy")
    environment_id = descriptor["environment_id"]
    kind = config.get("type", "scripted")
    if kind not in {"scripted", "heuristic", "llm"} or (kind == "heuristic" and environment_id != "negotiation"):
        raise ValueError("unsupported agent strategy")
    model = None
    if environment_id == "negotiation":
        from negotiation_game.runtime_strategy import NegotiationStrategy
        return NegotiationStrategy(config=config if kind == "llm" else None)
    if kind == "llm":
        from a2a_engine.llm.factory import make_llm_client
        from a2a_engine.remote.compose import MODEL_FIELDS
        if not config.get("model"):
            raise ValueError("provider strategy requires a model")
        model = make_llm_client({"temperature": 0.0, **{key: value for key, value in config.items() if key in MODEL_FIELDS and key != "type"}})
    if environment_id == "calendar":
        if model is not None:
            from .strategies import StructuredOutputStrategy
            return StructuredOutputStrategy(model)
        return ScriptedPolicy()
    if environment_id == "word_guess":
        from word_guess.runtime_strategy import WordGuessStrategy
        return WordGuessStrategy(client=model)
    if environment_id == "buyer_seller":
        from buyer_seller.runtime_strategy import BuyerSellerStrategy
        return BuyerSellerStrategy(client=model)
    if environment_id == "tangram_reference":
        from tangram_reference.runtime_strategy import TangramReferenceStrategy
        return TangramReferenceStrategy(client=model)
    if environment_id == "probe":
        return ProbePolicy(directory, network)
    raise ValueError("unsupported environment strategy")


async def watch(runtime, directory, callback_url):
    seen = set()
    sessions = {}
    while not runtime.stopping:
        existing = set(directory.glob("*.json"))
        for path in list(sessions):
            if path not in existing:
                await runtime.close_session(sessions.pop(path))
        seen.intersection_update(existing)
        for path in sorted(existing - seen):
            seen.add(path)
            try:
                descriptor = read_descriptor(path)
                policy = descriptor_policy(descriptor, directory, runtime.network)
                admission = await runtime.join(descriptor["join_url"], callback_url, descriptor["join_ticket"], policy=policy)
                sessions[path] = (admission.environment_id, admission.episode_id, admission.attempt_id, admission.seat)
            except Exception:
                # Never expose admission bodies, ticket-bearing paths, URLs or provider errors.
                print("Agent provisioning rejected", flush=True)
        runtime._expire()
        await asyncio.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--environment-origin", action="append", required=True)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--provisioning-dir", type=Path, default=Path("/provision"))
    args = parser.parse_args()
    origins = []
    for origin in args.environment_origin:
        if origin.endswith(":ports"):
            origins.extend(f"{origin.removesuffix(':ports')}:{port}" for port in range(8100, 8132))
        else:
            origins.append(origin)
    network = NetworkPolicy(advertised_origin=args.origin, allowed_origins=[args.origin, *origins])
    runtime = AgentRuntimeV2(network=network)
    with LoopbackServer(runtime.app, port=args.port, bind_host="0.0.0.0", network=network) as io:
        io.run(watch(runtime, args.provisioning_dir, args.origin), timeout=None)


if __name__ == "__main__":
    main()
