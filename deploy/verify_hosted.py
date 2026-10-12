"""Verify the hosted public/authentication boundary without collecting credentials.

This verifies deployment health and denial of unauthenticated private reads and
mutations. Real Google login, owner isolation, execution and exports are separate
authenticated end-to-end checks, not claims made by this script.
"""
import argparse
import hashlib
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def verify_runtime_bindings(project, region, source_sha):
    """Inspect persisted catalogs/Job definitions without executing any Job."""
    try:
        from .common import command
        from .release import BOOTSTRAP, DIGEST, SOURCE, environment, image_digest, task_spec
    except ImportError:
        from common import command
        from release import BOOTSTRAP, DIGEST, SOURCE, environment, image_digest, task_spec
    service = json.loads(command(["run", "services", "describe", "groundwork-control",
        "--project", project, "--region", region, "--format=json"], capture=True))
    api = service["spec"]["template"]["spec"]["containers"][0]
    values = environment(api)
    worker_image = api["image"]
    worker_digest = image_digest(worker_image)
    assert values.get("PLATFORM_SOURCE_SHA") == source_sha, "API worker source differs from this release"
    assert values.get("PLATFORM_IMAGE_DIGEST") == worker_digest, "API worker digest is incorrect"
    key = values.get("PLATFORM_RUNTIME_CATALOG_KEY", "")
    assert key.startswith("platform/runtime-bindings/") and ".." not in key, "runtime catalog key is invalid"
    raw = command(["storage", "cat", f"gs://{project}-groundwork-artifacts/{key}"], capture=True)
    if re.fullmatch(r"[0-9a-f]{64}\.json", key.rsplit("/", 1)[-1]):
        assert hashlib.sha256(raw.encode()).hexdigest() + ".json" == key.rsplit("/", 1)[-1], "catalog content hash differs"
    catalog = json.loads(raw)
    assert catalog.get("schema_version") == 1, "unknown catalog schema"
    assert catalog.get("worker") == {"source_sha": source_sha, "image_digest": worker_digest, "image": worker_image}, "catalog worker provenance differs"
    runtimes = catalog.get("runtimes")
    assert isinstance(runtimes, dict) and runtimes, "catalog has no supported runtimes"
    current = runtimes.get(values.get("PLATFORM_RUNTIME_DIGEST"))
    assert current and current["runtime_image"] == values.get("PLATFORM_RUNTIME_IMAGE"), "current science image has no binding"
    assert current["runtime_source_sha"] == values.get("PLATFORM_RUNTIME_SOURCE_SHA"), "current science source differs"
    for digest, binding in runtimes.items():
        assert DIGEST.fullmatch(digest) and image_digest(binding["runtime_image"]) == digest, "runtime digest mismatch"
        assert SOURCE.fullmatch(binding["runtime_source_sha"]) and binding.get("contract_version") == 1, "runtime contract is unsupported"
        name = binding["job_name"]
        assert re.fullmatch(r"groundwork-run-[0-9a-f]{12}-[0-9a-f]{12}", name), "Job binding name is invalid"
        job = json.loads(command(["run", "jobs", "describe", name, "--project", project,
            "--region", region, "--format=json"], capture=True))
        task = task_spec(job)
        containers = {value.get("name"): value for value in task["containers"]}
        assert set(containers) == {"worker", "runtime"}, "paired Job requires exactly worker and runtime containers"
        worker, science = containers["worker"], containers["runtime"]
        assert worker["image"] == worker_image and science["image"] == binding["runtime_image"], "Job images differ from catalog"
        assert worker.get("command") == ["python"] and worker.get("args") == ["-m", "cloud_platform.worker"], "Job worker command is incorrect"
        assert science.get("command") == ["python"] and science.get("args") == ["-c", BOOTSTRAP], "Job runtime bootstrap differs from this infrastructure release"
        worker_env, runtime_env = environment(worker), environment(science)
        assert worker_env.get("PLATFORM_EXECUTION_DIR") == runtime_env.get("PLATFORM_EXECUTION_DIR") == "/execution", "bridge path differs"
        assert worker_env.get("PLATFORM_WORKER_CONTRACT_VERSION") == "1", "worker contract differs"
        assert worker_env.get("PLATFORM_SOURCE_SHA") == source_sha and worker_env.get("PLATFORM_IMAGE_DIGEST") == worker_digest, "Job worker provenance differs"
        assert runtime_env.get("PLATFORM_RUNTIME_SOURCE_SHA") == binding["runtime_source_sha"] and runtime_env.get("PLATFORM_RUNTIME_DIGEST") == digest, "Job science provenance differs"
        assert not any(entry["name"].endswith("_API_KEY") for entry in worker.get("env", [])), "provider credentials were mounted in the worker"
        assert all({"name": "execution", "mountPath": "/execution"} in value.get("volumeMounts", []) for value in containers.values()), "execution volume is not shared"
        volumes = {volume["name"]: volume for volume in task.get("volumes", [])}
        assert volumes.get("execution", {}).get("emptyDir", {}).get("medium") == "Memory", "execution volume is not in-memory"
        assert int(task.get("maxRetries", -1)) == 0 and int(task.get("timeoutSeconds", 0)) == 3600, "Job retry/duration limits differ"
    return {"persisted_catalog": "passed", "paired_job_count": len(runtimes), "worker_source_sha": source_sha,
            "worker_image_digest": worker_digest, "current_runtime_digest": values["PLATFORM_RUNTIME_DIGEST"],
            "jobs_executed": 0}


def response(base, path, method="GET", body=None):
    request = Request(base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as result:
            return result.status, {key.lower(): value for key, value in result.headers.items()}, json.load(result)
    except HTTPError as error:
        return error.code, {key.lower(): value for key, value in error.headers.items()}, json.load(error)


def verify(url, source_sha, attempts=12):
    url = url.rstrip("/")
    if urlsplit(url).scheme != "https":
        raise ValueError("Hosted verification requires an HTTPS URL")
    last = None
    for attempt in range(attempts):
        try:
            status, headers, health = response(url, "/api/health")
            assert status == 200 and health.get("ok") is True, "health endpoint is unhealthy"
            assert health.get("source_sha") == source_sha, "health source_sha does not match deployed source"
            break
        except (AssertionError, URLError) as error:
            last = error
            if attempt == attempts - 1:
                raise RuntimeError(f"Hosted health verification failed: {last}") from error
            time.sleep(5)
    status, headers, config = response(url, "/auth/config")
    assert status == 200, "Google login configuration is unavailable"
    assert config.get("google_client_id", "").endswith(".apps.googleusercontent.com"), "missing Google web client"
    assert config.get("dev_auth") is False, "development login enabled on hosted platform"
    assert "Secure" in headers.get("set-cookie", ""), "hosted login cookie lacks Secure flag"
    paths = [("GET", "/auth/me", None), ("GET", "/api/releases", None),
             ("GET", "/api/experiments", None), ("GET", "/api/experiments/unauthorized", None),
             ("GET", "/api/launches/unauthorized", None), ("GET", "/api/episodes/unauthorized", None),
             ("GET", "/api/exports/unauthorized/download", None),
             ("POST", "/api/experiments", {}),
             ("POST", "/api/experiments/unauthorized/launch", {}),
             ("POST", "/api/experiments/unauthorized/export", {}),
             ("POST", "/api/launches/unauthorized/cancel", {}),
             ("POST", "/internal/dispatch", {"launch_id": "unauthorized"})]
    for method, path, body in paths:
        status, headers, data = response(url, path, method, body)
        assert status == 401, f"{method} {path}: expected 401, received {status}"
        assert set(data) == {"error"}, f"{method} {path}: error response exposes data"
        assert headers.get("cache-control") == "no-store", f"{path}: response may be cached"
    status, _, _ = response(url, "/auth/dev", "POST", {"user": "alice"})
    assert status == 404, "hosted development login must be disabled"
    result = {"service_url": url, "source_sha": source_sha, "health": "passed",
              "google_login_configuration": "passed", "unauthenticated_access_checks": len(paths),
              "development_login": "disabled", "authenticated_end_to_end": "requires real Google login"}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--project")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--verify-runtime-bindings", action="store_true")
    args = parser.parse_args()
    if args.verify_runtime_bindings and not args.project:
        parser.error("Runtime binding verification requires --project")
    url = args.url
    if args.project:
        from common import service_url
        url = service_url(args.project, args.region)
    verify(url, args.source_sha)
    if args.verify_runtime_bindings:
        print(json.dumps({"runtime_bindings": verify_runtime_bindings(args.project, args.region, args.source_sha)}, indent=2))
