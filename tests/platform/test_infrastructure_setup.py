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


@pytest.mark.parametrize("existing", [False, True])
def test_infrastructure_root_of_trust_is_separate_and_state_bucket_is_private(
    monkeypatch, existing
):
    calls, grants, bucket_grants = [], [], []
    monkeypatch.setattr(
        sys, "argv", ["setup", "--project", "test-project", "--state-bucket", "private-state"]
    )
    monkeypatch.setattr(setup_infrastructure, "command", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(setup_infrastructure, "exists", lambda args: existing)
    monkeypatch.setattr(
        setup_infrastructure,
        "service_account",
        lambda project, name: f"{name}@{project}.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(setup_infrastructure, "bucket", lambda *args: None)
    monkeypatch.setattr(setup_infrastructure, "grant", lambda *args: grants.append(args))
    monkeypatch.setattr(
        setup_infrastructure, "bucket_grant", lambda *args: bucket_grants.append(args)
    )
    setup_infrastructure.main()
    role = next(args for args in calls if args[:2] == ["iam", "roles"])
    assert role[2:4] == ["update" if existing else "create", "groundworkInfrastructureManager"]
    assert grants == [
        (
            "test-project",
            "serviceAccount:groundwork-infra@test-project.iam.gserviceaccount.com",
            "projects/test-project/roles/groundworkInfrastructureManager",
        )
    ]
    assert bucket_grants == [
        (
            "test-project",
            "private-state",
            "serviceAccount:groundwork-infra@test-project.iam.gserviceaccount.com",
            "roles/storage.objectAdmin",
        )
    ]
    bucket_update = next(args for args in calls if args[:3] == ["storage", "buckets", "update"])
    assert {"--versioning", "--uniform-bucket-level-access", "--public-access-prevention"} <= set(
        bucket_update
    )
    permissions = set(setup_infrastructure.PERMISSIONS)
    assert {
        "resourcemanager.projects.getIamPolicy",
        "resourcemanager.projects.setIamPolicy",
        "iam.roles.update",
        "serviceusage.services.enable",
        "serviceusage.operations.get",
    } <= permissions
    assert not permissions & {
        "aiplatform.endpoints.predict",
        "secretmanager.versions.access",
        "run.services.update",
        "storage.objects.get",
        "artifactregistry.repositories.uploadArtifacts",
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("secret_names", [None, [], ["groundwork-openai-api-key"]])
def test_federation_keeps_verification_branches_out_of_the_infrastructure_pool(
    monkeypatch, existing, secret_names
):
    calls, github_calls = [], []
    argv = [
        "configure",
        "--project",
        "test-project",
        "--repository",
        "owner/repo",
        "--oauth-client-id",
        "client.apps.googleusercontent.com",
        "--verification-branch",
        "codex/verification",
        "--terraform-state-bucket",
        "private-state",
    ]
    if secret_names is not None:
        argv.extend(["--provider-secret-names", *secret_names])
    monkeypatch.setattr(sys, "argv", argv)

    def command(args, **kwargs):
        calls.append(args)
        return "1234" if args[0] == "projects" else ""

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
    assert len(providers) == 2
    for args in providers:
        assert args[3] == ("update-oidc" if existing else "create-oidc")
        pool = args[args.index("--workload-identity-pool") + 1]
        trust = args[args.index("--attribute-condition") + 1]
        assert "assertion.repository_id == '42'" in trust
        assert "assertion.repository_owner_id == '43'" in trust
        assert "assertion.event_name in ['push', 'workflow_dispatch']" in trust
        assert "owner/repo/.github/workflows/platform.yml@refs/heads/main" in trust
        assert ("refs/heads/codex/verification" in trust) is (pool == "groundwork-github")
    bindings = [args for args in calls if "add-iam-policy-binding" in args]
    assert len(bindings) == 2
    for args in bindings:
        account = args[3]
        principal = args[args.index("--member") + 1]
        assert "--role=roles/iam.workloadIdentityUser" in args
        expected_pool = (
            "groundwork-infrastructure"
            if account.startswith("groundwork-infra@")
            else "groundwork-github"
        )
        assert f"/workloadIdentityPools/{expected_pool}/attribute.repository_id/42" in principal
    variables = {args[2]: args[-1] for args in github_calls if args[:2] == ["variable", "set"]}
    assert (
        variables["GCP_INFRA_SERVICE_ACCOUNT"]
        == "groundwork-infra@test-project.iam.gserviceaccount.com"
    )
    assert variables["GCP_TERRAFORM_STATE_BUCKET"] == "private-state"
    assert (
        "workloadIdentityPools/groundwork-infrastructure/providers/github"
        in variables["GCP_INFRA_WORKLOAD_IDENTITY_PROVIDER"]
    )
    if secret_names is None:
        assert "GCP_PROVIDER_SECRET_NAMES" not in variables
    else:
        assert json.loads(variables["GCP_PROVIDER_SECRET_NAMES"]) == secret_names


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
