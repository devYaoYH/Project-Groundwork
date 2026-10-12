"""Pair current infrastructure with every supported immutable science runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile

try:
    from .common import command, exists, service_url
except ImportError:
    from common import command, exists, service_url

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE = re.compile(r"[0-9a-f]{40,64}")
BOOTSTRAP = Path(__file__).with_name("runtime_bootstrap.py").read_text()
PROVIDERS = (("OPENAI_API_KEY", "groundwork-openai-api-key"),
             ("ANTHROPIC_API_KEY", "groundwork-anthropic-api-key"),
             ("GOOGLE_API_KEY", "groundwork-google-api-key"),
             ("OPENROUTER_API_KEY", "groundwork-openrouter-api-key"))


def image_digest(image):
    _, separator, digest = image.rpartition("@")
    if not separator or not DIGEST.fullmatch(digest):
        raise ValueError("An immutable image digest is required")
    return digest


def resolve_image(project, image):
    if "@" in image:
        image_digest(image)
        return image
    info = json.loads(command(["artifacts", "docker", "images", "describe", image,
                              "--project", project, "--format=json"], capture=True))
    digest = info.get("image_summary", info.get("imageSummary", {})).get("digest")
    if not DIGEST.fullmatch(digest or ""):
        raise RuntimeError("Artifact Registry did not return an immutable image digest")
    return image.rsplit(":", 1)[0] + "@" + digest


def task_spec(job):
    """Accept gcloud's v1 export and the corresponding v2 resource shape."""
    if "spec" in job:
        return job["spec"]["template"]["spec"]["template"]["spec"]
    return job["template"]["template"]


def environment(container):
    return {entry["name"]: entry["value"] for entry in container.get("env", []) if "value" in entry}


def job_name(job):
    return str(job.get("metadata", {}).get("name") or job.get("name", "")).rsplit("/", 1)[-1]


def runtime_from_job(job):
    """Recognize bridge-v1 provenance; never guess compatibility from its name."""
    name = job_name(job)
    if not name.startswith(("groundwork-ep-", "groundwork-run-")):
        return None
    try:
        containers = task_spec(job)["containers"]
        if name.startswith("groundwork-ep-"):
            if len(containers) != 1:
                raise ValueError("legacy Job must have one container")
            runtime = containers[0]
            env = environment(runtime)
            if runtime.get("command") != ["python"] or runtime.get("args") != ["-m", "cloud_platform.worker"]:
                raise ValueError("legacy Job does not declare the supported worker contract")
            if env.get("PLATFORM_WORKSPACE") != "/workspace":
                raise ValueError("legacy Job has an unsupported runtime workspace")
            image, source = runtime["image"], env.get("PLATFORM_SOURCE_SHA", "")
            digest = image_digest(image)
            if env.get("PLATFORM_IMAGE_DIGEST") != digest:
                raise ValueError("legacy Job image provenance disagrees with its container")
        else:
            named = {container.get("name"): container for container in containers}
            runtime, worker = named["runtime"], named["worker"]
            env = environment(runtime)
            worker_env = environment(worker)
            if worker_env.get("PLATFORM_WORKER_CONTRACT_VERSION") != "1" or worker_env.get("PLATFORM_EXECUTION_DIR") != "/execution":
                raise ValueError("paired Job has an unsupported execution contract")
            image, source = runtime["image"], env.get("PLATFORM_RUNTIME_SOURCE_SHA", "")
            digest = image_digest(image)
            if env.get("PLATFORM_RUNTIME_DIGEST") != digest:
                raise ValueError("paired Job runtime provenance disagrees with its image")
        if not SOURCE.fullmatch(source):
            raise ValueError("runtime source must identify a full commit")
        return {"runtime_image": image, "runtime_source_sha": source, "contract_version": 1}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Unsupported Groundwork Job {name}: {error}") from error


def discover_runtimes(jobs, current_image, current_source):
    if not SOURCE.fullmatch(current_source):
        raise ValueError("runtime source must identify a full commit")
    values = {}
    for job in sorted(jobs, key=lambda item: (item.get("metadata", {}).get("creationTimestamp", ""), job_name(item))):
        runtime = runtime_from_job(job)
        if runtime is not None:
            digest = image_digest(runtime["runtime_image"])
            if digest in values and values[digest]["runtime_source_sha"] != runtime["runtime_source_sha"]:
                raise ValueError("An existing runtime digest has conflicting source provenance")
            values.setdefault(digest, runtime)
    values.setdefault(image_digest(current_image), {"runtime_image": current_image,
        "runtime_source_sha": current_source, "contract_version": 1})
    return values


def paired_job_name(runtime_digest, worker_digest):
    for digest in (runtime_digest, worker_digest):
        if not DIGEST.fullmatch(digest):
            raise ValueError("paired Jobs require immutable digests")
    return "groundwork-run-" + runtime_digest[7:19] + "-" + worker_digest[7:19]


def job_manifest(project, region, worker_image, worker_source, runtime, providers=()):
    runtime_digest, worker_digest = image_digest(runtime["runtime_image"]), image_digest(worker_image)
    name = paired_job_name(runtime_digest, worker_digest)
    worker_env = {"PLATFORM_BACKEND": "gcp", "GOOGLE_CLOUD_PROJECT": project,
        "PLATFORM_REGION": region, "PLATFORM_BUCKET": f"{project}-groundwork-artifacts",
        "PLATFORM_SOURCE_SHA": worker_source, "PLATFORM_IMAGE_DIGEST": worker_digest,
        "PLATFORM_WORKER_SOURCE_SHA": worker_source, "PLATFORM_WORKER_IMAGE_DIGEST": worker_digest,
        "PLATFORM_WORKER_IMAGE": worker_image, "PLATFORM_WORKER_CONTRACT_VERSION": "1",
        "PLATFORM_RUNTIME_SOURCE_SHA": runtime["runtime_source_sha"], "PLATFORM_RUNTIME_DIGEST": runtime_digest,
        "PLATFORM_RUNTIME_IMAGE": runtime["runtime_image"], "PLATFORM_JOB_NAME": name,
        "PLATFORM_EXECUTION_DIR": "/execution", "PLATFORM_WORKSPACE": "/workspace",
        "PLATFORM_DATA_DIR": "/tmp/groundwork", "PLATFORM_MAX_PARALLELISM": "2"}
    runtime_env = {"PYTHONUNBUFFERED": "1", "A2A_RUNTIME_MODE": "loopback",
        "PLATFORM_EXECUTION_DIR": "/execution", "PLATFORM_RUNTIME_WORKSPACE": "/workspace",
        "PLATFORM_RUNTIME_DIGEST": runtime_digest, "PLATFORM_RUNTIME_SOURCE_SHA": runtime["runtime_source_sha"]}
    def container(label, image, arguments, env, cpu, memory):
        return {"name": label, "image": image, "command": ["python"], "args": arguments,
            "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
            "resources": {"limits": {"cpu": cpu, "memory": memory}},
            "volumeMounts": [{"name": "execution", "mountPath": "/execution"}]}
    worker = container("worker", worker_image, ["-m", "cloud_platform.worker"], worker_env, "1", "1Gi")
    science = container("runtime", runtime["runtime_image"], ["-c", BOOTSTRAP], runtime_env, "2", "2Gi")
    science["env"].extend({"name": key, "valueFrom": {"secretKeyRef": {"name": secret, "key": "latest"}}}
                          for key, secret in providers)
    return {"apiVersion": "run.googleapis.com/v1", "kind": "Job", "metadata": {"name": name},
        "spec": {"template": {"spec": {"taskCount": 1, "parallelism": 1, "template": {"spec": {
            "serviceAccountName": f"groundwork-worker@{project}.iam.gserviceaccount.com",
            "maxRetries": 0, "timeoutSeconds": "3600", "containers": [worker, science],
            "volumes": [{"name": "execution", "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"}}]}}}}}}


def binding_catalog(worker_image, worker_source, runtimes):
    worker_digest = image_digest(worker_image)
    return {"schema_version": 1, "worker": {"source_sha": worker_source,
            "image_digest": worker_digest, "image": worker_image},
        "runtimes": {digest: {**runtime, "job_name": paired_job_name(digest, worker_digest)}
                     for digest, runtime in sorted(runtimes.items())}}


def validate_existing_pair(actual, expected):
    name = job_name(actual)
    if runtime_from_job(actual) is None:
        raise ValueError("Existing Job does not declare a supported runtime")
    existing_task, wanted_task = task_spec(actual), task_spec(expected)
    existing = {container.get("name"): container for container in existing_task["containers"]}
    wanted = {container["name"]: container for container in wanted_task["containers"]}
    if set(existing) != set(wanted):
        raise ValueError(f"Immutable paired Job {name} has conflicting containers")
    for label in wanted:
        current, target = existing[label], wanted[label]
        for key in ("image", "command", "args", "volumeMounts"):
            if current.get(key) != target.get(key):
                raise ValueError(f"Immutable paired Job {name} has conflicting {label} {key}")
        current_env = environment(current)
        if any(current_env.get(key) != value for key, value in environment(target).items()):
            raise ValueError(f"Immutable paired Job {name} has conflicting {label} source/contract provenance")
        if label == "worker" and any(entry["name"].endswith("_API_KEY") for entry in current.get("env", [])):
            raise ValueError(f"Immutable paired Job {name} mounts provider secrets in the worker")
    if existing_task.get("volumes") != wanted_task["volumes"]:
        raise ValueError(f"Immutable paired Job {name} has a conflicting execution volume")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="cicl-487801")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--runtime-source-sha", required=True)
    parser.add_argument("--oauth-client-id", default="")
    parser.add_argument("--catalogue-sha256", required=True)
    parser.add_argument("--initialize-public", action="store_true")
    args = parser.parse_args()
    if not args.oauth_client_id.endswith(".apps.googleusercontent.com") or not SOURCE.fullmatch(args.source_sha):
        raise ValueError("Hosted release requires a Google web client and complete source SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", args.catalogue_sha256):
        raise ValueError("Hosted release requires a published catalogue content hash")
    project, region = args.project, args.region
    worker_image = resolve_image(project, args.image)
    current_runtime = resolve_image(project, args.runtime_image)
    worker_digest = image_digest(worker_image)
    jobs = json.loads(command(["run", "jobs", "list", "--project", project, "--region", region, "--format=json"], capture=True))
    runtimes = discover_runtimes(jobs, current_runtime, args.runtime_source_sha)
    providers = [(key, name) for key, name in PROVIDERS if exists(["secrets", "describe", name, "--project", project])]
    existing = {job_name(job): job for job in jobs}
    catalog = binding_catalog(worker_image, args.source_sha, runtimes)
    with tempfile.TemporaryDirectory(prefix="groundwork-release-") as directory:
        root = Path(directory)
        for runtime in catalog["runtimes"].values():
            manifest = job_manifest(project, region, worker_image, args.source_sha, runtime, providers)
            name = manifest["metadata"]["name"]
            if name in existing:
                validate_existing_pair(existing[name], manifest)
            else:
                path = root / (name + ".json")
                path.write_text(json.dumps(manifest))
                command(["run", "jobs", "replace", str(path), "--project", project, "--region", region, "--quiet"])
        payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(payload).hexdigest()
        catalog_key = f"platform/runtime-bindings/{worker_digest[7:]}/{checksum}.json"
        path = root / "catalog.json"
        path.write_bytes(payload)
        destination = f"gs://{project}-groundwork-artifacts/{catalog_key}"
        if exists(["storage", "objects", "describe", destination, "--project", project]):
            actual = command(["storage", "cat", destination], capture=True)
            if hashlib.sha256(actual.encode()).hexdigest() != checksum:
                raise RuntimeError("Existing immutable runtime catalog has conflicting contents")
        else:
            command(["storage", "cp", str(path), destination, "--if-generation-match=0", "--project", project], capture=True)
    public_url = service_url(project, region)
    current_binding = catalog["runtimes"][image_digest(current_runtime)]
    api_env = {"PLATFORM_BACKEND": "gcp", "GOOGLE_CLOUD_PROJECT": project, "PLATFORM_REGION": region,
        "PLATFORM_BUCKET": f"{project}-groundwork-artifacts", "PLATFORM_SOURCE_SHA": args.source_sha,
        "PLATFORM_IMAGE_DIGEST": worker_digest, "PLATFORM_WORKER_IMAGE": worker_image,
        "PLATFORM_CATALOGUE_SHA256": args.catalogue_sha256,
        "PLATFORM_WORKER_SOURCE_SHA": args.source_sha, "PLATFORM_WORKER_IMAGE_DIGEST": worker_digest,
        "PLATFORM_WORKER_CONTRACT_VERSION": "1", "PLATFORM_RUNTIME_CATALOG_KEY": catalog_key,
        "PLATFORM_RUNTIME_IMAGE": current_runtime, "PLATFORM_RUNTIME_DIGEST": image_digest(current_runtime),
        "PLATFORM_RUNTIME_SOURCE_SHA": current_binding["runtime_source_sha"],
        "PLATFORM_JOB_NAME": current_binding["job_name"], "PLATFORM_WORKSPACE": "/workspace",
        "PLATFORM_DATA_DIR": "/tmp/groundwork", "PLATFORM_MAX_PARALLELISM": "2", "PLATFORM_DEV_AUTH": "false",
        "GOOGLE_OAUTH_CLIENT_ID": args.oauth_client_id, "PLATFORM_TASK_QUEUE": "groundwork-dispatch",
        "PLATFORM_DISPATCH_AUDIENCE": public_url, "PLATFORM_DISPATCH_SA": f"groundwork-dispatch@{project}.iam.gserviceaccount.com",
        "PLATFORM_STATIC_DIR": "/opt/groundwork-web"}
    arguments = ["run", "deploy", "groundwork-control", "--project", project, "--region", region,
        "--image", worker_image, "--service-account", f"groundwork-api@{project}.iam.gserviceaccount.com",
        "--port=8080", "--cpu=1", "--memory=1Gi", "--concurrency=8", "--max-instances=4", "--min-instances=0",
        "--timeout=300s", "--set-secrets=SESSION_SECRET=groundwork-session:latest",
        "--set-env-vars", ",".join(f"{key}={value}" for key, value in api_env.items()), "--quiet"]
    if args.initialize_public:
        arguments.append("--allow-unauthenticated")
    command(arguments)
    print(json.dumps({"service_url": public_url, "source_sha": args.source_sha, "image": worker_image,
        "digest": worker_digest, "catalogue_sha256": args.catalogue_sha256, "runtime_image": current_runtime, "runtime_source_sha": current_binding["runtime_source_sha"],
        "runtime_catalog_key": catalog_key, "runtime_count": len(runtimes), "job": current_binding["job_name"]}, indent=2))


if __name__ == "__main__":
    main()
