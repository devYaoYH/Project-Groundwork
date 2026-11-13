"""Prepare the private backend for maintainer-run Terraform.

Run after bootstrap as a human administrator. This creates no infrastructure
service account, management role, federation trust, or application IAM grants.
Existing backend access must be audited before adopting this revision.
"""

import argparse

try:
    from .common import bucket, command
except ImportError:
    from common import bucket, command


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
            "storage.googleapis.com",
            "--project",
            project,
            "--quiet",
        ]
    )
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
    print(f"Maintainer state backend: gs://{state_bucket}/groundwork/iam")
    print("Review bucket IAM before use. GitHub and application identities must have no access.")


if __name__ == "__main__":
    main()
