"""Application releases stay automatic without GitHub acquiring IAM authority."""

import itertools
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/platform.yml").read_text())
JOBS = WORKFLOW["jobs"]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def condition(expression, context):
    expression = expression.replace("always()", "True").replace("cancelled()", "cancelled")
    for name in sorted(context, key=len, reverse=True):
        expression = expression.replace(name, repr(context[name]))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return bool(eval(expression.strip(), {"__builtins__": {}}, {}))


@pytest.mark.parametrize(
    "event", ["push", "workflow_dispatch", "pull_request", "pull_request_target"]
)
def test_automatic_release_requires_successful_application_gates_only(event):
    # Failed, missing or cancelled validation must never become a deployment.
    for validation, stack, cancelled, configured in itertools.product(
        ["success", "failure", "skipped", "cancelled"],
        ["success", "failure", "skipped", "cancelled"],
        [True, False],
        [True, False],
    ):
        context = {
            "github.event_name": event,
            "vars.GCP_WORKLOAD_IDENTITY_PROVIDER": "provider" if configured else "",
            "needs.validate.result": validation,
            "needs.platform-stack.result": stack,
            "cancelled": cancelled,
        }
        expected = (
            event in ("push", "workflow_dispatch")
            and configured
            and not cancelled
            and validation == stack == "success"
        )
        assert condition(JOBS["deploy"]["if"], context) == expected
    assert set(JOBS["deploy"]["needs"]) == {"validate", "platform-stack"}
    assert "environment" not in JOBS["deploy"]  # No extra per-release approval.


def test_pr_ci_has_no_production_identity_and_terraform_is_offline_in_both_workflows():
    assert CI.get("permissions") == {"contents": "read"}
    for job in CI["jobs"].values():
        assert "id-token" not in job.get("permissions", {})
        assert not any(
            "google-github-actions/auth" in step.get("uses", "") for step in job.get("steps", [])
        )
    for job in [CI["jobs"]["terraform"], JOBS["terraform-validate"]]:
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        assert "init -backend=false -input=false -lockfile=readonly" in commands
        assert "terraform -chdir=deploy/terraform validate" in commands
        assert "terraform -chdir=deploy/terraform test" in commands
        assert "apply" not in commands and "import" not in commands
        assert not job.get("permissions")
        assert not any("auth@" in step.get("uses", "") for step in job["steps"])


def test_release_has_no_infrastructure_credentials_state_or_iam_writer():
    assert "infrastructure" not in JOBS
    source = (ROOT / ".github/workflows/platform.yml").read_text()
    for forbidden in [
        "GCP_INFRA_",
        "GCP_TERRAFORM_STATE_BUCKET",
        "TF_VAR_",
        "terraform apply",
        "terraform -chdir=deploy/terraform apply",
        "setup_infrastructure.py",
        "bootstrap.py",
        "add-iam-policy-binding",
    ]:
        assert forbidden not in source
    auth_steps = [
        step
        for job in JOBS.values()
        for step in job.get("steps", [])
        if step.get("uses") == "google-github-actions/auth@v3"
    ]
    assert auth_steps
    for step in auth_steps:
        assert step["with"] == {
            "workload_identity_provider": "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}",
            "service_account": "${{ vars.GCP_CI_SERVICE_ACCOUNT }}",
        }
        assert step.get("if", "") != "always()"
    assert (
        WORKFLOW["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )
    publish = next(
        s
        for s in JOBS["deploy"]["steps"]
        if s.get("name") == "Publish the validated artifacts through Cloud Build"
    )
    assert "inputs.publish_only" in publish["env"]["PUBLISH_ONLY"]
    assert "github.ref != 'refs/heads/main'" in publish["env"]["PUBLISH_ONLY"]


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_offline_terraform_validation_remains_a_required_gate(result):
    for job, env in [
        (JOBS["validate"], {"TEST_RESULT": "success", "STACK_RESULT": "success"}),
        (CI["jobs"]["test"], {"PYTHON_RESULT": "success", "PACKAGE_RESULT": "success"}),
    ]:
        gate = job["steps"][0]
        outcome = subprocess.run(
            ["bash", "-e", "-c", gate["run"]],
            env={**os.environ, **env, "TERRAFORM_RESULT": result},
            capture_output=True,
        )
        assert outcome.returncode != 0


def test_deployment_machinery_has_a_concrete_owner_without_claiming_application_files():
    # CODEOWNERS uses the last matching pattern. Representative root/directory
    # matches protect transitive release helpers as well as the ownership policy.
    import fnmatch

    rules = []
    for line in (ROOT / ".github/CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern.lstrip("/"), owners))

    def owners(path):
        result = []
        for pattern, people in rules:
            if (pattern.endswith("/") and path.startswith(pattern)) or fnmatch.fnmatchcase(
                path, pattern
            ):
                result = people
        return result

    for path in [
        ".github/CODEOWNERS",
        ".github/workflows/platform.yml",
        ".github/workflows/pr-rebase.yml",
        ".github/actions/build-image/action.yml",
        ".github/ci/verify_image_promotion.py",
        "deploy/terraform/main.tf",
        "deploy/terraform/.terraform.lock.hcl",
        "deploy/latency.py",
        "cloudbuild.yaml",
        "cloud_platform/publish_catalogue.py",
        "scripts/install_workspace.py",
        "scripts/workspace.py",
        "Dockerfile.platform",
        ".gcloudignore",
        "docs/DEPLOYMENT_TRUST.md",
    ]:
        assert owners(path) == ["@devYaoYH"], path
    for path in [
        "games/new-game/pyproject.toml",
        "games/new-game/SPEC.md",
        "web/components/EnvironmentDetail.tsx",
        "experiments/new-config.yaml",
    ]:
        assert owners(path) == [], path
