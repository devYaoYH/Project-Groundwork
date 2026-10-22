locals {
  accounts = { for name in ["api", "worker", "dispatch", "build", "ci"] :
    name => "serviceAccount:groundwork-${name}@${var.project_id}.iam.gserviceaccount.com"
  }
  custom_roles = {
    vertex = {
      role_id     = "groundworkVertexPredictor"
      title       = "Groundwork Vertex predictor"
      permissions = ["aiplatform.endpoints.predict", "serviceusage.services.use"]
    }
    catalogue = {
      role_id     = "groundworkCataloguePublisher"
      title       = "Groundwork catalogue publisher"
      permissions = ["datastore.entities.create", "datastore.entities.get"]
    }
  }
  project_grants = {
    api-datastore    = { account = "api", role = "roles/datastore.user" }
    api-logging      = { account = "api", role = "roles/logging.logWriter" }
    api-tasks        = { account = "api", role = "roles/cloudtasks.enqueuer" }
    api-jobs         = { account = "api", role = "roles/run.jobsExecutorWithOverrides" }
    worker-datastore = { account = "worker", role = "roles/datastore.user" }
    worker-logging   = { account = "worker", role = "roles/logging.logWriter" }
    worker-vertex    = { account = "worker", role = google_project_iam_custom_role.roles["vertex"].name }
    build-run        = { account = "build", role = "roles/run.developer" }
    build-logging    = { account = "build", role = "roles/logging.logWriter" }
    build-catalogue  = { account = "build", role = google_project_iam_custom_role.roles["catalogue"].name }
    build-services   = { account = "build", role = "roles/serviceusage.serviceUsageConsumer" }
    ci-builds        = { account = "ci", role = "roles/cloudbuild.builds.editor" }
    ci-services      = { account = "ci", role = "roles/serviceusage.serviceUsageConsumer" }
  }
  impersonation = {
    api-dispatch = { actor = "api", target = "dispatch" }
    build-api    = { actor = "build", target = "api" }
    build-worker = { actor = "build", target = "worker" }
    ci-build     = { actor = "ci", target = "build" }
  }
  registry_grants = { for pair in setproduct(["groundwork", "groundwork-runtime"], ["build", "ci"]) :
    "${pair[0]}-${pair[1]}" => {
      repository = pair[0], account = pair[1],
      role       = pair[1] == "build" ? "roles/artifactregistry.writer" : "roles/artifactregistry.reader"
    }
  }
  artifact_bucket = "${var.project_id}-groundwork-artifacts"
  source_bucket   = "${var.project_id}-groundwork-build-source"
  bucket_grants = {
    api-artifacts    = { bucket = local.artifact_bucket, account = "api", role = "roles/storage.objectAdmin" }
    worker-artifacts = { bucket = local.artifact_bucket, account = "worker", role = "roles/storage.objectAdmin" }
    ci-source        = { bucket = local.source_bucket, account = "ci", role = "roles/storage.objectAdmin" }
    ci-source-read   = { bucket = local.source_bucket, account = "ci", role = "roles/storage.legacyBucketReader" }
    build-source     = { bucket = local.source_bucket, account = "build", role = "roles/storage.objectViewer" }
  }
  conditional_bucket_grants = {
    build-runtime-create = {
      bucket = local.artifact_bucket, role = "roles/storage.objectCreator", title = "groundwork-runtime-catalog"
      prefix = "platform/runtime-bindings/"
    }
    build-runtime-read = {
      bucket = local.artifact_bucket, role = "roles/storage.objectViewer", title = "groundwork-runtime-catalog"
      prefix = "platform/runtime-bindings/"
    }
    build-candidates = {
      bucket = local.source_bucket, role = "roles/storage.objectCreator", title = "groundwork-ci-images"
      prefix = "ci-image-builds/"
    }
  }
  provider_secret_grants = { for pair in setproduct(var.provider_secret_names, ["worker", "build"]) :
    "${pair[0]}-${pair[1]}" => {
      secret = pair[0], account = pair[1],
      role   = pair[1] == "worker" ? "roles/secretmanager.secretAccessor" : "roles/secretmanager.viewer"
    }
  }
}

# Enable only the managed Vertex API. Partner entitlement and quota are separate.
resource "google_project_service" "vertex" {
  project            = var.project_id
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_custom_role" "roles" {
  depends_on  = [google_project_service.vertex]
  for_each    = local.custom_roles
  project     = var.project_id
  role_id     = each.value.role_id
  title       = each.value.title
  permissions = each.value.permissions
  stage       = "GA"
  lifecycle {
    prevent_destroy = true
  }
}

# Member resources own only the named member/role/condition, preserving peers.
resource "google_project_iam_member" "grants" {
  for_each = local.project_grants
  project  = var.project_id
  member   = local.accounts[each.value.account]
  role     = each.value.role
}

resource "google_service_account_iam_member" "act_as" {
  for_each           = local.impersonation
  service_account_id = "projects/${var.project_id}/serviceAccounts/groundwork-${each.value.target}@${var.project_id}.iam.gserviceaccount.com"
  member             = local.accounts[each.value.actor]
  role               = "roles/iam.serviceAccountUser"
}

resource "google_artifact_registry_repository_iam_member" "images" {
  for_each   = local.registry_grants
  project    = var.project_id
  location   = var.region
  repository = each.value.repository
  member     = local.accounts[each.value.account]
  role       = each.value.role
}

resource "google_storage_bucket_iam_member" "objects" {
  for_each = local.bucket_grants
  bucket   = each.value.bucket
  member   = local.accounts[each.value.account]
  role     = each.value.role
}

resource "google_storage_bucket_iam_member" "prefixes" {
  for_each = local.conditional_bucket_grants
  bucket   = each.value.bucket
  member   = local.accounts["build"]
  role     = each.value.role
  condition {
    title      = each.value.title
    expression = "resource.name.startsWith('projects/_/buckets/${each.value.bucket}/objects/${each.value.prefix}')"
  }
}

resource "google_secret_manager_secret_iam_member" "session" {
  project   = var.project_id
  secret_id = "groundwork-session"
  member    = local.accounts["api"]
  role      = "roles/secretmanager.secretAccessor"
}

resource "google_secret_manager_secret_iam_member" "providers" {
  for_each  = local.provider_secret_grants
  project   = var.project_id
  secret_id = each.value.secret
  member    = local.accounts[each.value.account]
  role      = each.value.role
}
