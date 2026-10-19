# Haiku 5.5 on Vertex AI

The shared `haiku-5.5-vertex` binding uses `claude-haiku-5-5`,
`credential: vertexai_adc`, `api_format: vertexai_anthropic`, and
`config.gcp_location: global`. Only Haiku 5.5 is newly published; existing
Haiku 4.5 bindings stay unchanged, and Sonnet/Opus 5.5 have no shared entries.
The model registry already omits unsupported `temperature` parameters.
Input is text-only until verified image capabilities are declared.

Follow the [endpoint onboarding checklist](AGENT_CONFIGURATION.md#onboard-a-model-endpoint)
for resolution, frozen plans, and release publication. CLI experiments can use
`participants: [haiku-5.5-vertex, haiku-5.5-vertex]`; UI designs select the same
binding in the roster. Match the environment's seat count.

## Runtime setup

1. Choose the target project with `config.gcp_project`, ADC, or
   `GOOGLE_CLOUD_PROJECT`. Enable the Vertex AI API and the Haiku 5.5 partner
   product, `anthropic-932.cloudpartnerservices.goog`, in that project. Complete
   any required model-access form or terms in the console. See Google's
   [model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/haiku-5-5)
   and [Claude setup guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/use-claude).
2. Give the runtime identity `aiplatform.endpoints.predict` and
   `serviceusage.services.use` on the target project. For the supplied Cloud Run
   deployment this is `groundwork-worker@PROJECT_ID.iam.gserviceaccount.com`;
   the API/build/CI identities do not need prediction access. An administrator
   can apply the focused helper from the repository root:

   ```bash
   python -c "from deploy.bootstrap import enable_vertex_adc; enable_vertex_adc('PROJECT_ID', 'RUNTIME_SERVICE_ACCOUNT')"
   ```

   The helper enables Vertex and creates/updates a prediction-only custom role.
   Fresh bootstrap also accepts `--vertex-adc`. Neither path enables partner
   products, creates provider secrets, or grants service-account impersonation.
3. Configure ADC for the actual runtime. Host CLI credentials are not mounted
   into Docker automatically. Keep ADC files private. No Anthropic key or token
   is required, and failures never switch to direct Anthropic authentication.
4. Rebuild/release both images and check the published pool as described in the
   checklist. Existing locked experiments retain their plans and runtime pins.

Model Garden visibility and developer-account success do not establish runtime
access. Check model enablement, runtime IAM, and project quota independently.
When live validation is requested, make a small request under workload ADC to
this model's global endpoint. HTTP 429 is unresolved quota/capacity, not a
successful inference check. Scripted smoke and mocked HTTP tests establish
plumbing only; they do not establish live provider access.
