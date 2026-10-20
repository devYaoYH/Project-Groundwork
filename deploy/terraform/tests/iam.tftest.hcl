# All runs use a mock provider: no ADC, API access, or paid predictions.
mock_provider "google" {
  override_during = plan
}

variables {
  project_id = "test-groundwork"
  region     = "us-central1"
}

run "runtime_prediction_boundary" {
  override_resource {
    target          = google_project_iam_custom_role.roles["vertex"]
    override_during = plan
    values = {
      name = "projects/test-groundwork/roles/groundworkVertexPredictor"
    }
  }

  override_resource {
    target          = google_project_iam_custom_role.roles["catalogue"]
    override_during = plan
    values = {
      name = "projects/test-groundwork/roles/groundworkCataloguePublisher"
    }
  }

  command = plan
  assert {
    condition = toset(google_project_iam_custom_role.roles["vertex"].permissions) == toset([
      "aiplatform.endpoints.predict", "serviceusage.services.use"
    ])
    error_message = "The runtime role must be prediction-only, including API consumption."
  }
  assert {
    condition = (
      google_project_iam_member.grants["worker-vertex"].member == "serviceAccount:groundwork-worker@test-groundwork.iam.gserviceaccount.com" &&
      google_project_iam_member.grants["worker-vertex"].role == google_project_iam_custom_role.roles["vertex"].name &&
      google_project_iam_custom_role.roles["vertex"].role_id == "groundworkVertexPredictor" &&
      google_project_iam_custom_role.roles["vertex"].stage == "GA" &&
      google_project_service.vertex.service == "aiplatform.googleapis.com" &&
      google_project_service.vertex.disable_on_destroy == false
    )
    error_message = "Both Job containers use the worker identity; that identity needs the managed Vertex role."
  }
  assert {
    condition = alltrue([for key, value in google_project_iam_member.grants :
      value.role != google_project_iam_custom_role.roles["vertex"].name if key != "worker-vertex"
    ])
    error_message = "API, build and CI must not gain model prediction access."
  }
  assert {
    condition = (
      length(google_project_iam_member.grants) == 13 &&
      length(google_service_account_iam_member.act_as) == 4 &&
      length(google_artifact_registry_repository_iam_member.images) == 4 &&
      length(google_storage_bucket_iam_member.objects) == 5 &&
      length(google_storage_bucket_iam_member.prefixes) == 3 &&
      length(google_secret_manager_secret_iam_member.providers) == 0
    )
    error_message = "All bootstrap IAM grants must be accounted for, without discovering provider secrets."
  }
  assert {
    condition = toset(google_project_iam_custom_role.roles["catalogue"].permissions) == toset([
      "datastore.entities.create", "datastore.entities.get"
    ])
    error_message = "Catalogue publication must remain create-only with read-back."
  }
}

run "another_project_and_existing_secrets" {
  command = plan
  variables {
    project_id            = "another-groundwork"
    region                = "europe-west1"
    provider_secret_names = ["groundwork-anthropic-api-key", "groundwork-openai-api-key"]
  }
  assert {
    condition = (
      length(google_secret_manager_secret_iam_member.providers) == 4 &&
      google_secret_manager_secret_iam_member.providers["groundwork-anthropic-api-key-worker"].role == "roles/secretmanager.secretAccessor" &&
      google_secret_manager_secret_iam_member.providers["groundwork-anthropic-api-key-build"].role == "roles/secretmanager.viewer" &&
      google_secret_manager_secret_iam_member.providers["groundwork-anthropic-api-key-worker"].member == "serviceAccount:groundwork-worker@another-groundwork.iam.gserviceaccount.com" &&
      google_secret_manager_secret_iam_member.session.member == "serviceAccount:groundwork-api@another-groundwork.iam.gserviceaccount.com"
    )
    error_message = "Only workers can read provider values; the API reads only the session secret."
  }
  assert {
    condition = (
      google_storage_bucket_iam_member.prefixes["build-runtime-read"].condition[0].expression == "resource.name.startsWith('projects/_/buckets/another-groundwork-groundwork-artifacts/objects/platform/runtime-bindings/')" &&
      google_storage_bucket_iam_member.prefixes["build-runtime-read"].condition[0].title == "groundwork-runtime-catalog" &&
      google_storage_bucket_iam_member.prefixes["build-candidates"].condition[0].expression == "resource.name.startsWith('projects/_/buckets/another-groundwork-groundwork-build-source/objects/ci-image-builds/')" &&
      google_storage_bucket_iam_member.prefixes["build-candidates"].condition[0].title == "groundwork-ci-images"
    )
    error_message = "Preserve the existing IAM condition identity and its evidence/source boundary on adoption."
  }
  assert {
    condition = (
      google_artifact_registry_repository_iam_member.images["groundwork-ci"].role == "roles/artifactregistry.reader" &&
      google_artifact_registry_repository_iam_member.images["groundwork-build"].role == "roles/artifactregistry.writer" &&
      google_artifact_registry_repository_iam_member.images["groundwork-ci"].location == "europe-west1" &&
      google_service_account_iam_member.act_as["ci-build"].service_account_id == "projects/another-groundwork/serviceAccounts/groundwork-build@another-groundwork.iam.gserviceaccount.com"
    )
    error_message = "New projects must retain build/CI identity separation and explicit resource scope."
  }
}

run "reject_unrelated_secrets" {
  command = plan
  variables {
    provider_secret_names = ["unrelated-secret"]
  }
  expect_failures = [var.provider_secret_names]
}
