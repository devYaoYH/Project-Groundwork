"""Verify real Firestore transactions and immutable GCS, using caller credentials.

Run with --project/--bucket and ADC, or set PLATFORM_VERIFY_ACCESS_TOKEN to a
short-lived token externally. No tokens or credential values are printed.
Only this invocation's UUID-prefixed test documents/objects are cleaned up.
"""
from __future__ import annotations

import argparse
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore, storage
from google.oauth2.credentials import Credentials

from cloud_platform.storage import ArtifactStore, Repository


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args(argv)
    token = os.getenv("PLATFORM_VERIFY_ACCESS_TOKEN")
    credentials = Credentials(token) if token else None
    firestore_client = firestore.Client(project=args.project, credentials=credentials)
    storage_client = storage.Client(project=args.project, credentials=credentials)
    repo = Repository(backend="gcp", project=args.project, client=firestore_client)
    artifacts = ArtifactStore(backend="gcp", project=args.project, bucket=args.bucket, client=storage_client)
    identifier = "verification-" + uuid.uuid4().hex
    launch_id = identifier + "-launch"
    key = f"verification/{identifier}/immutable.json"
    try:
        assert repo.create("experiments", identifier, {"id": identifier, "owner_id": identifier})
        assert not repo.create("experiments", identifier, {"id": identifier})
        with ThreadPoolExecutor(max_workers=4) as pool:
            ordinals = list(pool.map(lambda _: repo.next_attempt(identifier), range(4)))
        assert sorted(ordinals) == [1, 2, 3, 4]
        repo.admit_launch({"id": launch_id, "owner_id": identifier, "experiment_id": identifier, "episode_count": 1})
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: repo.claim("launches", launch_id, {"QUEUED"}, {"status": "RUNNING"}), range(4)))
        assert sum(c is not None for c in claims) == 1
        repo.claim("launches", launch_id, {"RUNNING"}, {"status": "COMPLETED"})
        assert repo.get("quotas", identifier)["active"] == 0
        assert len(repo.list("launches", owner_id=identifier, experiment_id=identifier)) == 1
        artifacts.put(key, b'{"evidence":true}')
        artifacts.put(key, b'{"evidence":true}')
        try:
            artifacts.put(key, b'{"evidence":false}')
        except ValueError:
            pass
        else:
            raise AssertionError("immutable GCS object was overwritten")
        assert artifacts.get(key) == b'{"evidence":true}'
        assert artifacts.size(key) == len(b'{"evidence":true}')
        assert artifacts.keys(f"verification/{identifier}/") == [key]
        print("PASS: Firestore atomic create, ordinal reservation, competing worker claims, quota release, scoped query; GCS immutable writes, readback, size, listing")
    finally:
        for kind, doc_id in (("experiments", identifier), ("launches", launch_id), ("quotas", identifier)):
            repo._ref(kind, doc_id).delete()
        artifacts.bucket.blob(key).delete() if artifacts.bucket.blob(key).exists() else None


if __name__ == "__main__":
    main()
