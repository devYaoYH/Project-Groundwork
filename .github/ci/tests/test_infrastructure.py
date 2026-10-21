"""Evaluate the release DAG for cloud isolation, skips, failure and cancellation."""

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


def condition(expression, context):
    """Evaluate this workflow's boolean-only GHA conditions using supplied results.

    Deliberately reject unknown syntax/contexts rather than silently approximating
    the expression. Production deploy uses an explicit status function, so GHA
    does not inject its default success() and skip publish-only runs.
    """
    expression = expression.replace("always()", "True").replace("cancelled()", "cancelled")
    for name in sorted(context, key=len, reverse=True):
        expression = expression.replace(name, repr(context[name]))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return bool(eval(expression.strip(), {"__builtins__": {}}, {}))


@pytest.mark.parametrize(
    "event,ref,publish,configured",
    list(
        itertools.product(
            ["push", "workflow_dispatch", "pull_request", "pull_request_target"],
            ["refs/heads/main", "refs/heads/codex/verification", "refs/pull/42/merge"],
            [True, False],
            [True, False],
        )
    ),
)
def test_reconciliation_runs_on_every_eligible_production_event(event, ref, publish, configured):
    """Revoked grants need repair even when no Terraform files changed.

    Keep that repair independent of slow image builds, while ensuring PR and
    publish-only runs cannot reach the IAM mutation job.
    """
    context = {
        "github.event_name": event,
        "github.ref": ref,
        "inputs.publish_only": publish,
        "vars.GCP_WORKLOAD_IDENTITY_PROVIDER": "provider" if configured else "",
    }
    expected = (
        configured
        and ref == "refs/heads/main"
        and (event == "push" or (event == "workflow_dispatch" and not publish))
    )
    assert condition(JOBS["infrastructure"]["if"], context) == expected
    # No changed-file optimization or dependency on image-building duration.
    assert JOBS["infrastructure"]["needs"] == "terraform-validate"
    assert "paths" not in JOBS["infrastructure"]


@pytest.mark.parametrize(
    "event,ref,publish",
    [
        ("push", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/main", True),
        ("push", "refs/heads/codex/verification", False),
        ("workflow_dispatch", "refs/heads/codex/verification", False),
        ("pull_request", "refs/pull/42/merge", False),
        ("pull_request_target", "refs/heads/main", False),
    ],
)
def test_publication_requires_successful_gates_and_the_correct_infrastructure_result(
    event, ref, publish
):
    """Handle intentional skips without letting failed production gates pass.

    GitHub's skipped-dependency handling can block publish-only runs; bypassing
    it with always() must still reject failures and cancellations explicitly.
    """
    production = ref == "refs/heads/main" and not (event == "workflow_dispatch" and publish)
    for validation, stack, infrastructure, cancelled, configured in itertools.product(
        ["success", "failure", "skipped", "cancelled"],
        ["success", "failure", "skipped", "cancelled"],
        ["success", "failure", "skipped", "cancelled"],
        [True, False],
        [True, False],
    ):
        context = {
            "github.event_name": event,
            "github.ref": ref,
            "inputs.publish_only": publish,
            "vars.GCP_WORKLOAD_IDENTITY_PROVIDER": "provider" if configured else "",
            "needs.validate.result": validation,
            "needs.platform-stack.result": stack,
            "needs.infrastructure.result": infrastructure,
            "cancelled": cancelled,
        }
        expected = (
            event in ("push", "workflow_dispatch")
            and configured
            and not cancelled
            and validation == stack == "success"
            and infrastructure == ("success" if production else "skipped")
        )
        assert condition(JOBS["deploy"]["if"], context) == expected
    assert "always()" in JOBS["deploy"]["if"]
    assert "!cancelled()" in JOBS["deploy"]["if"]


def test_infrastructure_credentials_state_and_plans_do_not_enter_image_artifacts():
    """Keep IAM authority and Terraform data outside the image publication path.

    Separate credentials limit privilege sharing, while locking and refresh
    prevent concurrent state writes and missed IAM drift.
    """
    job = JOBS["infrastructure"]
    auth = next(
        step for step in job["steps"] if step.get("uses") == "google-github-actions/auth@v3"
    )
    assert auth["with"] == {
        "workload_identity_provider": "${{ vars.GCP_INFRA_WORKLOAD_IDENTITY_PROVIDER }}",
        "service_account": "${{ vars.GCP_INFRA_SERVICE_ACCOUNT }}",
    }
    assert not any("upload-artifact" in step.get("uses", "") for step in job["steps"])
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert '-backend-config="prefix=groundwork/iam"' in commands
    assert "-lockfile=readonly" in commands and "-lock-timeout=5m" in commands
    assert "-lock=false" not in commands and "-refresh=false" not in commands
    assert "terraform -chdir=deploy/terraform apply" in commands
    assert not any(step.get("continue-on-error") for step in job["steps"])
    assert (
        WORKFLOW["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' }}"
    )
    assert JOBS["terraform-validate"].get("permissions", {}) == {}
    assert not any("auth@" in step.get("uses", "") for step in JOBS["terraform-validate"]["steps"])


@pytest.mark.parametrize(
    "missing",
    ["INFRA_PROVIDER", "INFRA_ACCOUNT", "STATE_BUCKET", "TF_VAR_project_id", "TF_VAR_region"],
)
def test_production_configuration_fails_closed_before_authentication(missing):
    """Missing setup must stop releases before credential or backend defaults apply.

    Otherwise a partial installation could use an unintended identity, project
    or state location instead of the administrator's explicit configuration.
    """
    guard = JOBS["infrastructure"]["steps"][1]
    env = {
        **os.environ,
        **dict.fromkeys(
            [
                "INFRA_PROVIDER",
                "INFRA_ACCOUNT",
                "STATE_BUCKET",
                "TF_VAR_project_id",
                "TF_VAR_region",
            ],
            "configured",
        ),
    }
    env[missing] = ""
    result = subprocess.run(["bash", "-e", "-c", guard["run"]], env=env, capture_output=True)
    assert result.returncode != 0


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_terraform_validation_is_a_required_release_gate(result):
    """Passing application tests cannot compensate for unvalidated infrastructure.

    The aggregate gate must reject skipped or cancelled Terraform checks as well
    as failures, rather than reporting a misleading successful release check.
    """
    gate = JOBS["validate"]["steps"][0]
    env = {
        **os.environ,
        "TEST_RESULT": "success",
        "STACK_RESULT": "success",
        "TERRAFORM_RESULT": result,
    }
    assert subprocess.run(["bash", "-e", "-c", gate["run"]], env=env).returncode != 0
