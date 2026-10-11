"""Checks executed inside the disposable supported deployment, without providers."""

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

import yaml


def request(path, body=None):
    with urlopen(Request("http://viewer:8080" + path,
                         data=json.dumps(body).encode() if body is not None else None,
                         headers={"Content-Type": "application/json"}), timeout=15) as response:
        return json.load(response)


def await_launch(launch):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        detail = request(f"/api/launches/{launch['id']}")
        if detail["launch"]["status"] not in {"QUEUED", "RUNNING", "CANCELLING"}:
            return detail
        time.sleep(0.1)
    raise AssertionError("viewer launch did not terminate")


def main():
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore
    traces = list(SQLiteEpisodeStore(path="/data/a2a.db").iter_episodes())
    assert len(traces) == 8, "the CLI must persist all eight requested episodes"
    assert all(not trace.stopped and trace.metrics for trace in traces), "CLI smoke emitted a stopped episode"
    games = {"word_guess": "word-guess", "buyer_seller": "buyer-seller",
             "calendar": "calendar", "negotiation": "negotiation"}
    experiments, launches = {}, []
    for game, folder in games.items():
        assert request(f"/api/environments/{game}/workflow")["status"] == "available"
        experiment = request("/api/experiments", {"yaml_path": f"games/{folder}/experiments/runtime_smoke.yaml"})
        experiments[game] = experiment
        launches.append(request("/api/launches", {"experiment_id": experiment["id"], "mode": "smoke", "max_parallelism": 2}))
    for launch in launches:
        detail = await_launch(launch)
        assert detail["launch"]["status"] == "COMPLETED", detail
        assert detail["progress"]["completed"] == 1
        assert len(detail["attempts"]) == 1 and detail["attempts"][0]["episode_uid"]
        trace = request("/api/episodes/" + detail["attempts"][0]["episode_uid"])["episode"]
        registered = [event["data"] for event in trace["events"] if event["type"] == "agent_registered"]
        if trace["config"]["environment_id"] == "calendar":
            assert registered and all(seat["protocol_version"] == "a2a-turns/2" for seat in registered)
        assert all(agent["runtime"] == "external" for agent in trace["config"]["agents"])
        assert not trace["stopped"]
        assert all(field not in json.dumps(trace) for field in ("join_ticket", "seat_secret", "lifecycle_credential", "http://runtime-seat-"))
    repeated = request("/api/launches", {"experiment_id": experiments["calendar"]["id"], "mode": "smoke"})
    detail = await_launch(repeated)
    assert detail["launch"]["status"] == "COMPLETED", detail
    assert detail["attempts"][0]["attempt"] == 2
    trace = request("/api/episodes/" + detail["attempts"][0]["episode_uid"])["episode"]
    assert trace["config"]["provenance"]["attempt"] == 2

    path = Path("games/calendar/experiments/runtime_cancel_test.yaml")
    config = yaml.safe_load(Path("games/calendar/experiments/runtime_smoke.yaml").read_text())
    config["name"] = "runtime_cancel_test"
    config["defaults"].update(num_meetings=100, num_slots=120, enable_reflection=False)
    config["cells"] = [{"label": "cancel", "count": 8}]
    path.write_text(yaml.safe_dump(config))
    experiment = request("/api/experiments", {"yaml_path": str(path)})
    launch = request("/api/launches", {"experiment_id": experiment["id"], "max_parallelism": 2})
    deadline = time.monotonic() + 20
    while not list(Path("/provision/seat-0").glob("*.json")):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    request(f"/api/launches/{launch['id']}/cancel", {})
    detail = await_launch(launch)
    assert detail["launch"]["status"] == "CANCELLED", detail
    assert all(attempt["status"] == "CANCELLED" for attempt in detail["attempts"])
    path.unlink()
    deadline = time.monotonic() + 10
    while list(Path("/provision").glob("seat-*/*.json")):
        assert time.monotonic() < deadline, "descriptors survived cancellation"
        time.sleep(0.1)
    print("Mixed-game viewer launches, repeated attempts, cancellation and redacted readback passed")


if __name__ == "__main__":
    main()
