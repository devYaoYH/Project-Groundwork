"""Live launch visibility, private reads, and resumable SSE ordering."""
import asyncio
import json

from cloud_platform.streaming import launch_events
from cloud_platform.control import now
from cloud_platform.tests.test_api import platform, sign_in, create_experiment


def event(identifier, line):
    return {"id": identifier, "kind": "runner.log", "created_at": "2026-10-11T00:00:00Z",
            "payload": {"line": line}}


def test_live_snapshot_is_private_and_final_evidence_takes_precedence(platform):
    app, control, _ = platform
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")
    experiment = create_experiment(alice)
    launch = alice.post("/api/launches", json={"experiment_id": experiment["id"], "mode": "smoke"}).json()
    identifier = launch["id"]
    attempts = control.repo.list("attempts", owner_id=experiment["owner_id"], experiment_id=experiment["id"])
    attempts[0].update(status="RUNNING", episode_uid="provisional-episode")
    progress = {"planned": len(attempts), "completed": 0, "failed": 0, "by_cell": []}
    key = f"launches/{identifier}/live-detail.json"
    control.artifacts.put(key, json.dumps({"attempts": attempts, "progress": progress,
        "runner_logs": [event(7, "first live line")], "logs_truncated": True}).encode(), immutable=False)
    control.repo.claim("launches", identifier, {"QUEUED"}, {"status": "RUNNING",
        "live_detail_key": key, "live_updated_at": "2026-10-11T00:00:00Z",
        "heartbeat_at": now()})
    detail = alice.get(f"/api/launches/{identifier}").json()
    assert detail["status"] == "RUNNING" and detail["provisional"]
    assert not detail.get("evidence_complete")
    assert detail["attempts"][0]["status"] == "RUNNING"
    assert detail["runner_logs"][0]["payload"]["line"] == "first live line"
    assert detail["log_start_id"] == 7 and detail["logs_truncated"]
    assert bob.get(f"/api/launches/{identifier}").status_code == 404
    assert bob.get(f"/api/launches/{identifier}/events").status_code == 404
    final_key = f"launches/{identifier}/detail.json"
    final = {"attempts": attempts, "progress": progress, "runner_logs": [event(7, "final evidence")]}
    control.artifacts.put(final_key, json.dumps(final).encode())
    control.repo.claim("launches", identifier, {"RUNNING"}, {"status": "COMPLETED", "detail_key": final_key})
    detail = alice.get(f"/api/launches/{identifier}").json()
    assert not detail["provisional"] and not detail["logs_truncated"]
    assert detail["runner_logs"][0]["payload"]["line"] == "final evidence"
    response = alice.get(f"/api/launches/{identifier}/events", headers={"Last-Event-ID": "7"})
    assert response.status_code == 200 and "event: runner.log" not in response.text
    assert "event: launch.progress" in response.text and "event: launch.status" in response.text
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert alice.get(f"/api/launches/{identifier}/events?after_id=-1").status_code == 400


def test_stream_delivers_updates_before_completion_and_resumes_without_duplicates():
    class Viewer:
        calls = 0

        def launch(self, identifier, owner):
            assert (identifier, owner) == ("launch", "owner")
            self.calls += 1
            return {"launch": {"id": identifier, "status": "RUNNING" if self.calls == 1 else "COMPLETED"},
                    "runner_logs": [event(3, "already seen"), event(5, "live")]
                    + ([event(9, "last output")] if self.calls > 1 else []),
                    "attempts": [], "progress": {"completed": self.calls - 1}, "log_start_id": 3}

    async def check():
        viewer = Viewer()
        stream = launch_events(viewer, "launch", "owner", 3, interval=0)
        assert "connected" in await anext(stream) and viewer.calls == 0
        live = await anext(stream)
        assert "id: 5\n" in live and "live" in live and viewer.calls == 1
        remaining = [chunk async for chunk in stream]
        text = live + "".join(remaining)
        assert text.count("event: runner.log") == 2
        assert "already seen" not in text
        assert text.index("last output") < text.index('"status":"COMPLETED"')
        assert viewer.calls == 2

    asyncio.run(check())


def test_stream_stops_reading_on_disconnect_and_session_expiry():
    from starlette.exceptions import HTTPException

    class Viewer:
        def launch(self, *_):
            raise AssertionError("closed stream must not read private artifacts")

    class Request:
        async def is_disconnected(self):
            return True

    async def check():
        disconnected = [chunk async for chunk in launch_events(Viewer(), "launch", "owner", request=Request())]
        assert len(disconnected) == 1

        def expired(_):
            raise HTTPException(401)

        frames = [chunk async for chunk in launch_events(Viewer(), "launch", "owner", authenticate=expired)]
        assert len(frames) == 2 and "event: auth.expired" in frames[-1]

    asyncio.run(check())


def test_provider_error_detail_is_available_in_owner_log_api_and_event_stream(platform, monkeypatch, caplog):
    from io import BytesIO
    from urllib.error import HTTPError
    import pytest
    from a2a_engine.llm import api

    def expired_key(*args, **kwargs):
        raise HTTPError("https://fixture.invalid", 401, "Unauthorized", {},
                        BytesIO(b'{"error":{"message":"API key expired.","metadata":{"key":"private-key"}}}'))

    monkeypatch.setattr(api.urllib.request, "urlopen", expired_key)
    with pytest.raises(HTTPError):
        api.call_llm_oneshot(api_format="openai", api_base="https://fixture.invalid", api_key="private-key",
                             model="fixture", messages=[{"role": "user", "content": "hello"}])
    line = next(record.getMessage() for record in caplog.records if "API key expired." in record.getMessage())

    app, control, _ = platform
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")
    experiment = create_experiment(alice)
    launch = alice.post("/api/launches", json={"experiment_id": experiment["id"], "mode": "smoke"}).json()
    identifier = launch["id"]
    key = f"launches/{identifier}/detail.json"
    control.artifacts.put(key, json.dumps({"attempts": [], "progress": {},
                                         "runner_logs": [event(1, line)]}).encode())
    control.repo.claim("launches", identifier, {"QUEUED"}, {"status": "FAILED", "detail_key": key})
    detail = alice.get(f"/api/launches/{identifier}").json()
    assert "API key expired." in detail["runner_logs"][0]["payload"]["line"]
    stream = alice.get(f"/api/launches/{identifier}/events")
    assert stream.status_code == 200
    assert "event: runner.log" in stream.text and "API key expired." in stream.text
    assert "private-key" not in stream.text
    assert bob.get(f"/api/launches/{identifier}").status_code == 404
    assert bob.get(f"/api/launches/{identifier}/events").status_code == 404
