"""Provision only named Groundwork resources; existing project apps are untouched."""
import argparse
import json
import secrets

try:
    from .common import bucket, bucket_grant, command, exists, grant, service_account
except ImportError:
    from common import bucket, bucket_grant, command, exists, grant, service_account


def enable_vertex_adc(project, runtime_account):
    """Grant prediction-only ADC access to the Cloud Run runtime identity."""
    command(["services", "enable", "aiplatform.googleapis.com", "--project", project, "--quiet"])
    role = "groundworkVertexPredictor"
    action = "update" if exists(["iam", "roles", "describe", role, "--project", project]) else "create"
    command(["iam", "roles", action, role, "--project", project,
             "--title=Groundwork Vertex predictor", "--stage=GA",
             "--permissions=aiplatform.endpoints.predict,serviceusage.services.use", "--quiet"], capture=True)
    grant(project, f"serviceAccount:{runtime_account}", f"projects/{project}/roles/{role}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="cicl-487801")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--vertex-adc", action="store_true",
                        help="Enable Vertex prediction-only access for the runtime identity.")
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
    if args.vertex_adc:
        enable_vertex_adc(project, accounts["worker"])
    for name in ("api", "worker"):
        grant(project, f"serviceAccount:{accounts[name]}", "roles/datastore.user")
        grant(project, f"serviceAccount:{accounts[name]}", "roles/logging.logWriter")
    grant(project, f"serviceAccount:{accounts['api']}", "roles/cloudtasks.enqueuer")
    grant(project, f"serviceAccount:{accounts['api']}", "roles/run.jobsExecutorWithOverrides")
    command(["iam", "service-accounts", "add-iam-policy-binding", accounts["dispatch"],
             "--project", project, "--member", f"serviceAccount:{accounts['api']}",
             "--role", "roles/iam.serviceAccountUser"], capture=True)
    for name in ("api", "worker"):
        command(["iam", "service-accounts", "add-iam-policy-binding", accounts[name],
                 "--project", project, "--member", f"serviceAccount:{accounts['build']}",
                 "--role", "roles/iam.serviceAccountUser"], capture=True)
    for role in ("roles/run.developer", "roles/logging.logWriter"):
        grant(project, f"serviceAccount:{accounts['build']}", role)
    # Catalogue publication is create-only plus read-back verification. This
    # identity cannot update/delete documents or scan private collections.
    catalogue_role = "groundworkCataloguePublisher"
    role_args = ["iam", "roles"]
    action = "update" if exists([*role_args, "describe", catalogue_role, "--project", project]) else "create"
    command([*role_args, action, catalogue_role, "--project", project,
             "--title=Groundwork catalogue publisher", "--stage=GA",
             "--permissions=datastore.entities.create,datastore.entities.get", "--quiet"], capture=True)
    grant(project, f"serviceAccount:{accounts['build']}", f"projects/{project}/roles/{catalogue_role}")
    grant(project, f"serviceAccount:{accounts['ci']}", "roles/cloudbuild.builds.editor")
    for name in ("build", "ci"):
        grant(project, f"serviceAccount:{accounts[name]}", "roles/serviceusage.serviceUsageConsumer")
    command(["iam", "service-accounts", "add-iam-policy-binding", accounts["build"],
             "--project", project, "--member", f"serviceAccount:{accounts['ci']}",
             "--role", "roles/iam.serviceAccountUser"], capture=True)
    repository = "groundwork"
    if not exists(["artifacts", "repositories", "describe", repository, "--project", project, "--location", region]):
        command(["artifacts", "repositories", "create", repository, "--project", project,
                 "--location", region, "--repository-format=docker", "--quiet"])
    # Cloud Build alone writes images/caches; GitHub downloads candidates for tests.
    for identity, role in (("build", "roles/artifactregistry.writer"),
                           ("ci", "roles/artifactregistry.reader")):
        command(["artifacts", "repositories", "add-iam-policy-binding", repository,
                 "--project", project, "--location", region,
                 "--member", f"serviceAccount:{accounts[identity]}",
                 "--role", role], capture=True)
    runtime_repository = "groundwork-runtime"
    if not exists(["artifacts", "repositories", "describe", runtime_repository, "--project", project, "--location", region]):
        command(["artifacts", "repositories", "create", runtime_repository, "--project", project,
                 "--location", region, "--repository-format=docker", "--immutable-tags", "--quiet"])
    else:
        command(["artifacts", "repositories", "update", runtime_repository, "--project", project,
                 "--location", region, "--immutable-tags", "--quiet"])
    for identity, role in (("build", "roles/artifactregistry.writer"),
                           ("ci", "roles/artifactregistry.reader")):
        command(["artifacts", "repositories", "add-iam-policy-binding", runtime_repository,
                 "--project", project, "--location", region, "--member", f"serviceAccount:{accounts[identity]}",
                 "--role", role], capture=True)
    artifact_bucket, source_bucket = f"{project}-groundwork-artifacts", f"{project}-groundwork-build-source"
    bucket(project, artifact_bucket, region)
    bucket(project, source_bucket, region)
    for name in ("api", "worker"):
        bucket_grant(project, artifact_bucket, f"serviceAccount:{accounts[name]}", "roles/storage.objectAdmin")
    # The build can publish/read catalogs, but cannot read experiment evidence.
    binding_prefix = f"projects/_/buckets/{artifact_bucket}/objects/platform/runtime-bindings/"
    for role in ("roles/storage.objectCreator", "roles/storage.objectViewer"):
        command(["storage", "buckets", "add-iam-policy-binding", f"gs://{artifact_bucket}",
                 "--project", project, "--member", f"serviceAccount:{accounts['build']}", "--role", role,
                 "--condition", f"expression=resource.name.startsWith('{binding_prefix}'),title=groundwork-runtime-catalog"], capture=True)
    bucket_grant(project, source_bucket, f"serviceAccount:{accounts['ci']}", "roles/storage.objectAdmin")
    # builds submit checks source bucket metadata before uploading; object roles
    # do not include storage.buckets.get. Scope the extra read to this bucket.
    bucket_grant(project, source_bucket, f"serviceAccount:{accounts['ci']}", "roles/storage.legacyBucketReader")
    bucket_grant(project, source_bucket, f"serviceAccount:{accounts['build']}", "roles/storage.objectViewer")
    # Candidate manifests only; the build cannot upload arbitrary source objects.
    candidate_prefix = f"projects/_/buckets/{source_bucket}/objects/ci-image-builds/"
    command(["storage", "buckets", "add-iam-policy-binding", f"gs://{source_bucket}",
             "--project", project, "--member", f"serviceAccount:{accounts['build']}",
             "--role", "roles/storage.objectCreator", "--condition",
             f"expression=resource.name.startsWith('{candidate_prefix}'),title=groundwork-ci-images"], capture=True)
    session_secret = "groundwork-session"
    if not exists(["secrets", "describe", session_secret, "--project", project]):
        command(["secrets", "create", session_secret, "--project", project,
                 "--replication-policy=automatic", "--quiet"])
    if not command(["secrets", "versions", "list", session_secret, "--project", project,
                    "--filter=state:ENABLED", "--format=value(name)", "--limit=1"], capture=True):
        command(["secrets", "versions", "add", session_secret, "--project", project,
                 "--data-file=-"], stdin=secrets.token_urlsafe(48), capture=True)
    command(["secrets", "add-iam-policy-binding", session_secret, "--project", project,
             "--member", f"serviceAccount:{accounts['api']}",
             "--role", "roles/secretmanager.secretAccessor"], capture=True)
    for name in ("groundwork-openai-api-key", "groundwork-anthropic-api-key",
                 "groundwork-google-api-key", "groundwork-openrouter-api-key"):
        if exists(["secrets", "describe", name, "--project", project]):
            for account, role in (("worker", "roles/secretmanager.secretAccessor"),
                                  ("build", "roles/secretmanager.viewer")):
                command(["secrets", "add-iam-policy-binding", name, "--project", project,
                         "--member", f"serviceAccount:{accounts[account]}",
                         "--role", role], capture=True)
    queue = "groundwork-dispatch"
    if not exists(["tasks", "queues", "describe", queue, "--project", project, "--location", region]):
        command(["tasks", "queues", "create", queue, "--project", project, "--location", region,
                 "--max-concurrent-dispatches=5", "--max-dispatches-per-second=5",
                 "--max-attempts=10", "--min-backoff=5s", "--max-backoff=300s"])
    print(json.dumps({"project": project, "region": region, "artifact_bucket": artifact_bucket,
                      "source_bucket": source_bucket, "accounts": accounts, "queue": queue}, indent=2))


if __name__ == "__main__":
    main()
