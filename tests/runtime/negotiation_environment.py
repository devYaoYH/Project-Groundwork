"""Real runner, graphs and callback services against frozen backend and local calls."""

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
from expt_runner.run_experiment import _make_run_context, _run_one
from local_stack.server import LocalStackHandler
from negotiation_game.backend.engine import GameEngine
from negotiation_game.backend import workflow
from negotiation_game.game import NegotiationConfig, NegotiationGame, _to_engine_config, _metrics_from_result
from negotiation_game.remote import NEGOTIATION_TOOLS
from negotiation_fixture import CONFIG, make_controlled, digest


class ScopedManager(RuntimeManager):
    def __init__(self):
        network = NetworkPolicy(advertised_origin='http://runtime-environment:8000', allowed_origins=[
            'http://runtime-environment:8000', 'http://runtime-agent:8001', 'http://runtime-second:8001'])
        self.environment = EnvironmentApp(NEGOTIATION_TOOLS, network=network)
        self.environment.registry.lifecycle.grace_s = 60
        self.io = LoopbackServer(self.environment.app, port=8000, bind_host='0.0.0.0', network=network)
        self.episodes, self.lock = set(), threading.RLock()

    def provision(self, config, *, scripted=False):
        episode = self.environment.registry.provision(uuid.uuid4().hex, [0, 1], join_timeout_s=60,
                                                      environment_id='negotiation')
        managed = ManagedEpisode(self, episode)
        with self.lock:
            self.episodes.add(managed)
        for seat in episode.seats.values():
            directory = Path(f'/provision/seat-{seat.seat}')
            os.chmod(directory, 0o700)
            managed.descriptors.append(write_descriptor(directory, episode, seat,
                f'{self.io.base_url}/episodes/{episode.episode_id}/join'))
        return managed


def main():
    status = {'healthy': True, 'done': False, 'error': None, 'held': False}
    manager = ScopedManager()
    database = Path('/tmp/negotiation.db')
    LocalStackHandler.database, LocalStackHandler.workspace = database, Path('/workspace')
    async def health(request):
        return JSONResponse(status)
    async def release(request):
        Path('/provision/seat-1/release').touch()
        return JSONResponse({'released': True})
    manager.environment.app.routes.extend([Route('/health', health), Route('/status', health),
                                           Route('/release', release, methods=['POST'])])
    with manager:
        def run():
            try:
                mode = os.environ['NEGOTIATION_MODE']
                expected_events = []
                adapter = NegotiationGame(CONFIG)
                async def record(kind, data):
                    adapter._record(kind, data)
                    expected_events.append((kind, deepcopy(adapter.events[-1].data)))
                clients = [make_controlled(mode, seat) for seat in (0, 1)]
                legacy = runpy.run_path('games/negotiation/tests/legacy_engine.py')['LegacyEngine']
                engine = legacy(_to_engine_config(NegotiationConfig(**CONFIG)), clients[1], clients[0])
                engine.on_event(record)
                expected = asyncio.run(engine.run_game())
                observations, values = [[], []], [[], []]
                original = workflow.local_operation
                local_clients = [make_controlled(mode, seat) for seat in (0, 1)]
                async def capture(actor, operation, info):
                    seat = local_clients.index(actor)
                    if operation not in {'register', 'episode_end'}:
                        observations[seat].append((operation, deepcopy(info)))
                    result = await original(actor, operation, info)
                    if operation not in {'register', 'episode_end'}:
                        values[seat].append((operation, deepcopy(result.get('value'))))
                    return result
                workflow.local_operation = capture
                local = GameEngine(_to_engine_config(NegotiationConfig(**CONFIG)), local_clients[1], local_clients[0])
                assert asyncio.run(local.run_game()) == expected
                workflow.local_operation = original
                store = SQLiteEpisodeStore(path=database)
                def play(index):
                    context = _make_run_context('container-negotiation', 'cell', {
                        **CONFIG, 'agents': [{'type': 'heuristic', 'runtime': 'external'}] * 2}, index, dry_run=True)
                    context.update(runtime_manager=manager, verify_readback=True)
                    return _run_one(context, store, Path('/tmp/negotiation-results'))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(play, 0)
                    until = time.monotonic() + 30
                    held = None
                    while time.monotonic() < until:
                        held = next((r for r in list(manager.environment.registry.lifecycle.records.values())
                                     if r.invocation.operation == 'cheap_talk' and r.acceptance and r.recorder
                                     and len(r.recorder.records) == 1), None)
                        if held:
                            break
                        time.sleep(.02)
                    assert held is not None
                    time.sleep(2.5)
                    assert held.acceptance.status == 'accepted' and not held.event.is_set()
                    assert not held.recorder.closed and held.recorder.snapshot is None and not first.done()
                    assert not list(store.iter_episodes())
                    status['held'] = True
                    first.result(timeout=60)
                    list(pool.map(play, [1, 2]))
                traces = list(store.iter_episodes())
                assert len(traces) == 3
                for trace in traces:
                    assert [(e.type, e.data) for e in trace.events] == expected_events
                    assert trace.final_state == expected and trace.metrics == _metrics_from_result(expected)
                    detail = LocalStackHandler._episode_detail(trace.episode_uid)
                    assert detail['episode']['metrics'] == trace.metrics and detail['cursor_max'] == len(trace.events)
                turns = [r for r in list(manager.environment.registry.lifecycle.records.values()) if r.invocation.kind == 'turn']
                assert all(r.finished.status == 'succeeded' and r.recorder.closed_by == 'completed' and
                           len(r.recorder.snapshot) == 1 for r in turns)
                for host, seat in (('runtime-agent', 0), ('runtime-second', 1)):
                    audits = json.loads(urlopen(f'http://{host}:8001/audit', timeout=3).read())
                    assert len(audits) == 3
                    for audit in audits:
                        assert audit['seat'] == seat
                        assert audit['observations'] == digest(observations[seat])
                        assert audit['values'] == digest(values[seat])
                        assert audit['messages'] == digest(getattr(clients[seat], 'messages', []))
                        assert audit['history'] == digest(getattr(clients[seat], '_messages', []))
                serialized = json.dumps([trace.model_dump(mode='json') for trace in traces])
                assert all(secret not in serialized for secret in ('join_ticket', 'seat_secret', 'capability',
                           'lifecycle_credential', 'runtime-agent', 'runtime-second'))
                assert not manager.episodes
                status.update(episodes=3, actions=len(turns), parity=True, persisted=True,
                              workflow=LocalStackHandler._control().environment_workflow('negotiation')['status'])
            except Exception:
                status['error'] = 'negotiation_gate_failed'
                raise
            finally:
                status['done'] = True
        threading.Thread(target=run, daemon=True).start()
        while True:
            time.sleep(.1)


if __name__ == '__main__':
    main()
