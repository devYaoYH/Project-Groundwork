"""Verify the Compose API and its separate worker using public HTTP contracts.

Run ``python deploy/verify_local.py --url http://localhost:8080`` after
``docker compose -f compose.platform.yml up --build -d``. This checker never
starts a worker itself: successful evidence proves durable dispatch between
the running API and worker containers. Output contains identifiers/counts only.

``exercise_workspace`` can also accept explicitly authenticated Google sessions
for a hosted check. This script does not discover or extract browser cookies.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

WORKSPACE = Path(__file__).resolve().parents[1]


def expect(response: requests.Response, status: int, check: str) -> requests.Response:
    if response.status_code != status:
        # Avoid printing server bodies, request headers, sessions or credentials.
        raise RuntimeError(f"{check}: expected HTTP {status}, received HTTP {response.status_code}")
    return response


def request(session: requests.Session, url: str, path: str, *, method="GET", payload=None,
            expected=200, check="request") -> requests.Response:
    response = session.request(method, url.rstrip("/") + path, json=payload, timeout=30)
    return expect(response, expected, check)


def local_session(url: str, user: str) -> tuple[requests.Session, dict]:
    session = requests.Session()
    config = request(session, url, "/auth/config", check="authentication configuration").json()
    if not config.get("dev_auth"):
        raise RuntimeError("Local development login is disabled on this endpoint")
    session.headers["X-CSRF-Token"] = config["csrf_token"]
    principal = request(session, url, "/auth/dev", method="POST", payload={"user": user},
                        check="local login").json()["user"]
    return session, principal


def exercise_workspace(url: str, alice: requests.Session, bob: requests.Session,
                       alice_principal: dict, bob_principal: dict, *, timeout=120) -> dict:
    """Run a scripted episode, download joined evidence, then test isolation."""
    if alice_principal["owner_id"] == bob_principal["owner_id"]:
        raise RuntimeError("Isolation verification requires two different authenticated users")
    anonymous = requests.Session()
    request(anonymous, url, "/api/experiments", expected=401, check="anonymous access")
    request(anonymous, url, "/api/releases", expected=401, check="anonymous catalog access")
    releases = request(alice, url, "/api/releases", check="release catalog").json()["releases"]
    release = next((entry for entry in releases if entry["environment_id"] == "buyer_seller"), None)
    if release is None:
        raise RuntimeError("The buyer_seller release was not deployed")
    experiment = request(alice, url, "/api/experiments", method="POST", expected=201,
                         payload={"name": f"Deployment verification {uuid.uuid4().hex[:8]}",
                                  "release_id": release["id"],
                                  "design_text": (WORKSPACE / "local_stack/tests/buyer_seller_design_smoke.yaml").read_text(),
                                  "owner_id": bob_principal["owner_id"]}, check="experiment creation").json()
    if experiment["owner_id"] != alice_principal["owner_id"]:
        raise RuntimeError("Experiment ownership was accepted from request data")
    experiment_id = experiment["id"]
    # Existing persistent workspaces may contain prior verification runs.
    own_ids = {entry["id"] for entry in request(alice, url, "/api/experiments").json()["experiments"]}
    if experiment_id not in own_ids:
        raise RuntimeError("The new experiment is missing from its owner's list")
    launch = request(alice, url, f"/api/experiments/{experiment_id}/launch", method="POST", expected=202,
                     payload={"mode": "smoke", "max_parallelism": 1}, check="smoke admission").json()
    launch_id = launch["id"]
    deadline = time.monotonic() + timeout
    while True:
        completed = request(alice, url, f"/api/launches/{launch_id}", check="worker progress").json()
        if completed["status"] in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Worker did not finish launch {launch_id} within {timeout} seconds")
        time.sleep(2)
    if completed["status"] != "COMPLETED" or not completed.get("evidence_complete"):
        raise RuntimeError(f"Launch {launch_id} ended with status {completed['status']}")
    attempts = completed.get("attempts", [])
    if not attempts or any(attempt["status"] != "COMPLETED" for attempt in attempts):
        raise RuntimeError("Not all planned attempts have completed evidence")
    exported = request(alice, url, f"/api/experiments/{experiment_id}/export", method="POST",
                       payload={}, expected=201, check="joined export").json()
    export_id = exported["id"]
    download_path = f"/api/exports/{export_id}/download"
    downloaded = request(alice, url, download_path, check="private ZIP download")
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        metadata = json.loads(archive.read("metadata.json"))
        checksums = json.loads(archive.read("checksums.json"))
        if set(checksums) != set(archive.namelist()) - {"checksums.json"}:
            raise RuntimeError("Export checksum inventory does not cover every artifact")
        for key, digest in checksums.items():
            if hashlib.sha256(archive.read(key)).hexdigest() != digest:
                raise RuntimeError("An exported artifact checksum is invalid")
        if metadata["experiment"]["id"] != experiment_id or metadata["experiment"]["owner_id"] != alice_principal["owner_id"]:
            raise RuntimeError("Export metadata does not join to its owning experiment")
        if {entry["id"] for entry in metadata["launches"]} != {launch_id}:
            raise RuntimeError("Export launch identities do not join")
        if {entry["id"] for entry in metadata["attempts"]} != {entry["id"] for entry in attempts}:
            raise RuntimeError("Export attempt identities do not join")
        episodes = metadata["episodes"]
        if len(episodes) != len(attempts) or any(entry["experiment_id"] != experiment_id or entry["launch_id"] != launch_id
                or entry["owner_id"] != alice_principal["owner_id"] for entry in episodes):
            raise RuntimeError("Export episode identities do not join")
        if f"launches/{launch_id}/trace.sqlite" not in archive.namelist() or "metrics.csv" not in archive.namelist():
            raise RuntimeError("Export is missing raw traces or joined metrics")
        artifact_count = len(checksums)
    for episode in episodes:
        trace = request(alice, url, f"/api/episodes/{episode['id']}", check="episode evidence").json()
        if not trace.get("events"):
            raise RuntimeError("Episode evidence contains no durable events")
    visible_to_bob = request(bob, url, "/api/experiments", check="other user listing").json()["experiments"]
    if any(entry["id"] == experiment_id for entry in visible_to_bob):
        raise RuntimeError("Another user's experiment was listed")
    denied = 0
    paths = [f"/api/experiments/{experiment_id}", f"/api/launches/{launch_id}", download_path]
    paths += [f"/api/episodes/{entry['id']}" for entry in episodes]
    for path in paths:
        request(bob, url, path, expected=404, check="cross-user read denial")
        request(anonymous, url, path, expected=401, check="anonymous private read denial")
        denied += 2
    for path in (f"/api/experiments/{experiment_id}/launch", f"/api/experiments/{experiment_id}/export",
                 f"/api/launches/{launch_id}/cancel"):
        request(bob, url, path, method="POST", payload={}, expected=404, check="cross-user mutation denial")
        denied += 1
    # Temporarily omit the CSRF header while retaining the real signed session.
    csrf = alice.headers.pop("X-CSRF-Token", None)
    try:
        request(alice, url, "/api/experiments", method="POST", payload={}, expected=403, check="CSRF mutation denial")
    finally:
        if csrf:
            alice.headers["X-CSRF-Token"] = csrf
    return {"experiment_id": experiment_id, "launch_id": launch_id, "export_id": export_id,
            "status": completed["status"], "episode_count": len(episodes), "attempt_count": len(attempts),
            "verified_artifact_checksums": artifact_count, "isolation_checks": denied + 4}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    parsed = urlparse(args.url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
        parser.error("This local checker requires a localhost URL; hosted checks need explicitly authenticated sessions")
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    try:
        alice, alice_principal = local_session(args.url, "Alice")
        bob, bob_principal = local_session(args.url, "Bob")
        result = exercise_workspace(args.url, alice, bob, alice_principal, bob_principal, timeout=args.timeout)
        print(json.dumps(result, indent=2))
        return 0
    except RuntimeError as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Verification failed: HTTP transport unavailable ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
