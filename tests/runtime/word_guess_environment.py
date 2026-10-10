"""Disposable Compose gate using the real runner, graph, MCP and viewer readback."""

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.network import NetworkPolicy
from a2a_engine.remote.seats import write_descriptor
from a2a_engine.remote.server import EnvironmentApp, ManagedEpisode, RuntimeManager
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from expt_runner.run_experiment import _make_run_context, _run_one
from local_stack.server import LocalStackHandler
from word_guess.remote import WORD_GUESS_TOOLS


class ScopedManager(RuntimeManager):
    def __init__(self):
        network = NetworkPolicy(advertised_origin="http://runtime-environment:8000", allowed_origins=[
            "http://runtime-environment:8000", "http://runtime-agent:8001", "http://runtime-host:8001",
        ])
        self.environment = EnvironmentApp(WORD_GUESS_TOOLS, network=network)
        self.environment.registry.lifecycle.grace_s = 60
        self.io = LoopbackServer(self.environment.app, port=8000, bind_host="0.0.0.0", network=network)
        self.episodes, self.lock = set(), threading.RLock()

    def provision(self, config, *, scripted=False):
        episode = self.environment.registry.provision(uuid.uuid4().hex, [0, 1], join_timeout_s=60, environment_id="word_guess")
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
    database = Path("/tmp/word-guess.db")
    LocalStackHandler.database = database
    LocalStackHandler.workspace = Path("/workspace")

    async def health(request):
        return JSONResponse(status)

    manager.environment.app.routes.extend([Route("/health", health), Route("/status", health)])
    with manager:
        def run():
            try:
                store = SQLiteEpisodeStore(path=database)

                def play(index):
                    context = _make_run_context("container-words", "cell", {
                        "environment_id": "word_guess", "secret_word": "apple" if index == 2 else "dog", "max_turns": 6,
                        "agents": [{"type": "scripted", "runtime": "external"}] * 2,
                        "turn_timeout_s": 15,
                    }, index, dry_run=True)
                    context.update(runtime_manager=manager, verify_readback=True)
                    return _run_one(context, store, Path("/tmp/word-results"))

                play(0)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(play, [1, 2]))
                traces = list(store.iter_episodes())
                assert len(traces) == 3
                for trace in traces:
                    won = trace.config.secret_word == "dog"
                    turns_used = 3 if won else 6
                    assert trace.final_state == {"secret_word": trace.config.secret_word, "guessed_word": "dog", "won": won, "turns_used": turns_used}
                    messages = [event.data for event in trace.events if event.type == "message"]
                    guesses = ["is it an animal?", "is it a dog?", *(["I guess: dog"] * (turns_used - 2))]
                    replies = ["yes", "yes", "correct!"] if won else ["no", "no", *(["wrong"] * 4)]
                    assert messages == [message for guess, reply in zip(guesses, replies)
                                        for message in ({"speaker": "guesser", "text": guess}, {"speaker": "host", "text": reply})]
                    detail = LocalStackHandler._episode_detail(trace.episode_uid)
                    assert detail["cursor_max"] == 2 * turns_used + 2 and detail["episode"]["metrics"]["won"] == won
                registrations = [record.invocation for record in manager.environment.registry.lifecycle.records.values()
                                 if record.invocation.kind == "register"]
                assert len(registrations) == 6
                assert all(("secret_word" in i.observation) == (i.seat == 1) for i in registrations)
                assert all("dog" not in i.prompt for i in registrations if i.seat == 0)
                turns = [record for record in manager.environment.registry.lifecycle.records.values() if record.invocation.kind == "turn"]
                assert len(turns) == 24
                assert all(record.finished.status == "succeeded" and record.recorder.closed_by == "completed" and
                           len(record.recorder.snapshot) == 1 for record in turns)
                serialized = json.dumps([trace.model_dump(mode="json") for trace in traces])
                assert all(value not in serialized for value in ("join_ticket", "seat_secret", "capability", "lifecycle_credential", "runtime-agent", "runtime-host"))
                assert not manager.episodes
                status.update(episodes=3, actions=24, private_registration=True, persisted_readback=True,
                              workflow=LocalStackHandler._control().environment_workflow("word_guess")["status"])
            except Exception:
                status["error"] = "word_guess_gate_failed"
                raise
            finally:
                status["done"] = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
