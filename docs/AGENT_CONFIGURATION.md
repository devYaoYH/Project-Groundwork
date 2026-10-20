# Agent configuration

Agent configuration belongs in an experiment's resolved YAML. The runner writes
that configuration into every trace and removes secret values from the
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

`model` identifies the model; one nonsecret `credential` enum selects its access
method. Put secret values in `.env` or injected runtime secrets, never YAML.

| `credential` | Provider / `api_format` | Runtime authentication |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic / `anthropic` | Only `ANTHROPIC_API_KEY` |
| `ANTHROPIC_AUTH_TOKEN` | Anthropic / `anthropic` | Only `ANTHROPIC_AUTH_TOKEN` |
| `vertexai_adc` | Vertex / `vertexai`, `vertexai_anthropic`, or `vertexai_openai` | Google ADC |
| `OPENAI_API_KEY` | OpenAI / `openai` | Only `OPENAI_API_KEY` |
| `OPENROUTER_API_KEY` | OpenRouter / `openai` | Only `OPENROUTER_API_KEY` |
| `GOOGLE_API_KEY` | Gemini / `openai` | Only `GOOGLE_API_KEY` |
| `OLLAMA_API_KEY` | Ollama / `openai` | Only `OLLAMA_API_KEY`, if the server requires it |

The selector determines routing and authentication together; a missing key/token,
IAM failure, or quota error never switches accounts. Declare it once at the agent
level. Unknown/multiple selectors, nested `config.credential`, conflicting formats,
inline key overrides, and ADC options on key/token bindings fail validation.
The shared definition is `a2a_engine.llm.credentials.ModelCredential`.

For example, a private Vertex binding in `agents.local.yaml` can select its own
project without changing the runtime's ADC identity:

```yaml
agents:
  claude-lab:
    type: llm
    model: claude-sonnet-4-6
    credential: vertexai_adc
    api_format: vertexai_anthropic
    config: {gcp_project: your-lab-project, gcp_location: global}
```

Vertex requires an explicit Vertex `api_format`; `gcp_project` otherwise comes
from ADC or `GOOGLE_CLOUD_PROJECT`. Optional `vertex_adc_file` (alias `adc_file`)
selects a private ADC file. Set `api_base` for local servers or custom endpoints.
Resolved plans retain the selector and project/location. Local children receive
only the selected key/token or ADC settings; isolated services check the same
selector. The Agents page labels ADC explicitly. See the
[onboarding checklist](#onboard-a-model-endpoint) for publication and deployment.

Legacy inline configurations without `credential` retain model inference and
direct Anthropic's `ANTHROPIC_API_KEY` then `ANTHROPIC_AUTH_TOKEN` lookup. Explicit
OAuth selection also forwards the token to local children; legacy children
forward only the API-key variable. Tokens beginning `sk-ant-oat` retain Bearer
and `oauth-2025-04-20` headers. Existing pool entries with standard variable names
now freeze that selection; custom variable names are not enum members. Hosted
secret wiring supplies API keys only. Frozen plans and runtime pins stay intact.

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

Bindings retain their [credential selector](#credentials-and-endpoints) during
hydration; secret values stay in the runtime environment.

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

The model registry controls request parameters; the agent pool supplies published
UI/CLI choices. Complete both paths when adding an endpoint:

1. **Resolve access.** Follow [credential selection](#credentials-and-endpoints).
   Existing adapters need configuration; new protocols need `llm/api.py` support.
   New access methods also need `llm/credentials.py` and factory resolution.
2. **Register API quirks.** Update `llm/model_config.py` for token limits or
   unsupported parameters; check outgoing payloads with mocked HTTP.
3. **Publish a binding.** Add a distinct name to `experiments/agents.yaml` with
   `model`, `credential`, `api_format`, and a description. Preserve existing names
   and targets. Use `agents.local.yaml` for private routes; `A2A_AGENT_POOL` replaces
   pool discovery rather than merging with the shared pool.
4. **Check resolution.** CLI `participants: [gpt-mini, gpt-mini]` and UI roster
   `binding: gpt-mini` must freeze concrete model/adapter/credential settings,
   without secrets. Match the release's seat count. Existing locked experiments
   retain their plans and runtime pins; create or fork a design for new bindings.
5. **Configure and publish the runtime.** Supply the selected key/token or ADC,
   model access, and runtime IAM separately; see [hosted setup](../deploy/README.md).
   Rebuild both selected images following [installation](INSTALLATION.md#refresh-and-stop).
   Check `GET /api/agent-pool` and the Agents page; they consume the pool directly,
   with no frontend model list. Hosted publication requires a release.

Run `python -m pytest a2a-engine/tests/test_agent_pool.py cloud_platform/tests/test_viewer.py`
and a scripted `a2a-run EXPERIMENT --smoke-test` with SQLite readback. These prove
configuration/execution plumbing, not live access. `--dry-run` can call providers;
use it only when provider-backed validation is requested.
