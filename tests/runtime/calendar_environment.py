"""Real Calendar subgraphs and frozen-driver parity over isolated /2 MCP services."""

import copy
import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.remote.network import NetworkPolicy
from a2a_engine.remote.seats import write_descriptor
from a2a_engine.remote.server import EnvironmentApp, ManagedEpisode, RuntimeManager
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from calendar_game.game import CalendarGame
from calendar_game.remote import CALENDAR_TOOLS
from calendar_game.tests._legacy_workflow_oracle import LegacyCalendarGame
from calendar_game.tests.test_remote_parity import normalized_events
from local_stack.server import LocalStackHandler


class ScopedManager(RuntimeManager):
    def __init__(self):
        network = NetworkPolicy(advertised_origin="http://runtime-environment:8000", allowed_origins=[
            "http://runtime-environment:8000", "http://runtime-agent:8001", "http://runtime-host:8001",
            "http://runtime-observer:8001",
        ])
        self.environment = EnvironmentApp(CALENDAR_TOOLS, network=network)
        self.environment.registry.lifecycle.grace_s = 60
        self.io = LoopbackServer(self.environment.app, port=8000, bind_host="0.0.0.0", network=network)
        self.episodes, self.lock = set(), threading.RLock()

    def provision(self, config, *, scripted=False):
        episode = self.environment.registry.provision(uuid.uuid4().hex, [0, 1, 2], join_timeout_s=60, environment_id="calendar")
        managed = ManagedEpisode(self, episode)
        with self.lock:
            self.episodes.add(managed)
        for seat in episode.seats.values():
            directory = Path(f"/provision/seat-{seat.seat}")
            os.chmod(directory, 0o700)
            managed.descriptors.append(write_descriptor(directory, episode, seat,
                f"{self.io.base_url}/episodes/{episode.episode_id}/join"))
        return managed


def main():
    status = {"healthy": True, "done": False, "error": None}
    manager = ScopedManager()
    lifecycle = manager.environment.registry.lifecycle
    completion_order = []
    finish = lifecycle.finish

    def observed_finish(credential, finished):
        receipt = finish(credential, finished)
        record = lifecycle.records[lifecycle.key(finished)]
        if record.invocation.kind == "reflect" and receipt.disposition == "recorded":
            completion_order.append((finished.episode_id, finished.seat, record.invocation.observation["target_agent_id"]))
        return receipt
    lifecycle.finish = observed_finish

    async def health(request):
        with lifecycle.lock:
            pending = [r for r in lifecycle.records.values() if r.invocation.phase == "DECISION_RETRY"
                       and r.invocation.seat == 0 and not r.event.is_set()]
            record = pending[0] if pending else None
            return JSONResponse({**status, "accepted_pending": bool(record and record.acceptance),
                "staged": len(record.recorder.records) if record else 0,
                "tool_calls": record.recorder.calls if record else 0,
                "closed": record.recorder.closed if record else None})

    async def release(request):
        Path("/provision/seat-0/release").touch()
        return JSONResponse({"released": True})

    manager.environment.app.routes.extend([Route("/health", health), Route("/status", health), Route("/release", release, methods=["POST"])])
    with manager:
        def run():
            try:
                cfg = {"seed": 42, "num_agents": 3, "num_slots": 3, "num_meetings": 1,
                    "agents": [{"type": "scripted", "runtime": "external"}] * 3,
                    "enable_fallback": False, "enable_reflection": True, "reflection_max_parallelism": 3,
                    "decision_retries": 1, "max_turns_per_round": 1, "turn_timeout_s": 30}
                scenario = {"calendars": [[None] * 3 for _ in range(3)], "meetings": [
                    {"id": 0, "participants": [0, 1, 2], "speaker_order": [0, 1, 2], "cost": 1, "duration": 1}]}
                traces, episodes = [], []
                database = Path("/tmp/calendar.db")
                store = SQLiteEpisodeStore(path=database)
                LocalStackHandler.database, LocalStackHandler.workspace = database, Path("/workspace")
                for cls in (CalendarGame, LegacyCalendarGame):
                    managed = manager.provision(cfg)
                    episodes.append(managed.episode.episode_id)
                    trace = cls(cfg, runtime_context=managed).run_with_scenario(copy.deepcopy(scenario))
                    assert trace.metrics["meetings_scheduled"] == 1 and trace.metrics["reflection_estimate_count"] == 18
                    assert len([e for e in trace.events if e.type == "cell_rejected"]) == 3
                    starts = [i for i, e in enumerate(trace.events) if e.type == "reflection_start"]
                    ends = [i for i, e in enumerate(trace.events) if e.type == "reflection_end"]
                    assert max(starts) < min(ends)
                    assert [(trace.events[i].data["agent_id"], trace.events[i].data["target_agent_id"]) for i in ends] == [
                        (0, 1), (0, 2), (1, 0), (1, 2), (2, 0), (2, 1)]
                    manifest = EpisodeManifest.from_run(config=trace.config.model_dump(), experiment_name="container-calendar",
                        cell_id="cell", episode_idx=len(traces), episode_uid=trace.episode_uid)
                    store.put_episode(trace, manifest)
                    assert LocalStackHandler._episode_detail(trace.episode_uid)["episode"]["metrics"]["meetings_scheduled"] == 1
                    traces.append(trace)
                assert normalized_events(traces[0]) == normalized_events(traces[1])
                assert traces[0].final_state == traces[1].final_state and traces[0].metrics == traces[1].metrics
                records = list(lifecycle.records.values())
                assert all(r.finished and r.finished.status == "succeeded" for r in records)
                reflections = [r for r in records if r.invocation.kind == "reflect"]
                assert len(reflections) == 12
                assert all(r.invocation.protocol_version == "a2a-turns/2" for r in records)
                assert [(seat, target) for episode, seat, target in completion_order if episode == episodes[0]] != [
                    (0, 1), (0, 2), (1, 0), (1, 2), (2, 0), (2, 1)]
                serialized = json.dumps([trace.model_dump(mode="json") for trace in traces])
                assert not any(secret in serialized for secret in ("join_ticket", "seat_secret", "capability", "lifecycle_credential", "runtime-agent", "runtime-host"))
                assert not manager.episodes
                status.update(episodes=2, reflections=12, parity=True, persisted_readback=True,
                    workflow=LocalStackHandler._control().environment_workflow("calendar")["status"])
            except Exception as exc:
                status["error"] = "calendar_gate_failed"
                status["error_type"] = type(exc).__name__
                status["error_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
                raise
            finally:
                status["done"] = True

        threading.Thread(target=run, daemon=True).start()
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
