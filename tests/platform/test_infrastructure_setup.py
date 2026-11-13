"""Exercise administrator setup without credentials or changing real IAM."""

import ast
import json
import sys
from pathlib import Path

import pytest

from deploy import bootstrap, configure_github, setup_infrastructure

ROOT = Path(__file__).resolve().parents[2]


def test_bootstrap_creates_resources_without_competing_iam_writers(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--project", "test-project"])
    monkeypatch.setattr(bootstrap, "command", lambda args, **kw: calls.append(args) or "existing")
    monkeypatch.setattr(bootstrap, "exists", lambda args: True)
    monkeypatch.setattr(
        bootstrap,
        "service_account",
        lambda project, name: f"{name}@{project}.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(bootstrap, "bucket", lambda *args: None)
    bootstrap.main()
    assert not any("add-iam-policy-binding" in args for args in calls)
    assert not any(args[:2] == ["iam", "roles"] for args in calls)
    assert not hasattr(bootstrap, "enable_vertex_adc")
    # Inspect calls too, so an unused competing writer cannot silently remain.
    tree = ast.parse((ROOT / "deploy/bootstrap.py").read_text())
    assert not any(
        isinstance(node, ast.Name) and node.id in {"grant", "bucket_grant"}
        for node in ast.walk(tree)
    )


def test_backend_setup_never_creates_an_iam_identity_or_grants(monkeypatch):
    calls, buckets = [], []
    monkeypatch.setattr(
        sys, "argv", ["setup", "--project", "test-project", "--state-bucket", "private-state"]
    )
    monkeypatch.setattr(setup_infrastructure, "command", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(setup_infrastructure, "bucket", lambda *args: buckets.append(args))
    setup_infrastructure.main()
    assert buckets == [("test-project", "private-state", "us-central1")]
    assert all(args[0] in ("services", "storage") for args in calls)
    assert not any("add-iam-policy-binding" in args for args in calls)
    update = next(args for args in calls if args[:3] == ["storage", "buckets", "update"])
    assert {"--versioning", "--uniform-bucket-level-access", "--public-access-prevention"} <= set(
        update
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("verification_branch", [None, "codex/verification"])
def test_federation_grants_only_the_application_account(monkeypatch, existing, verification_branch):
    calls, github_calls = [], []
    argv = [
        "configure",
        "--project",
        "test-project",
        "--repository",
        "owner/repo",
        "--oauth-client-id",
        "client.apps.googleusercontent.com",
    ]
    if verification_branch:
        argv += ["--verification-branch", verification_branch]
    monkeypatch.setattr(sys, "argv", argv)

    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "projects":
            return "1234"
        if "list" in args:
            return "[]"
        return ""

    def github(args, **kwargs):
        github_calls.append(args)
        return json.dumps({"id": 42, "owner": {"id": 43}})

    monkeypatch.setattr(configure_github, "command", command)
    monkeypatch.setattr(configure_github, "exists", lambda args: existing)
    monkeypatch.setattr(configure_github, "github", github)
    configure_github.main()
    providers = [
        args for args in calls if args[:3] == ["iam", "workload-identity-pools", "providers"]
    ]
    assert len(providers) == 1
    args = providers[0]
    assert args[3] == ("update-oidc" if existing else "create-oidc")
    assert args[args.index("--workload-identity-pool") + 1] == "groundwork-github"
    trust = args[args.index("--attribute-condition") + 1]
    assert "assertion.repository_id == '42'" in trust
    assert "assertion.repository_owner_id == '43'" in trust
    assert "assertion.event_name in ['push', 'workflow_dispatch']" in trust
    assert "owner/repo/.github/workflows/platform.yml@refs/heads/main" in trust
    assert ("refs/heads/codex/verification" in trust) is bool(verification_branch)
    bindings = [args for args in calls if "add-iam-policy-binding" in args]
    assert len(bindings) == 1
    assert bindings[0][3] == "groundwork-ci@test-project.iam.gserviceaccount.com"
    assert "--role=roles/iam.workloadIdentityUser" in bindings[0]
    assert bindings[0][-1].endswith(
        "/workloadIdentityPools/groundwork-github/attribute.repository_id/42"
    )
    variables = {args[2]: args[-1] for args in github_calls if args[:2] == ["variable", "set"]}
    assert set(variables) == {
        "GCP_PROJECT",
        "GCP_REGION",
        "GCP_WORKLOAD_IDENTITY_PROVIDER",
        "GCP_CI_SERVICE_ACCOUNT",
        "GOOGLE_OAUTH_CLIENT_ID",
    }
    assert not any(
        "groundwork-infra@" in str(args) and "add-iam-policy-binding" in args for args in calls
    )


@pytest.mark.parametrize("state", ["ACTIVE", "DISABLED"])
def test_legacy_infrastructure_pool_blocks_setup_before_any_mutation(monkeypatch, state):
    calls = []
    monkeypatch.setattr(
        sys, "argv", ["configure", "--project", "test-project", "--oauth-client-id", "client"]
    )
    monkeypatch.setattr(
        configure_github, "github", lambda *a, **k: json.dumps({"id": 42, "owner": {"id": 43}})
    )

    def command(args, **kwargs):
        calls.append(args)
        return (
            "1234"
            if args[0] == "projects"
            else json.dumps(
                [
                    {
                        "name": "projects/1234/locations/global/workloadIdentityPools/groundwork-infrastructure",
                        "state": state,
                    }
                ]
            )
        )

    monkeypatch.setattr(configure_github, "command", command)
    with pytest.raises(ValueError, match="maintainer must revoke"):
        configure_github.main()
    assert len(calls) == 2
    assert all("create" not in args and "add-iam-policy-binding" not in args for args in calls)


def test_alternate_workload_pool_cannot_keep_infrastructure_impersonation(monkeypatch):
    def command(args, **kwargs):
        if args[1:3] == ["workload-identity-pools", "list"]:
            return "[]"
        if args[1:3] == ["service-accounts", "list"]:
            return json.dumps([{"email": "groundwork-infra@test-project.iam.gserviceaccount.com"}])
        assert args[1:3] == ["service-accounts", "get-iam-policy"]
        return json.dumps(
            {
                "bindings": [
                    {
                        "role": "roles/iam.serviceAccountTokenCreator",
                        "members": [
                            "principalSet://iam.googleapis.com/projects/1234/locations/global/workloadIdentityPools/other/attribute.repository_id/42"
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(configure_github, "command", command)
    with pytest.raises(ValueError, match="still trusts application/workload identities"):
        configure_github.require_application_only_trust("test-project")


def test_federation_audit_permission_failure_is_not_treated_as_absence(monkeypatch):
    import subprocess

    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr="PERMISSION_DENIED")

    monkeypatch.setattr(configure_github, "command", denied)
    with pytest.raises(subprocess.CalledProcessError):
        configure_github.require_application_only_trust("test-project")


def test_terraform_owns_only_additive_members_roles_and_vertex_service():
    source = (ROOT / "deploy/terraform/main.tf").read_text()
    assert 'resource "google_project_iam_policy"' not in source
    assert '_iam_binding"' not in source
    assert "prevent_destroy = true" in source
    assert "google_secret_manager_secret_version" not in source
    assert 'resource "google_service_account"' not in source
    assert 'resource "google_storage_bucket"' not in source
    assert 'resource "google_artifact_registry_repository"' not in source
    assert "credentials" not in (ROOT / "deploy/terraform/versions.tf").read_text()


def test_terraform_scratch_files_cannot_enter_source_uploads_or_image_contexts():
    assert "#!include:.gitignore" in (ROOT / ".gcloudignore").read_text()
    gitignore = (ROOT / ".gitignore").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text()
    for pattern in [
        ".terraform",
        "*.tfstate",
        "*.tfstate.*",
        "*.tfplan",
        "*.tfvars",
        "*.tfvars.json",
        "*.tfbackend",
    ]:
        assert pattern in gitignore and pattern in dockerignore


@pytest.mark.parametrize(
    "member,blocked",
    [
        ("user:maintainer@example.com", False),
        ("serviceAccount:groundwork-ci@test-project.iam.gserviceaccount.com", True),
        ("serviceAccount:groundwork-build@test-project.iam.gserviceaccount.com", True),
    ],
)
def test_existing_infrastructure_account_cannot_delegate_to_application_accounts(
    monkeypatch, member, blocked
):
    def command(args, **kwargs):
        if args[1:3] == ["workload-identity-pools", "list"]:
            return "[]"
        if args[1:3] == ["service-accounts", "list"]:
            return json.dumps([{"email": "groundwork-infra@test-project.iam.gserviceaccount.com"}])
        return json.dumps(
            {"bindings": [{"role": "roles/iam.serviceAccountUser", "members": [member]}]}
        )

    monkeypatch.setattr(configure_github, "command", command)
    if blocked:
        with pytest.raises(ValueError, match="application/workload identities"):
            configure_github.require_application_only_trust("test-project")
    else:
        configure_github.require_application_only_trust("test-project")
