# Model-call routing audit

Audited on 2026-10-20. Provider requests in maintained source now enter
`a2a_engine.llm.api`. The audit covered core packages, game runtimes, standalone
analysis/judge/optimization scripts, platform/deployment scripts, and searches
of JavaScript, TypeScript and shell sources. Built distributions and historical
results are not executable source targets for this migration.

## Findings and changes

| Path | Previous behavior | Current route |
| --- | --- | --- |
| `games/negotiation/negotiation_game/backend/agents/api.py` | Copied provider payload, HTTP, SDK, response and SSE implementations | Compatibility adapter delegates to engine; no transport remains here |
| `games/negotiation/negotiation_game/backend/agents/llm.py` | Stdlib agent used that copy; `LLMAgent` had another HTTPX implementation | Both classes use engine streaming, including callbacks and token metadata |
| `games/negotiation/negotiation_game/backend/agents/model_config.py` | Separate registry could drift from engine | Compatibility exports of the engine registry; legacy Vertex MaaS output ceilings moved into it |
| `games/negotiation/negotiation_judge/run_taxonomy_resumable_batch.py` | Called Google GenAI and Anthropic Vertex SDKs directly | Engine `call_llm_oneshot_full`, retaining text, thinking, duration and raw response evidence |
| Other Negotiation judge/analysis callers and optimizer oracle | Imported the copied API | Import shared API directly |
| Negotiation optimizer `name_projects` | Raw Vertex/Gemini REST requests | Engine one-shot API |
| Calendar RQ2/RQ3 GEPA scripts | Five `dspy.LM` constructors delegated provider transport to LiteLLM | Optional `a2a_engine.llm.dspy.EngineLM` adapter for proposer and reflection calls |

Buyer–Seller, Word Guess, Calendar game clients (including the DSPy prompt
variant), grounding game agents, and the main agent harness already used shared
engine clients. The `a2a-judge` package builds judge prompts/data; it does not
implement a provider transport. Other network clients inspected handle runtime
communication, authentication, storage, telemetry or input downloads.

## API and compatibility

Use `LLMClient`/`make_llm_client` for agent clients, or the low-level shared
`call_llm_streaming`, `call_llm_oneshot`, `call_llm_oneshot_with_thinking` and
`call_llm_oneshot_full` functions where the caller owns retries. Low-level calls
do not add another retry loop. Full one-shot responses retain the provider
response for callers that explicitly save judge evidence; these responses are
not automatically added to logs or telemetry.

Negotiation retains its existing retry/fallback policy and provider selection.
Its historical Anthropic streaming JSON suffix lives in its prompt module and
is applied to a copy of the request messages. The engine stays independent of
Negotiation's prompt schema. Legacy module/class import paths remain available.
Project naming retains its ADC-first credential selection, model, temperature,
token limit and timeout. Its API-key branch now uses the shared Google
OpenAI-compatible endpoint instead of the native Gemini REST endpoint.

The DSPy adapter retains formatting, callbacks, history, cache/rollout identity,
Vertex project/location, token/temperature settings, and shared retry handling.
It supports the single-text-completion workflows in these scripts and rejects
unsupported generation options explicitly. Its cache namespace differs from
the former LiteLLM transport, so old transport cache entries are not reused.
DSPy is imported only by the optional adapter, not by ordinary engine clients.

Existing frozen runtime snapshots/images are not rewritten. New source must be
built into a new runtime release to affect deployed execution.

## Regression boundary

`a2a-engine/tests/test_llm_boundary.py` scans maintained Python source for
provider SDK imports, direct completion calls, model HTTP endpoint construction
and default DSPy provider clients outside the engine LLM package. CI's existing
`a2a-engine/tests` command includes it. This guards the known bypass patterns;
it is not a proof about arbitrary dynamically constructed network requests.

## Verification

The PR branch was validated on current `main` in an isolated checkout, with
current workspace packages installed using `scripts/install_workspace.py`.
All provider tests use mocked transports. No paid model calls were made.

```sh
DSPY_CACHEDIR=/tmp/groundwork-llm-dspy-cache .venv/bin/python -m pytest \
  a2a-engine/tests/test_llm_deadline.py \
  a2a-engine/tests/test_llm_retry.py \
  a2a-engine/tests/test_llm_full_response.py \
  a2a-engine/tests/test_llm_boundary.py \
  a2a-engine/tests/test_multimodal.py \
  a2a-engine/tests/test_tracing_otel.py \
  a2a-engine/tests/test_agent_pool.py \
  a2a-engine/tests/test_haiku_vertex.py \
  a2a-agent/tests a2a-judge/tests games/negotiation/tests \
  games/calendar/calendar_game/tests/test_llmclient.py \
  games/calendar/calendar_game/tests/test_prompts.py \
  games/calendar/calendar_game/tests/test_engine_dspy.py -qx
```

Result: 465 passed. Local socket permission is needed for the deterministic
remote fixtures.

```sh
.venv/bin/a2a-run games/negotiation/experiments/example.yaml \
  --smoke-test --max-parallelism 1 \
  --results-dir /tmp/groundwork-llm-pr-smoke \
  --storage-path /tmp/groundwork-llm-pr-smoke/a2a.db
```

Result: 2/2 scripted episodes persisted to SQLite and read back successfully.
The repository's pinned Ruff lint/format checks and `git diff --check` also pass.
Live provider authentication and current deployed images were not validated.
