# Groundwork IAM reconciliation

`platform.yml` runs offline Terraform validation on PRs, pushes and manual runs.
A separate `infrastructure` job refreshes and applies **every production release**
on main, concurrently with platform tests and candidate image building. It uses
`groundwork-infra`, never the application CI/build/runtime identities. Application
publication/deployment requires successful existing validation gates and this
apply. Missing infrastructure configuration fails production closed. No path or
changed-file filter can skip reconciliation for an eligible production release.

Manual `publish_only: true` and verification branches retain candidate build,
validation and promotion, but skip infrastructure authentication/apply and hosted
rollout. A skipped infrastructure job is accepted only for those publish-only
runs. PRs run no cloud authentication. Main runs retain the workflow's existing
non-cancelling concurrency policy; cancellation prevents downstream deployment.
GCS locking also serializes administrator/other-runner access to the same state.
On cancellation, inspect state before retrying; never disable locking or
force-unlock a live writer. A cancelled run may have partly reconciled IAM, but
cannot publish the application; rerunning refresh/apply converges the state.

## Ownership and IAM inventory

Terraform 1.13.5 and `hashicorp/google` 7.10.0 are exact pins, with a committed
provider lockfile including Linux amd64 and Mac arm64 checksums. This root owns
only the following resources, migrated from `bootstrap.py` (including its former
`enable_vertex_adc` helper). Use stable map keys when extending declarations.

| Owner / resource | Declared scope |
| --- | --- |
| Terraform: custom roles | `groundworkVertexPredictor`: exactly `aiplatform.endpoints.predict`, `serviceusage.services.use`; `groundworkCataloguePublisher`: exactly `datastore.entities.create`, `datastore.entities.get`. Both have `prevent_destroy`. |
| Terraform: project memberships | API and worker: Datastore User, Logging Writer; API: Tasks Enqueuer, Run Jobs Executor With Overrides; worker: Vertex custom role; build: Run Developer, Logging Writer, catalogue custom role, Service Usage Consumer; CI: Cloud Build Editor, Service Usage Consumer. |
| Terraform: service-account memberships | API acts as dispatch; build acts as API/worker; CI acts as build. No project-wide `actAs`. |
| Terraform: Artifact Registry memberships | Build Writer and CI Reader on `groundwork` and `groundwork-runtime`, in the selected region. |
| Terraform: artifact bucket memberships | API/worker Object Admin; build Object Creator/Viewer only under `platform/runtime-bindings/`, preserving the `groundwork-runtime-catalog` condition. |
| Terraform: source bucket memberships | CI Object Admin and Legacy Bucket Reader; build Object Viewer and Object Creator only under `ci-image-builds/`, preserving the `groundwork-ci-images` condition. |
| Terraform: Secret Manager memberships | API Secret Accessor on `groundwork-session`; worker Secret Accessor and build metadata Viewer on explicitly configured existing provider secrets. No secret versions or values. |
| Terraform: API enablement | `aiplatform.googleapis.com`, retained on destroy; partner product enablement/access is external. |
| Administrator: `bootstrap.py` | Creates the five application service accounts, two registries (science tags immutable), two private buckets, session secret/value and queue, and enables other base APIs. It no longer writes application IAM. |
| Administrator: `setup_infrastructure.py` | Creates `groundwork-infra`, `groundworkInfrastructureManager`, that account's management membership, and the separate private versioned state bucket and bucket-scoped Object Admin membership. Terraform never owns its own credentials/backend. |
| Administrator: `configure_github.py` | Owns both federation pools/providers, account-scoped Workload Identity User memberships and nonsecret GitHub variables. Infrastructure trusts only main push/manual events, numeric repository/owner IDs and this workflow. Application verification-branch trust is confined to a separate pool. |
| Administrator: initial `release.py --initialize-public` | Owns the public Run Invoker membership on `groundwork-control`; routine release/verification do not set IAM. |
| Google / project administrators | Service-agent grants, partner model entitlement, billing/quota, unrelated memberships and legacy excess grants. |

All IAM memberships use non-authoritative `*_iam_member` resources, preserving
other members and roles. Do not introduce authoritative policy/binding resources
for the same roles. The two custom role definitions are wholly owned, so drift in
their permission lists is corrected. Missing owned memberships are restored;
unexpected broader grants or changed-condition variants are not automatically
revoked. Audit and remove those separately with administrator review. In
particular, remove obsolete CI Artifact Registry Writer grants from older setup
versions after confirming Reader access; additive Reader does not revoke Writer.

## Initial setup and adoption (administrator, once per project)

These commands are rollout prerequisites, **not commands run while preparing a
PR**. Use an administrator with IAM role/policy, service-account, federation,
resource-creation and bucket-management permissions; no basic Owner role is
required by CI. The infrastructure manager's exact permissions are declared in
`deploy/setup_infrastructure.py`: project/resource IAM read/write, custom-role
management, project metadata and API enablement/consumption. State object access
is granted on its one bucket. It has no direct model invocation, secret-value,
image-writing or application deployment permissions. IAM management is inherently
privileged and can grant access; protect main and review changes to this root and
workflow accordingly. Build, application CI, API and worker permissions are
unchanged apart from the explicitly declared worker Vertex grant.

1. Pause production releases during the ownership handoff. Use this revision of
   the setup scripts so the old imperative writers cannot race Terraform.
   Install the pinned Terraform version. For a fresh project (or missing named
   resources), create the base resources first:

   ```bash
   export GROUNDWORK_PROJECT=your-project-id
   export GROUNDWORK_REGION=us-central1
   python deploy/bootstrap.py --project "$GROUNDWORK_PROJECT" --region "$GROUNDWORK_REGION"
   python deploy/setup_infrastructure.py --project "$GROUNDWORK_PROJECT" --region "$GROUNDWORK_REGION"
   ```

   The state bucket defaults to `PROJECT-groundwork-tfstate`; `--state-bucket`
   selects another globally unique bucket. It must be dedicated to this state,
   with uniform access, public access prevention and versioning. Existing bucket
   access must also be audited by the administrator. Do not apply a retention
   lock to lock objects: Terraform must be able to delete its GCS lock. Keep state
   versions for recovery; CI has no permission to change backend configuration.

2. Authenticate local ADC as the administrator (not with a JSON service-account
   key). Configure the same nonsecret variables that CI will use and initialize
   the remote backend. List only provider secrets that already exist; provider
   values remain in Secret Manager. For an ADC-only deployment use `[]`.

   ```bash
   gcloud auth application-default login
   export TF_VAR_project_id="$GROUNDWORK_PROJECT"
   export TF_VAR_region="$GROUNDWORK_REGION"
   export TF_VAR_provider_secret_names='[]'
   terraform -chdir=deploy/terraform init -input=false -lockfile=readonly \
     -backend-config="bucket=$GROUNDWORK_PROJECT-groundwork-tfstate" \
     -backend-config="prefix=groundwork/iam"
   ```

3. Inspect the existing named custom roles with `gcloud iam roles describe`.
   Import **each role that exists** before applying; skip its import for a fresh
   project where it does not exist. Treat permission/read failures as failures,
   not absence. In the existing `cicl-487801` deployment both roles exist:

   ```bash
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_project_iam_custom_role.roles["catalogue"]' \
     "projects/$GROUNDWORK_PROJECT/roles/groundworkCataloguePublisher"
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_project_iam_custom_role.roles["vertex"]' \
     "projects/$GROUNDWORK_PROJECT/roles/groundworkVertexPredictor"
   ```

   Import only if the address is absent from `terraform state list`. Never import
   an object already managed by a different state: migrate ownership explicitly.
   Soft-deleted custom roles require administrator recovery before adoption.
   Do not delete/recreate existing roles or service accounts. The module does not
   own service-account/registry/bucket/secret objects, so they need no import.

   Existing memberships can be adopted by the first additive apply: the provider
   merges precisely that member into the existing role/condition, then records
   it in state. A `+ create` for a membership is not a replacement of its parent
   resource or IAM policy. Missing grants are added on the same apply. If a
   no-write migration is preferred, import individual memberships first; e.g.:

   ```bash
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_project_iam_member.grants["worker-vertex"]' \
     "$GROUNDWORK_PROJECT projects/$GROUNDWORK_PROJECT/roles/groundworkVertexPredictor serviceAccount:groundwork-worker@$GROUNDWORK_PROJECT.iam.gserviceaccount.com"
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_storage_bucket_iam_member.prefixes["build-runtime-read"]' \
     "b/$GROUNDWORK_PROJECT-groundwork-artifacts roles/storage.objectViewer serviceAccount:groundwork-build@$GROUNDWORK_PROJECT.iam.gserviceaccount.com groundwork-runtime-catalog"
   ```

   Import requires the actual membership to exist. Use the provider's import IDs
   for other resource types. Vertex API enablement is also idempotently adopted,
   or can be imported at `google_project_service.vertex` with ID
   `PROJECT/aiplatform.googleapis.com`.

4. Review a private refreshed plan, confirm only owned resources change, and
   apply it before unpausing releases. In particular there must be no account,
   bucket or registry replacement, authoritative IAM policy, or role deletion.
   Inspect the two custom roles for unexpected consumers before accepting their
   exact permission sets. Run the plan and apply with the same variables/backend:

   ```bash
   terraform -chdir=deploy/terraform plan -lock-timeout=5m -out=adoption.tfplan
   terraform -chdir=deploy/terraform apply -lock-timeout=5m adoption.tfplan
   rm deploy/terraform/adoption.tfplan
   ```

5. Configure federation and repository variables using the same target/backend:

   ```bash
   python deploy/configure_github.py --project "$GROUNDWORK_PROJECT" \
     --region "$GROUNDWORK_REGION" --repository OWNER/REPOSITORY \
     --oauth-client-id CLIENT.apps.googleusercontent.com \
     --terraform-state-bucket "$GROUNDWORK_PROJECT-groundwork-tfstate" \
     --provider-secret-names
   ```

   Supply the existing provider secret names after `--provider-secret-names` to
   enable their grants. An empty flag explicitly selects ADC-only; omitting the
   flag preserves the previous GitHub variable. Configure the same list in local
   `TF_VAR_provider_secret_names` during migration. Removing a name later revokes
   its two managed grants, so review such changes as access changes.

   This writes `GCP_INFRA_WORKLOAD_IDENTITY_PROVIDER`, `GCP_INFRA_SERVICE_ACCOUNT`,
   `GCP_TERRAFORM_STATE_BUCKET`, and optionally `GCP_PROVIDER_SECRET_NAMES` alongside
   the existing application variables. Infrastructure trust is never extended by
   `--verification-branch`. Do not reuse the application's pool for infrastructure.
   A different project needs its own backend/state and setup, the correct region,
   OAuth origin/client and these variables; no production project ID is hardcoded
   in Terraform. Keep one state per project and region; do not point a different
   project at an existing project's state or change workspaces in CI.

For fresh deployments, perform the existing first-image and public-service
initialization in `deploy/README.md` after IAM setup. Subsequent main releases
use normal Terraform refresh/apply with native GCS locking and a five-minute
lock timeout. There is no separate saved CI plan or plan artifact. Backend and
provider credentials use short-lived federation through ADC environment settings;
never pass them in `-backend-config`, tfvars, outputs or configuration. Plans,
state, `.terraform`, local tfvars and generated auth files are ignored by Git.
No infrastructure files are uploaded by the infrastructure job. Application
build uploads/artifacts stay in separate runners with their existing allowlists.

## Validation and operational limits

```bash
terraform -chdir=deploy/terraform fmt -check -recursive
terraform -chdir=deploy/terraform init -backend=false -input=false -lockfile=readonly
terraform -chdir=deploy/terraform validate
terraform -chdir=deploy/terraform test
```

Tests use mocked providers and plans only. They assert exact Vertex permissions,
runtime identity selection, catalogue permissions, all migrated grant groups,
conditional prefixes, CI Reader/build Writer separation, provider-secret scope
and another project's configuration. Python tests cover the workflow result
matrix, cancellation, missing setup, trust boundaries and bootstrap ownership.

A successful apply proves declared infrastructure reconciliation. IAM propagation
can still take time. It does **not** prove model entitlement, partner-product
enablement, regional endpoint support, billing or available quota. Scripted smoke
runs and `verify_hosted.py` check deployment and application behavior without
calling paid providers; they cannot establish live model access. Any live model
access check must be separately requested and run as the actual worker identity.
Keep source/image pin validation and scientific evidence publication unchanged.

Official references: [GCS backend and locking](https://developer.hashicorp.com/terraform/language/backend/gcs),
[non-authoritative project IAM members and imports](https://registry.terraform.io/providers/hashicorp/google/7.10.0/docs/resources/google_project_iam),
[custom-role imports](https://registry.terraform.io/providers/hashicorp/google/7.10.0/docs/resources/google_project_iam_custom_role),
[bucket IAM imports](https://registry.terraform.io/providers/hashicorp/google/7.10.0/docs/resources/storage_bucket_iam),
[deployment federation restrictions](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines).
