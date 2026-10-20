"""Create named Groundwork resources. Application IAM lives in deploy/terraform.

Run Terraform adoption/reconciliation before the first application release.
"""
import argparse
import json
import secrets

try:
    from .common import bucket, command, exists, service_account
except ImportError:
    from common import bucket, command, exists, service_account


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="cicl-487801")
    parser.add_argument("--region", default="us-central1")
    args = parser.parse_args()
    project, region = args.project, args.region
    command(["services", "enable", "run.googleapis.com", "cloudbuild.googleapis.com",
             "artifactregistry.googleapis.com", "firestore.googleapis.com",
             "cloudtasks.googleapis.com", "secretmanager.googleapis.com",
             "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
             "cloudresourcemanager.googleapis.com",
             "--project", project, "--quiet"])
    accounts = {name: service_account(project, f"groundwork-{name}")
                for name in ("api", "worker", "dispatch", "build", "ci")}
    repository = "groundwork"
    if not exists(["artifacts", "repositories", "describe", repository, "--project", project, "--location", region]):
        command(["artifacts", "repositories", "create", repository, "--project", project,
                 "--location", region, "--repository-format=docker", "--quiet"])
    runtime_repository = "groundwork-runtime"
    if not exists(["artifacts", "repositories", "describe", runtime_repository, "--project", project, "--location", region]):
        command(["artifacts", "repositories", "create", runtime_repository, "--project", project,
                 "--location", region, "--repository-format=docker", "--immutable-tags", "--quiet"])
    else:
        command(["artifacts", "repositories", "update", runtime_repository, "--project", project,
                 "--location", region, "--immutable-tags", "--quiet"])
    artifact_bucket, source_bucket = f"{project}-groundwork-artifacts", f"{project}-groundwork-build-source"
    bucket(project, artifact_bucket, region)
    bucket(project, source_bucket, region)
    session_secret = "groundwork-session"
    if not exists(["secrets", "describe", session_secret, "--project", project]):
        command(["secrets", "create", session_secret, "--project", project,
                 "--replication-policy=automatic", "--quiet"])
    if not command(["secrets", "versions", "list", session_secret, "--project", project,
                    "--filter=state:ENABLED", "--format=value(name)", "--limit=1"], capture=True):
        command(["secrets", "versions", "add", session_secret, "--project", project,
                 "--data-file=-"], stdin=secrets.token_urlsafe(48), capture=True)
    queue = "groundwork-dispatch"
    if not exists(["tasks", "queues", "describe", queue, "--project", project, "--location", region]):
        command(["tasks", "queues", "create", queue, "--project", project, "--location", region,
                 "--max-concurrent-dispatches=5", "--max-dispatches-per-second=5",
                 "--max-attempts=10", "--min-backoff=5s", "--max-backoff=300s"])
    print(json.dumps({"project": project, "region": region, "artifact_bucket": artifact_bucket,
                      "source_bucket": source_bucket, "accounts": accounts, "queue": queue}, indent=2))


if __name__ == "__main__":
    main()
