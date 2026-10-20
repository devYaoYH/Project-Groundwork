"""Administrator-only root of trust for Terraform; never run by release CI.

This owns the infrastructure identity, its management role/grant, and the state
bucket/grant. Terraform must not manage its own trust or backend. No secret values
are read and no application IAM is changed here.
"""

import argparse

try:
    from .common import bucket, bucket_grant, command, exists, grant, service_account
except ImportError:
    from common import bucket, bucket_grant, command, exists, grant, service_account

# IAM policy management on this project and its resources, with no secret-value,
# image-write, application-deploy, or model-invocation permission.
PERMISSIONS = (
    "resourcemanager.projects.get",
    "resourcemanager.projects.getIamPolicy",
    "resourcemanager.projects.setIamPolicy",
    "iam.roles.create",
    "iam.roles.get",
    "iam.roles.list",
    "iam.roles.update",
    "iam.roles.delete",
    "iam.roles.undelete",
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "artifactregistry.repositories.getIamPolicy",
    "artifactregistry.repositories.setIamPolicy",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.setIamPolicy",
    "serviceusage.operations.get",
    "serviceusage.services.get",
    "serviceusage.services.list",
    "serviceusage.services.enable",
    "serviceusage.services.use",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--state-bucket")
    args = parser.parse_args()
    project = args.project
    command(
        [
            "services",
            "enable",
            "iam.googleapis.com",
            "iamcredentials.googleapis.com",
            "sts.googleapis.com",
            "cloudresourcemanager.googleapis.com",
            "serviceusage.googleapis.com",
            "storage.googleapis.com",
            "--project",
            project,
            "--quiet",
        ]
    )
    account = service_account(project, "groundwork-infra")
    role = "groundworkInfrastructureManager"
    action = (
        "update" if exists(["iam", "roles", "describe", role, "--project", project]) else "create"
    )
    command(
        [
            "iam",
            "roles",
            action,
            role,
            "--project",
            project,
            "--title=Groundwork infrastructure manager",
            "--stage=GA",
            "--permissions=" + ",".join(PERMISSIONS),
            "--quiet",
        ],
        capture=True,
    )
    grant(project, f"serviceAccount:{account}", f"projects/{project}/roles/{role}")
    state_bucket = args.state_bucket or f"{project}-groundwork-tfstate"
    bucket(project, state_bucket, args.region)
    command(
        [
            "storage",
            "buckets",
            "update",
            f"gs://{state_bucket}",
            "--project",
            project,
            "--versioning",
            "--uniform-bucket-level-access",
            "--public-access-prevention",
        ],
        capture=True,
    )
    bucket_grant(project, state_bucket, f"serviceAccount:{account}", "roles/storage.objectAdmin")
    print(f"State backend: gs://{state_bucket}/groundwork/iam (versioned, native GCS locking)")


if __name__ == "__main__":
    main()
