# Agent configuration

Agent configuration belongs in an experiment's resolved YAML. The runner writes
that configuration into every trace and removes credential fields from the
manifest, so collaborators can reproduce a run without sharing secrets. New
typed experiments bind each agent to a role declared by `ReleaseDeclaration`;
legacy `defaults.agents` remains supported during migration.

## Typed experiment configuration

Use this form for a new release. Every explicit agent names an release
role, and the number of agents for each role must equal that role's declared
count. This makes the episode topology inspectable before any model is called.

```yaml
schema_version: 1
name: buyer_seller_local
release: ../environments/buyer_seller_tiny_v1.yaml
agents:
  - role: buyer
    type: llm
    model: my-local-model
    api_format: openai
    api_base: http://host.docker.internal:11434/v1
    temperature: 0.2
    max_tokens: 1024
  - role: seller
    type: llm
    model: my-local-model
    api_format: openai
    api_base: http://host.docker.internal:11434/v1
    temperature: 0.2
    max_tokens: 1024
episodes: [{label: baseline, count: 1, seeds: [7]}]
storage: {backend: sqlite, path: ./results/buyer_seller.db}
observability: {capture_content: true}
```

The role count check is intentional: leaving a `role` out or adding an extra
agent is a configuration error, not a runtime convention.

## Minimal configuration

The following legacy form remains useful for existing experiments:

```yaml
defaults:
  environment_id: buyer_seller
  agents:
    - type: llm
      model: gpt-4o-mini
      temperature: 0.2
      max_tokens: 1024
    - type: llm
      model: gpt-4o-mini
      temperature: 0.2
      max_tokens: 1024
```

`agents` is ordered by seat. A environment may define more `type` values, but all
shipped games accept `llm`; their smoke tests substitute deterministic scripted
agents and require no provider access.

## Credentials and endpoints

Put credentials in `.env` (or the deployment release), never committed
YAML. `make_llm_client` infers a provider from `model` unless you override it.

| Model / configuration | API format | Credential source |
|---|---|---|
| `gpt-*`, `o1-*`, `o3-*`, `o4-*` | `openai` | `OPENAI_API_KEY` |
| `claude-*` (direct API) | `anthropic` | `ANTHROPIC_API_KEY`, then `ANTHROPIC_AUTH_TOKEN` |
| `gemini-*` | OpenAI-compatible | `GOOGLE_API_KEY` |
| model identifier containing `/` | OpenAI-compatible | `OPENROUTER_API_KEY` |
| `llama`, `mistral`, `qwen` | OpenAI-compatible | `OLLAMA_API_KEY` (usually optional locally) |
| Vertex model | `vertexai`, `vertexai_anthropic`, or `vertexai_openai` | ADC plus `GOOGLE_CLOUD_PROJECT` |

Use explicit fields only when inference is not enough:

```yaml
agents:
  - type: llm
    model: my-local-model
    api_format: openai
    api_base: http://host.docker.internal:11434/v1
    api_key: ${OLLAMA_API_KEY:-ollama}
    temperature: 0.0
    max_tokens: 1024
    rating_player_id: my-local-model-baseline
```

`api_format` supports `openai`, `anthropic`, `vertexai`,
`vertexai_anthropic`, and `vertexai_openai`. Vertex-only fields are
`gcp_project`, `gcp_location`, and `vertex_adc_file` (or `adc_file`). They are
optional integrations, not part of the local stack.

For direct Anthropic clients, an unset or empty `ANTHROPIC_API_KEY` falls back
to `ANTHROPIC_AUTH_TOKEN`. Tokens starting with `sk-ant-oat` use Bearer auth and
the `oauth-2025-04-20` beta header. This works for in-process CLI seats when the
variable is present in the runner's environment. It is not end-to-end platform
OAuth support: the pool's credential indicator checks only its named variable,
`local_process` children forward `ANTHROPIC_API_KEY` only, and the supplied
platform Compose/runtime secret configuration passes API keys only. Use
`ANTHROPIC_API_KEY` for those paths. Keep either credential private.

## Reproducibility and checks

- Keep behavioral knobs such as model, endpoint, prompt variant, temperature,
  token limit, and `rating_player_id` in YAML.
- Keep keys in `.env`; do not add `api_key` to a committed experiment.
- Run `a2a-run EXPERIMENT --dry-run` to validate provider configuration without
  writing episodes. Run `--smoke-test` to exercise the environment and SQLite path with
  scripted agents instead.
- Give prompt variants distinct `rating_player_id` values. Calendar ratings use
  it in preference to `model`, preventing different variants from being pooled.

See `docs/ADDING_A_GAME.md` for the environment-side agent contract and
`docs/LOCAL_STACK.md` for containerised local-model networking.

## Calendar Seat Runtimes

`runtime` is independent of agent kind and defaults to `in_process`. Calendar supports mixed in-process, launched local-process, and independently admitted external seats. Other games retain in-process execution; `human` and unknown runtimes fail before launch. In-process DSM, scripted, DSPy, and LLM clients retain their existing factory and synchronous interface.

```yaml
defaults:
  environment_id: calendar
  num_agents: 3
  agents:
    - {id: alice, type: scripted, runtime: in_process}
    - {id: bob, type: llm, runtime: local_process, harness: structured_output, model: gpt-4o-mini, temperature: 0.0}
    - {id: carol, type: llm, runtime: external}
```

Local scripted seats use `harness: scripted`; local model seats use `harness: structured_output` and require `type: llm` and a model. The harness owns the provider client, raw responses and conversation history. Provider keys come from the selected provider's environment variable, never inline YAML; children inherit only required credentials, not sibling tickets, runner signing keys or exporters. External model credentials and implementation belong entirely to the joining runtime. Its bounded model/implementation self-report is not verified model provenance.

External seats require `A2A_PROVISIONING_DIR` naming a dedicated owner-only directory. The runner publishes one private descriptor per seat/attempt; `a2a-agent --join-descriptor PATH` joins through the callback challenge and ready acknowledgement. For external structured models, also pass `--harness structured_output --model-config MODEL.json` with nonsecret provider configuration and credentials in the external owner's environment. All callback/MCP addresses remain literal loopback. See `docs/AGENT_RUNTIME_CONTRACT.md` for signing, limits, admission, schemas, and the independent conformance command.

Smoke substitutes scripted policy but preserves child processes and MCP transport, persists SQLite, and verifies readback. Dry-run preserves existing credential checks and does not persist; it also retains remote transport with scripted policy. Neither mode silently collapses remote seats back in-process. A live provider-backed run is optional research follow-up, not an acceptance test. The ordinary cloud-free mixed-runtime example is:

```bash
uv run a2a-run games/calendar/experiments/mcp_mixed_smoke.yaml --smoke-test --max-parallelism 2 --results-dir results/mcp-smoke --storage-path results/mcp-smoke/a2a.db
```

Communication topology can be authored directly in `communication.topology` using complete/ring/star/edges graphs, channel enablement, shared per-agent/per-round budget, and phase overrides. Typed designs use the categorical `communication_topology` factor with named profiles (`complete`, `ring`, `star`, `silent`, `phase_shift`); profile resolution expands before hashing. Do not combine a profile with a direct topology. Explicitly empty override channels mean silence; omitted fields inherit. Legacy `communication_protocol` remains supported when topology is absent. Behavioral runtime/topology changes affect provenance; transient ports, callback addresses, tickets, secrets and capabilities do not.

## The agent pool

An experiment can name its agents inline, or name **participants** from a pool
and let the pool supply the endpoint. The second form exists because the same
logical agent reaches a different provider for different people — Claude through
the Anthropic API, through OpenRouter, or through Vertex under ADC — and that is
a property of who is running the study, not of the study.

```yaml
# games/word-guess/experiments/pool_example.yaml
participants: [gpt-mini, haiku]     # one entry per agent seat, positional
```

`experiments/agents.yaml` holds the shared bindings. Copy an entry into
`agents.local.yaml` at the repository root to change how *you* reach a provider;
that file is gitignored and shadows the shared defaults entry by entry, so the
experiment config stays portable.

```yaml
# agents.local.yaml — same logical name, different route
agents:
  haiku:
    model: anthropic/claude-haiku-4.5
    api_format: openai
    api_base: https://openrouter.ai/api/v1
    credential: OPENROUTER_API_KEY
```

`credential` names the expected environment variable for pool inspection.
Values go in `.env`. It is metadata: hydration drops this field, and the client
factory/runner preflight selects credentials using the concrete model's provider.
Naming an arbitrary variable here does not make the client read it.

The direct Anthropic pool includes `haiku-5.5`, `sonnet-5.5`, and `opus-5.5`,
resolving to `claude-haiku-5-5`, `claude-sonnet-5-5`, and `claude-opus-5-5`.
They use `ANTHROPIC_API_KEY` on the platform. Existing `haiku` and `haiku-or`
bindings retain Claude Haiku 4.5. The 5.5 registry entries omit `temperature`
from requests; changing a pool binding alone would not supply that API rule.
These bindings declare text input only; image support requires the explicit
capability evidence described below.

The shipped pool includes three OpenRouter bindings: `ds-flash`
(`deepseek/deepseek-v4-flash`), `ds-v4.1-flash`
(`deepseek/deepseek-v4.1-flash`, verified text and image input), and `haiku-or`.
One `OPENROUTER_API_KEY`
reaches many vendors cheaply, which makes it the practical way to explore a
line-up before committing to a provider. `haiku` and `haiku-or` are the same
model by two routes, which is the clearest illustration of why the pool is a
layer rather than a lookup table. Any OpenRouter model id works: copy an entry
and change `model`.

Three rules keep a line-up unambiguous:

- **Positional.** The number of participants must equal the agent slots the
  release declares (`roles`), or `num_agents` for a pre-typed config. A
  mismatch is a load-time error, never a truncation.
- **Inline `agents:` wins and stays valid.** A config that already names agents
  is hydrated; the pool is not consulted.
- **Declaring both is an error.** Two sources for one line-up diverge silently.

The pool is an input to resolution, not part of the record: what a run persists
is the hydrated config with concrete model strings, exactly as an inline config
would. A colleague reading the trace sees what actually ran.

Image input requires explicit `capabilities.input: [text, image]` and a
`capabilities.evidence` reference on the concrete binding, plus a supported
provider adapter. Unknown support remains text-only. See
[MULTIMODAL_INPUTS.md](MULTIMODAL_INPUTS.md) for agent-pool, design-roster, and
remote runtime examples.

## Onboard a model endpoint

Runtime support and published choices are separate steps. The model registry is
an API-parameter lookup, not an allowlist or the source of the Agents screen.
An inline `model: claude-sonnet-5-5` can work in `a2a-run` while no corresponding
pool choice appears in the UI. Complete this path when adding an endpoint:

1. **Resolve the provider and wire format.** Check
   `a2a-engine/a2a_engine/llm/factory.py` and `llm/providers.py` for model-name
   inference, base URL and credentials. An existing provider usually needs only
   configuration; a new protocol needs an adapter in `llm/api.py`.
2. **Register API quirks when needed.** Update `llm/model_config.py` for token
   parameter names, default token budgets or unsupported sampling parameters.
   Unknown model IDs otherwise use fallback rules. Verify outgoing payloads
   with mocked HTTP, including omission of unsupported parameters.
3. **Publish a binding.** Add a distinct logical name in
   `experiments/agents.yaml` with `type`, concrete `model`, `api_format`,
   `credential`, and a description; set `api_base` for custom endpoints. Keep
   existing names stable so new studies do not silently change models. For
   private routing use `agents.local.yaml`; `A2A_AGENT_POOL=/path/to/pool.yaml`
   replaces discovery entirely rather than merging with the shared pool.
4. **Resolve each authoring path.** CLI experiments use positional
   `participants: [haiku-5.5, sonnet-5.5]` (matching the release's seat count), or
   inline `agents` with concrete model IDs. UI designs use pool names in the
   roster, for example:

   ```yaml
   roster:
     - {id: seller, role: seller, kind: llm, binding: sonnet-5.5}
     - {id: buyer, role: buyer, kind: llm, binding: opus-5.5}
   ```

   The design compiler hydrates bindings before freezing the plan. Test that
   the frozen agents contain concrete model IDs and formats, with no secrets.
   Locked experiments retain their resolved bindings and runtime pins; create
   or fork a design to select a newly published model.
5. **Supply runtime credentials and rebuild.** The platform API reads the pool
   through `GET /api/agent-pool`; the Agents page and roster choices consume
   that response. No frontend model list needs editing. Provider secrets belong
   in the runtime service (local `compose.platform.yml`, or the hosted secret
   setup in [deploy/README.md](../deploy/README.md)), never the pool or bridge
   request files. Rebuild/recreate the selected stack following
   [INSTALLATION.md](INSTALLATION.md#refresh-and-stop), including both platform
   and science-runtime images. Reload Agents and check the authenticated API
   response. Hosted availability requires a separate release of the change;
   editing the checkout or restarting an old image does not publish it.

Run the focused offline checks from the repository's Python environment:

```bash
python -m pytest a2a-engine/tests/test_agent_pool.py a2a-engine/tests/test_experiment.py
python -m pytest cloud_platform/tests/test_viewer.py cloud_platform/tests/test_catalogue.py
```

Use `--smoke-test` for scripted execution and SQLite readback. Neither a smoke
run nor a mocked provider request proves live provider access. Run
`a2a-run EXPERIMENT --dry-run` only when provider-backed validation is requested;
it can contact providers and incur usage.
