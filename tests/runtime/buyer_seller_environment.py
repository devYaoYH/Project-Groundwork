"""Real runner + LangGraph + MCP bargaining gate, with frozen local-loop parity."""

import asyncio
import json
import os
import runpy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from urllib.request import urlopen

from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.network import NetworkPolicy
from a2a_engine.remote.seats import write_descriptor
from a2a_engine.remote.server import EnvironmentApp, ManagedEpisode, RuntimeManager
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from buyer_seller.agents import BuyerAgent, SellerAgent
from buyer_seller.game import BuyerSellerGame
from buyer_seller.remote import BUYER_SELLER_TOOLS
from expt_runner.run_experiment import _make_run_context, _run_one
from local_stack.server import LocalStackHandler
from buyer_seller_fixture import CONFIG, MockProvider, digest


class ScopedManager(RuntimeManager):
    def __init__(self):
        network = NetworkPolicy(advertised_origin="http://runtime-environment:8000", allowed_origins=[
            "http://runtime-environment:8000", "http://runtime-agent:8001", "http://runtime-buyer:8001"])
        self.environment = EnvironmentApp(BUYER_SELLER_TOOLS, network=network)
        self.environment.registry.lifecycle.grace_s = 60
        self.io = LoopbackServer(self.environment.app, port=8000, bind_host="0.0.0.0", network=network)
        self.episodes, self.lock = set(), threading.RLock()

    def provision(self, config, *, scripted=False):
        episode = self.environment.registry.provision(uuid.uuid4().hex, [0, 1], join_timeout_s=60,
                                                      environment_id="buyer_seller")
        managed = ManagedEpisode(self, episode)
        with self.lock:
            self.episodes.add(managed)
        for seat in episode.seats.values():
            directory = Path(f"/provision/seat-{seat.seat}")
            os.chmod(directory, 0o700)
            managed.descriptors.append(write_descriptor(directory, episode, seat,
                f"{self.io.base_url}/episodes/{episode.episode_id}/join"))
        return managed


class CapturingAgent:
    def __init__(self, agent):
        self.agent, self.actions, self.observations = agent, [], []

    async def act(self, observation, tools):
        self.observations.append(deepcopy(observation))
        action = await self.agent.act(observation, tools)
        self.actions.append(deepcopy(action))
        return action


def main():
    status = {"healthy": True, "done": False, "error": None, "held": False}
    manager = ScopedManager()
    games = []
    original_build = BuyerSellerGame._build_agents

    def capture(game):
        original_build(game)
        games.append(game)

    BuyerSellerGame._build_agents = capture
    database = Path("/tmp/buyer-seller.db")
    LocalStackHandler.database, LocalStackHandler.workspace = database, Path("/workspace")

    async def health(request):
        return JSONResponse(status)

    async def release(request):
        Path("/provision/seat-0/release").touch()
        return JSONResponse({"released": True})

    manager.environment.app.routes.extend([Route("/health", health), Route("/status", health),
                                           Route("/release", release, methods=["POST"])])
    with manager:
        def run():
            try:
                expected_game = BuyerSellerGame(CONFIG, dry_run=True)
                clients = [MockProvider(), MockProvider()]
                if os.environ["BARGAIN_MODE"] == "mock":
                    expected_game.seller = SellerAgent(clients[0], cost=7.51, num_items=2, max_rounds=4,
                                                      discount_factor=.5, monotonic=True)
                    expected_game.buyer = BuyerAgent(clients[1], value=33.27, num_items=2, max_rounds=4,
                                                    discount_factor=.5)
                captures = [CapturingAgent(expected_game.seller), CapturingAgent(expected_game.buyer)]
                expected_game.seller, expected_game.buyer = captures
                original_loop = runpy.run_path("games/buyer-seller/tests/baseline.py")["original_loop"]
                expected = asyncio.run(original_loop(expected_game))
                games.clear()
                store = SQLiteEpisodeStore(path=database)

                def play(index):
                    context = _make_run_context("container-bargain", "cell", {
                        **CONFIG, "agents": [{"type": "scripted", "runtime": "external"}] * 2}, index, dry_run=True)
                    context.update(runtime_manager=manager, verify_readback=True)
                    return _run_one(context, store, Path("/tmp/bargain-results"))

                first = threading.Thread(target=play, args=(0,))
                first.start()
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    records = list(manager.environment.registry.lifecycle.records.values())
                    held = next((r for r in records if r.invocation.kind == "turn" and r.acceptance
                                 and r.recorder and len(r.recorder.records) == 1), None)
                    if held:
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError("held turn not reached")
                time.sleep(2.5)
                assert held.acceptance.status == "accepted" and not held.event.is_set()
                assert not held.recorder.closed and held.recorder.snapshot is None
                assert first.is_alive()
                with games[0].events._lock:
                    pending_events = list(games[0].events._events)
                assert [e.type for e in pending_events] == ["game_start", "round_start"]
                assert pending_events[-1].data["round"] == 1
                status["held"] = True
                first.join(45)
                assert not first.is_alive()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(play, [1, 2]))
                traces = list(store.iter_episodes())
                assert len(traces) == 3
                for trace in traces:
                    assert [(e.type, e.data) for e in trace.events] == [(e.type, e.data) for e in expected.events]
                    assert trace.final_state == expected.final_state and trace.metrics == expected.metrics
                    detail = LocalStackHandler._episode_detail(trace.episode_uid)
                    assert detail["episode"]["metrics"] == expected.metrics
                    assert detail["cursor_max"] == len(trace.events)
                records = list(manager.environment.registry.lifecycle.records.values())
                turns = [r for r in records if r.invocation.kind == "turn"]
                assert len(turns) == 6 * expected.final_state["rounds_used"]
                assert all(r.finished.status == "succeeded" and r.recorder.closed_by == "completed" and
                           len(r.recorder.snapshot) == 1 for r in turns)
                registrations = [r.invocation for r in records if r.invocation.kind == "register"]
                assert len(registrations) == 6
                assert all(("cost" in i.observation) == (i.seat == 0) and
                           ("value" in i.observation) == (i.seat == 1) for i in registrations)
                for host, seat in (("runtime-agent", 0), ("runtime-buyer", 1)):
                    audits = json.loads(urlopen(f"http://{host}:8001/audit", timeout=3).read())
                    assert len(audits) == 3
                    assert all(audit["private"] and audit["seat"] == seat and
                               audit["actions"] == digest(captures[seat].actions) and
                               audit["observations"] == digest(captures[seat].observations) and
                               audit["messages"] == digest(clients[seat].messages) for audit in audits)
                serialized = json.dumps([trace.model_dump(mode="json") for trace in traces])
                assert all(value not in serialized for value in ("join_ticket", "seat_secret", "capability",
                           "lifecycle_credential", "runtime-agent", "runtime-buyer"))
                assert not manager.episodes
                status.update(episodes=3, actions=len(turns), private=True, persisted=True,
                              workflow=LocalStackHandler._control().environment_workflow("buyer_seller")["status"])
            except Exception:
                status["error"] = "buyer_seller_gate_failed"
                raise
            finally:
                status["done"] = True

        threading.Thread(target=run, daemon=True).start()
        while True:
            time.sleep(.1)


if __name__ == "__main__":
    main()
