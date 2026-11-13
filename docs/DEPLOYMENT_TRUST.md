# Groundwork deployment trust

Groundwork has three human groups and two deployment-related GitHub workflows.
Existing maintainer PR-maintenance automation is separate from cloud deployment;
it must obey the same branch protections and cannot carry cloud administrator
credentials. No workflow receives Terraform state or project IAM administration.

| People | Access | Review and execution |
| --- | --- | --- |
| Owner / infrastructure maintainers | Cloud resources, IAM, Terraform, federation, provider-secret setup and repository protections. | Review deployment/IAM changes, then manually review/apply the approved Terraform revision. |
| Contributors | Environment code, web-console changes and proposed configuration/item-bank changes through PRs. No cloud administration needed. | Normal maintainer review and required CI. Infrastructure-aware review is required only for protected deployment files. |
| Experiment users | Authenticated application operations on their own experiments, configurations, item banks and artifacts. No GitHub or GCP identity needed. | API ownership and validation govern private edits; shared publication requires an explicit product permission or maintainer review. Missing self-service flows are separate application work. |

## CI and automatic application releases

`ci.yml` runs credential-free PR checks, including offline/mock Terraform checks.
`platform.yml` builds and validates exact candidate images, promotes those images
and deploys on eligible main pushes. A manual run defaults to publish-only; an
explicit main rollout uses the same application identity and validation gates.
Failed, skipped or cancelled required checks cannot produce a hosted release.
Neither workflow applies Terraform or obtains infrastructure credentials. A
normal application merge does not wait for an infrastructure approval or apply.

The release uses `groundwork-ci` to submit Cloud Build jobs as `groundwork-build`.
That delegation is effective execution authority as the build account; registry
Reader access on GitHub does not reduce it. The declared build/runtime grants
are application-scoped and contain no IAM-administration or state-bucket grant.
A deployment can introduce code with runtime permissions, so it has effective
access to the application's data/secrets. Accepted environment code is trusted
application code, not an isolation boundary against the worker identity. Users
supply validated data/configuration, not executable code or cloud identities.
Worker/runtime containers currently share a credential boundary. Keep API owner
checks and explicit private observations even within these trusted processes.

Terraform restores declared memberships and custom roles when a maintainer
applies it. Missing permissions fail the release; the error directs the operator
to a reviewed plan/apply. CI never repairs IAM or retries as an administrator.
Unexpected broader grants require a separate audit/removal. A shared lab project
also needs a review of actual database/resource scope: application ownership
filters do not isolate another lab workload from a compromised cloud principal.

## Enforce review at change time

`.github/CODEOWNERS` names `@devYaoYH`, the current repository owner, for workflow,
local action, cloud-build, Terraform and deployment code, their privileged helper
scripts, and the ownership policy itself. Environment/game source, web components
and ordinary experiment configuration retain normal application review. Add any
new privileged helper to the protected inventory, wherever it lives.

**CODEOWNERS alone does not enforce approval.** A repository administrator must
configure main's existing ruleset/branch protection before enabling the new
release route. Preserve current required checks while adding these requirements:

1. Require a PR, required status checks and code-owner approval. Retain `lint`,
   `web`, `test`, `runtime-containers` and the project's existing required checks.
   The `test` aggregate now includes offline Terraform validation. Avoid making
   path-filtered platform jobs mandatory for unrelated documentation-only PRs.
2. Dismiss stale approvals when changes are pushed, require approval of the most
   recent reviewable push, and restrict who can dismiss reviews/bypass rules.
   A protected-file author needs another infrastructure-aware reviewer; name an
   additional eligible maintainer before relying on a one-person ownership list.
3. Block direct/force pushes that bypass review. Include maintainers, deployment
   bots and PR-maintenance automation in the policy; do not give their tokens an
   implicit bypass. Restrict any exceptional break-glass bypass and record it.
4. Restrict repository settings, deployment variables, federation and cloud IAM
   changes to infrastructure maintainers. Settings outside Git are not governed
   by CODEOWNERS. No per-run administrator review is needed for ordinary release.

Verify the rules using repository Settings and read-only API inspection:

```bash
gh api repos/devYaoYH/Project-Groundwork/rules/branches/main
gh api repos/devYaoYH/Project-Groundwork/branches/main/protection
```

A 404 from the second command may mean rulesets are used instead; inspect both.
A permission failure is not proof of no protection. Check GitHub plan/visibility
support and test a protected-file PR as a non-owner: merge must remain blocked
until the eligible owner's fresh approval. An application-only PR should retain
its normal review/check requirements with no extra infrastructure review.
Repository protection changes are administrator rollout steps, not performed by
this PR or by application CI.

## Maintainer operations

Follow [Terraform setup/adoption](../deploy/terraform/README.md) for the backend,
reviewed plan/apply, applied-revision record, revocation of legacy infrastructure
federation and recovery. Do this before GitHub's target is switched to a new
project. GitHub receives only application federation; it never needs a JSON key,
`GCP_INFRA_*` variables, an infrastructure impersonation grant or state access.

Use short-lived human ADC for Terraform. An optional administrator-only service
account must have human-only access; a broad project IAM/custom-role grant is
inherently powerful even without direct secret/model permissions. Inspect all
alternate impersonation and Cloud Build paths during adoption. No application
account may submit work as, act as, mint tokens for, or grant access to such an
identity. Validate the live effective policy as a maintainer; offline declaration
and mocked tests cannot prove the absence of unrelated project-level grants.

Retain digest/source provenance, successful scripted hosted execution and final
export publication when validating a release. The setup and verification tooling
must not perform paid model checks implicitly.
