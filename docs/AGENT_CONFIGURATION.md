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
| `claude-*` | `anthropic` | `ANTHROPIC_API_KEY` |
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

`credential` names the release variable the binding needs, so a missing key
is reported by name before a run starts rather than as a 401 during it. Values
go in `.env`.

The shipped pool includes two OpenRouter bindings — `ds-flash`
(`deepseek/deepseek-v4-flash`) and `haiku-or` — because one `OPENROUTER_API_KEY`
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
