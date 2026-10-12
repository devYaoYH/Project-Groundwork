"""Resolve a frozen scientific runtime to the current infrastructure worker.

The deployment publishes an immutable catalog only after all paired Jobs exist.
The control service never substitutes a newer scientific image for a missing pin.
"""
from __future__ import annotations

import re

CONTRACT_VERSION = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_JOB = re.compile(r"[a-z][a-z0-9-]{0,62}")


def validate_catalog(catalog, *, worker_digest, worker_source_sha):
    if not isinstance(catalog, dict) or catalog.get("schema_version") != CONTRACT_VERSION:
        raise ValueError("Unsupported runtime binding catalog")
    worker = catalog.get("worker", {})
    if not isinstance(worker, dict) or worker.get("image_digest") != worker_digest or worker.get("source_sha") != worker_source_sha:
        raise ValueError("Runtime binding catalog does not match this worker release")
    if not _DIGEST.fullmatch(worker_digest) or not _SHA.fullmatch(worker_source_sha):
        raise ValueError("Worker provenance must use an immutable image and source commit")
    if not str(worker.get("image", "")).endswith("@" + worker_digest):
        raise ValueError("Worker catalog image does not match its digest")
    runtimes = catalog.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        raise ValueError("Runtime binding catalog is empty")
    for digest, runtime in runtimes.items():
        if not _DIGEST.fullmatch(digest) or not isinstance(runtime, dict):
            raise ValueError("Invalid runtime binding")
        if not str(runtime.get("runtime_image", "")).endswith("@" + digest):
            raise ValueError("Runtime image does not match its digest")
        if not _SHA.fullmatch(str(runtime.get("runtime_source_sha", ""))):
            raise ValueError("Runtime source commit is missing")
        if not _JOB.fullmatch(str(runtime.get("job_name", ""))) or runtime.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("Unsupported runtime worker contract")
    return catalog


def resolve_binding(catalog, experiment):
    digest = experiment.get("runtime_image_digest", experiment.get("image_digest"))
    runtime = catalog["runtimes"].get(digest)
    if runtime is None:
        raise ValueError("The pinned experiment runtime is unavailable in this worker release; no launch was admitted")
    source = experiment.get("runtime_source_sha", experiment.get("source_sha"))
    if source != runtime["runtime_source_sha"]:
        raise ValueError("Pinned runtime source does not match its registered image")
    worker = catalog["worker"]
    return {"runtime_source_sha": source, "runtime_image_digest": digest,
            "runtime_image": runtime["runtime_image"], "job_name": runtime["job_name"],
            "worker_source_sha": worker["source_sha"], "worker_image_digest": worker["image_digest"],
            "worker_image": worker["image"], "worker_contract_version": runtime["contract_version"],
            "live_logs": True}
