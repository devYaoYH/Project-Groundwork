"""Publish one immutable image release and update the authenticated control pane.

Run bootstrap once as project administrator. Cloud Build subsequently needs only
the groundwork-build identity and cannot change IAM or read provider secrets.
"""
import argparse
import json
import re
from common import command, exists, service_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="cicl-487801")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--oauth-client-id", default="")
    parser.add_argument("--initialize-public", action="store_true",
                        help="Administrator-only first deployment IAM setup")
    args = parser.parse_args()
    if not args.oauth_client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("A Google web OAuth client ID is required for hosted login")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.source_sha):
        raise ValueError("source-sha must identify the complete source commit")
    project, region = args.project, args.region
    image = args.image
    if "@sha256:" not in image:
        info = json.loads(command(["artifacts", "docker", "images", "describe", image,
                                  "--project", project, "--format=json"], capture=True))
        digest = info.get("image_summary", info.get("imageSummary", {})).get("digest")
        if not digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("Artifact Registry did not return a valid image digest")
        image = image.rsplit(":", 1)[0] + "@" + digest
    digest = image.split("@", 1)[1]
    # A rebuild of the same commit can resolve different dependency versions.
    # Keep both image identities addressable rather than updating an old Job.
    job = "groundwork-ep-" + args.source_sha[:12] + "-" + digest.removeprefix("sha256:")[:12]
    api = "groundwork-control"
    public_url = service_url(project, region, api)
    common_env = {
        "PLATFORM_BACKEND": "gcp", "GOOGLE_CLOUD_PROJECT": project,
        "PLATFORM_REGION": region, "PLATFORM_BUCKET": f"{project}-groundwork-artifacts",
        "PLATFORM_SOURCE_SHA": args.source_sha, "PLATFORM_IMAGE_DIGEST": digest,
        "PLATFORM_JOB_NAME": job, "PLATFORM_WORKSPACE": "/workspace",
        "PLATFORM_DATA_DIR": "/tmp/groundwork", "A2A_RUNTIME_MODE": "loopback",
        "PLATFORM_MAX_PARALLELISM": "2", "PLATFORM_DEV_AUTH": "false",
    }
    worker_env = ",".join(f"{key}={value}" for key, value in common_env.items())
    secrets = []
    for environment, name in (("OPENAI_API_KEY", "groundwork-openai-api-key"),
                              ("ANTHROPIC_API_KEY", "groundwork-anthropic-api-key"),
                              ("GOOGLE_API_KEY", "groundwork-google-api-key"),
                              ("OPENROUTER_API_KEY", "groundwork-openrouter-api-key")):
        if exists(["secrets", "describe", name, "--project", project]):
            secrets.append(f"{environment}={name}:latest")
    job_args = ["run", "jobs", "deploy", job, "--project", project, "--region", region,
                "--image", image, "--service-account", f"groundwork-worker@{project}.iam.gserviceaccount.com",
                "--command=python", "--args=-m,cloud_platform.worker", "--tasks=1", "--parallelism=1",
                "--max-retries=0", "--task-timeout=3600s", "--cpu=2", "--memory=2Gi",
                "--set-env-vars", worker_env, "--quiet"]
    if secrets:
        job_args += ["--set-secrets", ",".join(secrets)]
    command(job_args)
    api_env = {**common_env, "GOOGLE_OAUTH_CLIENT_ID": args.oauth_client_id,
               "PLATFORM_TASK_QUEUE": "groundwork-dispatch",
               "PLATFORM_DISPATCH_AUDIENCE": public_url,
               "PLATFORM_DISPATCH_SA": f"groundwork-dispatch@{project}.iam.gserviceaccount.com",
               "PLATFORM_STATIC_DIR": "/opt/groundwork-web"}
    api_args = ["run", "deploy", api, "--project", project, "--region", region,
                "--image", image, "--service-account", f"groundwork-api@{project}.iam.gserviceaccount.com",
                "--port=8080", "--cpu=1", "--memory=1Gi", "--concurrency=8", "--max-instances=4",
                "--min-instances=0", "--timeout=300s", "--set-secrets=SESSION_SECRET=groundwork-session:latest",
                "--set-env-vars", ",".join(f"{key}={value}" for key, value in api_env.items()), "--quiet"]
    if args.initialize_public:
        api_args.append("--allow-unauthenticated")
    command(api_args)
    url = command(["run", "services", "describe", api, "--project", project, "--region", region,
                   "--format=value(status.url)"], capture=True)
    print(json.dumps({"service_url": public_url, "infrastructure_url": url,
                      "dispatch_audience": public_url, "job": job, "source_sha": args.source_sha,
                      "image": image, "digest": digest}, indent=2))


if __name__ == "__main__":
    main()
