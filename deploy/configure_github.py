"""Configure application-only GitHub OIDC; Terraform remains maintainer-run.

The deployment workflow on main is trusted. --verification-branch allows exactly
one additional branch for initial verification; rerun without it after merging.
"""

import argparse
import json
import subprocess

try:
    from .common import command, exists
except ImportError:
    from common import command, exists


def github(arguments, capture=False):
    result = subprocess.run(["gh", *arguments], check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def configure_provider(project, repository, repo, pool, provider, branches):
    if not exists(
        [
            "iam",
            "workload-identity-pools",
            "describe",
            pool,
            "--project",
            project,
            "--location=global",
        ]
    ):
        command(
            [
                "iam",
                "workload-identity-pools",
                "create",
                pool,
                "--project",
                project,
                "--location=global",
                "--display-name=Groundwork GitHub CI",
            ]
        )
    workflow_conditions = []
    for branch in branches:
        # Serialize CEL string literals rather than interpolating shell text.
        ref = json.dumps(f"refs/heads/{branch}")
        workflow = json.dumps(f"{repository}/.github/workflows/platform.yml@refs/heads/{branch}")
        workflow_conditions.append(
            f"(assertion.ref == {ref} && assertion.workflow_ref == {workflow})"
        )
    condition = (
        f"assertion.repository_id == '{repo['id']}' && "
        f"assertion.repository_owner_id == '{repo['owner']['id']}' && "
        "assertion.event_name in ['push', 'workflow_dispatch'] && ("
        + " || ".join(workflow_conditions)
        + ")"
    )
    mapping = (
        "google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,"
        "attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref,"
        "attribute.workflow_ref=assertion.workflow_ref,attribute.event_name=assertion.event_name"
    )
    base = ["--project", project, "--location=global", "--workload-identity-pool", pool]
    update = exists(["iam", "workload-identity-pools", "providers", "describe", provider, *base])
    command(
        [
            "iam",
            "workload-identity-pools",
            "providers",
            "update-oidc" if update else "create-oidc",
            provider,
            *base,
            "--issuer-uri=https://token.actions.githubusercontent.com",
            "--attribute-mapping",
            mapping,
            "--attribute-condition",
            condition,
        ]
    )


def require_application_only_trust(project):
    """Fail before changing trust/variables if the retired GitHub IAM path remains.

    Checked list/read calls distinguish permission failures from missing objects.
    Alternate project-wide or custom-role grants still need the maintainer audit
    documented in terraform/README.md; this is not an effective-IAM proof.
    """
    pools = json.loads(
        command(
            [
                "iam",
                "workload-identity-pools",
                "list",
                "--project",
                project,
                "--location=global",
                "--format=json",
            ],
            capture=True,
        )
    )
    if any(
        pool["name"].endswith("/groundwork-infrastructure") and pool.get("state") != "DELETED"
        for pool in pools
    ):
        raise ValueError(
            "Retired infrastructure federation remains. A maintainer must revoke "
            "GitHub infrastructure access using deploy/terraform/README.md before "
            "configuring application releases. No IAM repair is run by CI."
        )
    accounts = json.loads(
        command(
            [
                "iam",
                "service-accounts",
                "list",
                "--project",
                project,
                "--format=json",
            ],
            capture=True,
        )
    )
    infrastructure = f"groundwork-infra@{project}.iam.gserviceaccount.com"
    if any(account["email"] == infrastructure for account in accounts):
        policy = json.loads(
            command(
                [
                    "iam",
                    "service-accounts",
                    "get-iam-policy",
                    infrastructure,
                    "--project",
                    project,
                    "--format=json",
                ],
                capture=True,
            )
        )
        application_accounts = {
            f"serviceAccount:groundwork-{name}@{project}.iam.gserviceaccount.com"
            for name in ("api", "worker", "dispatch", "build", "ci")
        }
        if any(
            "/workloadIdentityPools/" in member or member in application_accounts
            for binding in policy.get("bindings", [])
            for member in binding.get("members", [])
        ):
            raise ValueError(
                "Infrastructure account still trusts application/workload identities. Revoke "
                "that binding as a maintainer; application setup cannot retain it."
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--repository", default="devYaoYH/Project-Groundwork")
    parser.add_argument("--oauth-client-id", required=True)
    parser.add_argument("--verification-branch")
    args = parser.parse_args()
    repo = json.loads(github(["api", f"repos/{args.repository}"], capture=True))
    project, pool, provider = args.project, "groundwork-github", "github"
    number = command(
        ["projects", "describe", project, "--format=value(projectNumber)"], capture=True
    )
    branches = ["main"]
    if args.verification_branch and args.verification_branch != "main":
        branches.append(args.verification_branch)
    require_application_only_trust(project)
    configure_provider(project, args.repository, repo, pool, provider, branches)
    ci = f"groundwork-ci@{project}.iam.gserviceaccount.com"
    principal = f"principalSet://iam.googleapis.com/projects/{number}/locations/global/workloadIdentityPools/{pool}/attribute.repository_id/{repo['id']}"
    command(
        [
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            ci,
            "--project",
            project,
            "--role=roles/iam.workloadIdentityUser",
            "--member",
            principal,
        ],
        capture=True,
    )
    resource = (
        f"projects/{number}/locations/global/workloadIdentityPools/{pool}/providers/{provider}"
    )
    variables = {
        "GCP_PROJECT": project,
        "GCP_REGION": args.region,
        "GCP_WORKLOAD_IDENTITY_PROVIDER": resource,
        "GCP_CI_SERVICE_ACCOUNT": ci,
        "GOOGLE_OAUTH_CLIENT_ID": args.oauth_client_id,
    }
    for name, value in variables.items():
        github(["variable", "set", name, "--repo", args.repository, "--body", value])
    print(
        json.dumps(
            {
                "repository": args.repository,
                "provider": resource,
                "trusted_branches": branches,
                "service_account": ci,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
