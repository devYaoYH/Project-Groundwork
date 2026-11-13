# Maintainer-run Groundwork IAM

Terraform is the source of truth for declared application IAM. **Infrastructure
maintainers review and apply it manually; GitHub never gets IAM-administration
credentials or Terraform state access.** Ordinary application releases remain
automatic after their validation gates. `ci.yml` and the platform validation job
run only offline/mocked Terraform checks. There is no production apply job or
requirement for a fresh apply on every application release.

See [deployment trust and review rules](../../docs/DEPLOYMENT_TRUST.md) for the
three human groups, protected paths, repository-rule rollout and effective Cloud
Build delegation. Configuration/item-bank self-service remains application work.

## Ownership and limits

Terraform 1.13.5 and `hashicorp/google` 7.10.0 are exact pins. The committed provider
lockfile includes Linux amd64 and Mac arm64 checksums.

| Owner | Resources and scope |
| --- | --- |
| Maintainer-run Terraform | `groundworkVertexPredictor`: exactly `aiplatform.endpoints.predict`, `serviceusage.services.use`; `groundworkCataloguePublisher`: exactly `datastore.entities.create`, `datastore.entities.get`. Both roles have `prevent_destroy`. |
| Maintainer-run Terraform | API/worker datastore and logging grants; API dispatch/Job execution grants; worker Vertex role; application build/CI project grants; account-scoped `actAs`; build Writer and CI Reader on the two named registries; application bucket/prefix and explicitly declared per-secret memberships; Vertex API enablement, retained on destroy. |
| Maintainer: `bootstrap.py` | Five application service accounts, two registries (science tags immutable), private artifact/source buckets, session secret/value, queue and base APIs. No application IAM writer. |
| Maintainer: `setup_infrastructure.py` | Private, versioned remote-state bucket only. Creates no infrastructure service account, management role, federation or application IAM. The maintainer audits its access. |
| Maintainer: `configure_github.py` | Application pool/provider and account-scoped GitHub impersonation of `groundwork-ci`; the five nonsecret application repository variables. No infrastructure pool/account/backend variables. |
| Maintainer: first `release.py --initialize-public` | Initial public Run Invoker membership. Routine releases preserve this policy. |
| Google / project administrators | Service agents, model entitlement, partner APIs, quota/billing, unrelated grants and review/removal of excess legacy access. |

All memberships use non-authoritative `*_iam_member` resources, preserving peers.
The two custom-role definitions are wholly owned and their permission lists are
reconciled. A plan/apply restores missing declared grants; unexpected broader
roles or changed-condition variants are not revoked automatically. Audit/remove
those separately. Adding CI Reader does not remove an older CI Writer grant.

The API/worker artifact grants and build publication privileges are application
access. In a shared project, review their actual scope, including Firestore,
rather than treating these declarations as proof of isolation from other apps.
A deployer can introduce runtime code and therefore has effective runtime access.

## Initial setup and adoption (administrator, once per project)

Pause production release/cutover during adoption. Use these updated scripts so
imperative IAM writers cannot race Terraform. Use an infrastructure maintainer
with resource-creation, IAM role/policy, federation and backend permissions; do
not grant that authority to GitHub, application CI/build or runtime accounts.

1. Set the target explicitly and create missing base resources and the backend:

   ```bash
   export GROUNDWORK_PROJECT=your-project-id
   export GROUNDWORK_REGION=us-central1
   python deploy/bootstrap.py --project "$GROUNDWORK_PROJECT" --region "$GROUNDWORK_REGION"
   python deploy/setup_infrastructure.py --project "$GROUNDWORK_PROJECT" --region "$GROUNDWORK_REGION"
   ```

   The backend defaults to `PROJECT-groundwork-tfstate`; `--state-bucket` selects
   another globally unique bucket. Audit existing bucket IAM and inherited
   grants. Only authorized human maintainers may access state. Preserve public
   access prevention, uniform access and versioning. Do not put a retention lock
   on Terraform lock objects: GCS locking must be able to remove its lock.

2. If the previous CI-infrastructure proposal was applied, complete
   [legacy trust revocation](#revoke-legacy-github-infrastructure-access) below.
   `configure_github.py` refuses a remaining legacy infrastructure pool or
   workload-federation binding on `groundwork-infra`. Read failures fail setup;
   they are not treated as missing resources. This guard is not a full effective
   IAM audit: inspect project-level/custom-role and alternate-account paths too.

3. Install Terraform 1.13.5 and authenticate human ADC, without a JSON key. Use
   the same variables for plan and apply. Declare only existing provider-secret
   names; values stay in Secret Manager. Use `[]` for scripted/ADC-only operation.

   ```bash
   gcloud auth application-default login
   export TF_VAR_project_id="$GROUNDWORK_PROJECT"
   export TF_VAR_region="$GROUNDWORK_REGION"
   export TF_VAR_provider_secret_names='[]'
   terraform -chdir=deploy/terraform init -input=false -lockfile=readonly \
     -backend-config="bucket=$GROUNDWORK_PROJECT-groundwork-tfstate" \
     -backend-config="prefix=groundwork/iam"
   ```

   Use one canonical IAM state for the deployment project, with its selected
   region. Never reuse another project's state. Credentials must not appear in
   backend configuration, tfvars, outputs, command logs or exported plans.

4. Inspect the two named roles with `gcloud iam roles describe`. Import each
   existing role only if its address is absent from `terraform state list`.
   A genuinely fresh project skips missing-role imports. A read/permission failure
   is not absence. Existing roles in another state require explicit ownership
   migration; soft-deleted roles require administrator recovery.

   ```bash
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_project_iam_custom_role.roles["catalogue"]' \
     "projects/$GROUNDWORK_PROJECT/roles/groundworkCataloguePublisher"
   terraform -chdir=deploy/terraform import -lock-timeout=5m \
     'google_project_iam_custom_role.roles["vertex"]' \
     "projects/$GROUNDWORK_PROJECT/roles/groundworkVertexPredictor"
   ```

   Existing memberships can be adopted on the first additive apply, or imported
   individually for a no-write migration. A membership `+ create` merges that
   member/role/condition; it does not replace its parent account/bucket/policy.
   Existing accounts, buckets, registries and secret values are outside this
   state's ownership and require no object imports. Existing Vertex API enablement
   is adopted, or imported at `google_project_service.vertex` with ID
   `PROJECT/aiplatform.googleapis.com`.

5. From the reviewed checkout, make and inspect a private refreshed plan. Confirm
   that only intended owned roles/memberships/API enablement change: no parent
   account, bucket or registry replacement, authoritative policy, or role deletion.
   Review existing consumers before accepting the exact custom-role permissions.

   ```bash
   terraform -chdir=deploy/terraform plan -lock-timeout=5m -out=adoption.tfplan
   terraform -chdir=deploy/terraform show adoption.tfplan
   terraform -chdir=deploy/terraform apply -lock-timeout=5m adoption.tfplan
   rm deploy/terraform/adoption.tfplan
   ```

   Record the approved source SHA (`git rev-parse HEAD`), target project/region,
   backend/prefix, plan checksum, operator, time and successful apply result in
   the private infrastructure change record. Keep previous state versions for
   recovery. Do not publish state/plans as GitHub or application artifacts.

6. Enforce the repository review rules in
   [DEPLOYMENT_TRUST.md](../../docs/DEPLOYMENT_TRUST.md), configure Google login and
   perform the documented first-image/catalogue/public-service initialization.
   Then configure application federation/variables for the selected project:

   ```bash
   python deploy/configure_github.py --project "$GROUNDWORK_PROJECT" \
     --region "$GROUNDWORK_REGION" --repository OWNER/REPOSITORY \
     --oauth-client-id CLIENT.apps.googleusercontent.com
   ```

   This switches the repository's application target, without creating or
   triggering infrastructure CI. Review a first automatic scripted release and
   its final evidence before retiring an old deployment. A fresh deployment does
   not require migration of old experiments.

## Revoke legacy GitHub infrastructure access

These are administrator changes to inspect/review before running, not application
CI steps. If the older proposal was never applied, no legacy objects need removal.
If it was applied, first pause releases and disable the old `infrastructure` job.
Inspect all effective project/service-account/bucket grants before revoking them.

1. Remove the infrastructure account's management grant first so old/unexpired
   federated credentials cannot keep administering project IAM:

   ```bash
   gcloud projects remove-iam-policy-binding "$GROUNDWORK_PROJECT" \
     --member="serviceAccount:groundwork-infra@$GROUNDWORK_PROJECT.iam.gserviceaccount.com" \
     --role="projects/$GROUNDWORK_PROJECT/roles/groundworkInfrastructureManager" \
     --condition=None
   ```

   Inspect and remove any additional privileged grants; removing this one role
   alone is insufficient if the account has other effective authority.

2. Inspect `gcloud iam service-accounts get-iam-policy` for that account and remove
   **every workload-pool impersonation binding**, including alternate pools,
   conditions, Token Creator/User roles and broad project-level grants. Keep only
   explicitly approved human access if retaining the account. Inspect inherited
   access and service-account `actAs`/Cloud Build delegation as well.

3. Delete the retired pool after reviewing its consumers:

   ```bash
   gcloud iam workload-identity-pools delete groundwork-infrastructure \
     --project "$GROUNDWORK_PROJECT" --location=global
   ```

   Remove the retired account's Object Admin grant on the state bucket (and any
   other state access), preserving legitimate human access and existing state:

   ```bash
   gcloud storage buckets remove-iam-policy-binding \
     "gs://$GROUNDWORK_PROJECT-groundwork-tfstate" \
     --member="serviceAccount:groundwork-infra@$GROUNDWORK_PROJECT.iam.gserviceaccount.com" \
     --role=roles/storage.objectAdmin
   ```

4. Remove obsolete repository variables `GCP_INFRA_WORKLOAD_IDENTITY_PROVIDER`,
   `GCP_INFRA_SERVICE_ACCOUNT`, `GCP_TERRAFORM_STATE_BUCKET` and the Terraform-only
   `GCP_PROVIDER_SECRET_NAMES`. Inspect unused copies in environments/org settings.
   Deleting a variable is not revocation of its underlying cloud grants.
   Re-check IAM after propagation and account for already-issued credentials;
   no application identity may impersonate the retired privileged account or
   access the backend. Do not delete a custom role/account still used elsewhere.

## Routine IAM changes, failures and recovery

A maintainer reviews the Terraform PR/plan, applies the approved revision using
human ADC, and records the result. Routine application releases do not wait for a
fresh apply. If a build/release reports PERMISSION_DENIED, inspect its actual
identity and logs, compare declared and effective access, and have a maintainer
plan/apply the approved missing grant. Do not grant basic Owner, upload a key,
change provider identities, disable validation or add IAM repair to CI.

GCS locks serialize maintainer access. After interruption, inspect state and the
operation before retrying; never force-unlock a live writer. State versions allow
recovery. Re-run refresh/plan after partial application. The workflow does not
change IAM while a release is pending. Schedule a maintainer audit when drift
must be checked; additive membership ownership will not remove excess grants.

Offline checks require no ADC and make no paid model calls:

```bash
terraform -chdir=deploy/terraform fmt -check -recursive
terraform -chdir=deploy/terraform init -backend=false -input=false -lockfile=readonly
terraform -chdir=deploy/terraform validate
terraform -chdir=deploy/terraform test
```

Tests validate declarations, workflow gating, federation setup/legacy guards,
state/setup ownership and protected-file selection. They do not prove live
organization policy, backend access, inherited grants, model entitlement or quota.
Perform reviewed live IAM/negative authentication checks and a scripted hosted
experiment/export before production cutover. Live provider calls are a separate,
explicitly requested check under the actual worker identity.

References: [GCS locking](https://developer.hashicorp.com/terraform/language/backend/gcs),
[additive member/import semantics](https://registry.terraform.io/providers/hashicorp/google/7.10.0/docs/resources/google_project_iam),
[custom-role imports](https://registry.terraform.io/providers/hashicorp/google/7.10.0/docs/resources/google_project_iam_custom_role),
[service-account security](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts).
