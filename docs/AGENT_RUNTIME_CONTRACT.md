# Agent Runtime Contract: a2a-turns/1 And a2a-turns/2

Every environment owns its clock, ordering, authorization, validation, resolution, seeds and event log. All four shipped games execute LangGraph programs; Calendar additionally owns calendar commits and message routing. The reference package requires Python 3.12+ and pins MCP SDK 1.26.0. Application protocol negotiation is separate from MCP's transport version. `a2a-turns/1` remains the explicit legacy loopback adapter; `a2a-turns/2` admits short requests and reports eventual completion independently. There is no automatic fallback after ambiguous delivery.

The sections through Schema And Conformance describe the unchanged `/1` contract. The Acknowledgement-First Protocol section specifies `/2` and the private two-container probe.

## Admission And Identity

The runner provisions one ticket per remote seat and attempt. `local_process` children receive only their ticket and join URL through environment variables. `external` owners receive an exclusive mode-0600 JSON descriptor in a dedicated owner-only provisioning directory; this is not an experiment artifact or public log. Descriptors contain `join_url`, `seat`, `expires_at` (Unix seconds), and `join_ticket`, and are removed on teardown.

1. The agent posts a `JoinRequest` to `/episodes/{id}/join`, including its ticket, literal loopback callback base URL, supported versions, and bounded self-reported `agent_info`.
2. The environment atomically consumes the ticket before posting `/hello` with a nonce, signed using the ticket as bootstrap key. No game state is included. The runtime verifies the signature and returns the nonce with `accept: true`; a failed challenge does not make the ticket reusable.
3. After the successful challenge, the environment returns a separately minted seat secret, the two MCP URLs, negotiated protocol, and `ready_url`.
4. The runtime installs the secret and acknowledges `ready_url` using `Authorization: Bearer <seat_secret>`. No game data is dispatched before this barrier.

Tickets cannot authenticate later pushes. `/1` URLs must use HTTP with literal loopback addresses and explicit ports, without credentials, query, fragment, or redirects. Host and Origin must match the actual bound server. A generic private IP, Compose service name, or public address is not trusted. Legacy Compose children share the runner/viewer container's loopback namespace; MCP and callback ports are never published. `/2` uses separately provisioned exact service origins as described below. Production TLS/mTLS remains outside this local deployment contract.

## Signed Pushes

Every notification or action invocation has a unique `turn_id` and an absolute timezone-aware UTC `deadline`. `x-a2a-id` and `x-a2a-deadline` must equal the fields in the body. `x-a2a-signature` is the hex HMAC-SHA256 of these exact bytes, using the seat secret after admission:

```text
UTF8(turn_id) + b"\n" + UTF8(deadline.isoformat()) + b"\n" + exact_body_bytes
```

The callback verifies the signature, version, episode, seat, MCP URLs, deadline, and registration state before executing. Duplicate pushes share one in-flight execution or cached completion; changed bodies with the same ID fail. Cache entries expire at the invocation deadline and live entries are never evicted for capacity. Model history is serialized per seat. Runtime errors produce sanitized codes, not exception bodies or raw provider responses.

| Kind | Meaning | Capability |
|---|---|---|
| `register` | Environment system prompt, game config, action schemas and action-to-tool mapping | None |
| `round_start` | Meeting, calendar, round and penalty notification | None |
| `turn` | CHEAP_TALK, VOLUNTARY, DECISION or DECISION_RETRY action hand-off | Fresh turn capability |
| `reflect` | Measurement-only model query, outside conversation history | None |
| `episode_end` | Final notification before revocation and teardown | None |

A retry preserves `parent_phase: DECISION | VOLUNTARY`, conflict and attempt information. Voluntary retries never gain schedule authority. `prompt` is the environment-rendered text and the reference model strategy uses it verbatim. `inbox` is delivered deterministically in the addressee's next push and rendered prompt; `read_inbox` only re-reads that snapshot.

## MCP Calls And Resolution

Each action call uses `Authorization: Bearer <capability>` and a stable `_meta["a2a/call_id"]`. Capabilities are HMAC-signed claims scoped to episode, attempt, seat, currently open turn, phase, tools, and expiry. No tool accepts caller-selected seat identity. SDK sessions are not authority. A duplicate call ID with identical endpoint/tool/arguments returns its original outcome without duplicate delivery or budget charging; changed arguments fail.

| Endpoint | Tools | Resolution |
|---|---|---|
| `/episodes/{id}/env/mcp` | `get_observation()` | Immutable pushed observation |
| env | `schedule(meeting_id, slot, justification?)` | Participant cell, end of phase |
| env | `reschedule(item_id, from_slot, to_slot, justification)` | Participant cell at end of phase; voluntary cell at end of turn |
| `/episodes/{id}/comm/mcp` | `list_peers()`, `read_inbox()` | Read-only current topology/inbox snapshot |
| comm | `send(channel, content, to?)` | Environment-routed at end of turn |

Tool discovery is fixed per endpoint; permission is enforced per call. There is no lifecycle, yield, polling, or `end_turn` tool. An accepted MCP action is staged, not committed: calendar validates schedules and all reschedules together, applies participant cells atomically, and can reject/roll back them. Explicit topology authorizes before charging budget; legacy policy retains existing accounting. Rejections are drained and emitted once by the calendar worker, never by the HTTP thread.

The environment closes on the first permitted completion, configured environment condition, deadline, or unreachable delivery. Calendar honors completion only after the entire cell, including empty or invalid yields. Accepted calls survive timeout; late calls cannot mutate state. Admission no-show stops the episode with `seat_unavailable`; there is no invisible scripted fallback. Push/MCP transport retries preserve IDs and are bounded by one absolute deadline. Turn timeouts default to 120 seconds and can vary by phase; join deadline is configured separately.

## Bounds And Telemetry

- Push/response body: 262144 bytes; per-message content: 16384 characters.
- Recorder: 128 distinct attempts and 262144 aggregate argument bytes per turn, separate from game messaging budgets.
- Reference runtime: at most 128 cached/live entries and 16 in-flight entries; entries expire at their deadline.
- Admission metadata: 16 entries, 64-character keys, 256-character values; allowed self-reported fields are name, version, and implementation.
- Completion has no authoritative actions. Optional finite nonnegative latency and nonnegative integer token usage (at most 1000000000 per field) are self-reported. Raw responses and extracted thinking stay in the agent process; the environment persists numeric telemetry and sanitized reflection measurements, not raw provider content.

Structured extraction retains calendar's object/bare-list, fence-strip, optional JSON repair, and regex recovery parser. The register mapping selects destinations, not model-provided tool names. MCP still enforces schemas and phase/topology authority. Native provider tool calling (`harness: native_tools`) is reserved and explicitly unsupported.

The model strategy clips provider request timeouts to the remaining invocation deadline and disables provider-level retries. Reflection retains delta/logprob extraction and may retry once without unsupported logprob options, within the same deadline. Cancelling a synchronous provider call cannot stop its thread immediately; a separate lock prevents overlap with subsequent model calls, and cancelled results cannot append history or stage actions. The runtime's invocation timeout remains the authoritative bound.

## Schema And Conformance

`docs/a2a-turns-1.schema.json` is generated from the public Pydantic models. Regenerate with `uv run python scripts/export_turn_contract.py`; `--check` fails on any drift. Calendar-specific action schemas are supplied at register/tool discovery, not duplicated in this generic bundle.

The conformance command requires the optional Calendar package and drives a real tiny calendar episode against an independently hosted endpoint implementing the deterministic reference scripted policy. It checks admission/hello, concurrent identical pushes, fixed tool discovery and read snapshots on both MCP surfaces, stricter malformed-input rejection, exactly-once sends, and calendar commits. This is a scripted behavior gate, not a claim that arbitrary model outputs match a scripted policy.

```bash
uv run a2a-conformance --callback-url http://127.0.0.1:8765 --provisioning-dir /tmp/a2a-private-joins
```

While it waits, the endpoint owner reads the freshly created descriptor in that dedicated private directory and starts their runtime. The reference command is:

```bash
uv run a2a-agent --port 8765 --join-descriptor /tmp/a2a-private-joins/ATTEMPT-seat-0.json
```

Do not print, commit, or copy the descriptor into logs. Use a fresh runtime per episode. The command emits only a sanitized pass/fail report and removes provisioning on all exit paths. Cloud-free CI also runs the packaged mixed smoke experiment, independent scripted subprocess conformance, and mock-model prompt/action tests. No live provider calls are an acceptance gate.

## Acknowledgement-First Protocol: a2a-turns/2

### Negotiation And Network Authority

`JoinRequest.protocol_versions` explicitly selects `/2` when supported. A `/2` bootstrap challenge and admission include `environment_id` and `attempt_id` in addition to episode and seat. The ticket is consumed once, authenticates only the challenge, and is removed from runtime bootstrap state after admission. The ready barrier remains mandatory. Container mode rejects `/1`; a loopback harness can deliberately choose either protocol. `PROTOCOL_VERSION` and the existing `/1` schema/conformance fixtures are unchanged.

`NetworkPolicy` distinguishes the socket bind from the advertised origin. The local container servers bind `0.0.0.0`, advertise the exact configured Compose service origin, and validate Host and any supplied Origin against that origin. Endpoints must match provisioned HTTP origins with explicit ports; arbitrary private IPs, unrelated service names, credentials, query strings, fragments, redirects, and alternate callback paths are rejected. Admission uses the configured environment origin, not untrusted request headers, to construct the endpoint map. The agent checks this exact map against its private join URL. Loopback mode retains literal-IP validation.

### Wire Shapes

Every `/2` invocation, acceptance, completion, and receipt carries the complete identity: `protocol_version`, `environment_id`, `episode_id`, `attempt_id`, `seat`, and `turn_id`.

| Shape | Additional Fields | Meaning |
|---|---|---|
| `TurnInvocationV2` | kind, bounded operation, phase/parent phase, absolute deadline, observation, prompt, inbox, MCP endpoints, optional action capability, lifecycle credential | One invocation, including lifecycle-only work |
| `TurnAcceptance` | status accepted/rejected, safe reason code | Validation and admission to an in-memory runtime-owned task, not model progress or durable delivery |
| `TurnFinished` | status succeeded/failed, safe failure code, numeric telemetry, optional reflection | Immutable terminal outcome; never authoritative game actions |
| `CompletionReceipt` | disposition recorded/duplicate/closed | Correlated lifecycle acknowledgement, not renewed action authority |

The operation vocabulary includes `register`, `observe`, `act`, `reflect`, `episode_end`, and the integration-only `probe`. Negotiation additionally admits `initialize_session`, `reset_context`, `project_update`, `error_feedback`, `cheap_talk`, `decide_allocation`, and `notify_round_result`. These backend operations (including Negotiation reflection) use action-bearing `turn` invocations with an exact uppercase operation phase, no retry parent, and one game-owned `submit_candidate` result. Standard registration/end are lifecycle-only; session initialization separately returns a bounded prompt and finite optional-behavior set. Private backend methods are adapted inside the agent strategy, never accepted as RPC operation names. Calendar action turns retain the existing supported phases and retry-parent restrictions. Other environments cannot use the Negotiation vocabulary.

Negotiation candidates carry bounded text or integer allocation/project quantities plus allowlisted output/usage metadata, never raw provider responses. Overbudget/negative candidates reach environment validation rather than being transport-rejected. Provider retries remain within one invocation; the executable decision graph permits three total game validation attempts. History reset, project/error feedback, and result notification execute agent-side before completion. Failed/missing/expired results do not invent allocations. A lingering synchronous provider worker holds its session's model lock after cancellation, preventing history overlap. Backend stream-start events are replayed on the environment worker after correlated completion, preserving event order without exposing a new live streaming channel.

### Request And Completion Ordering

1. The environment opens a lifecycle pending record and registers its waiter before dispatch. Every register, round notification, reflection, action turn, and episode-end invocation has its own record, even without game tools.
2. The environment calls the agent's Streamable HTTP MCP `start_turn` tool at `/mcp`, authenticated with the admitted seat secret. Its ordinary MCP result contains `TurnAcceptance`; it is not HTTP 202 and is never parsed as `TurnCompletion`.
3. Admission atomically reserves one execution slot for the episode/attempt/seat session. Identical starts return the same acceptance without repeating execution; changed bodies conflict. Another observation for that busy session is rejected, not queued. Different admitted sessions can execute independently.
4. The task owns model execution/history and awaits its action tool outcomes. Game tool calls retain stable call IDs, deduplication, action capabilities, and the existing recorder/worker authority. The request that admitted the task can end without cancelling it.
5. The runtime finishes history and action work, installs an immutable `TurnFinished`, and releases the execution slot before requesting a completion receipt. The next invocation can begin as soon as the environment records completion, even while the previous receipt is still in flight. A lingering synchronous provider thread keeps the model/history unavailable until its lock is released.
6. The runtime calls `complete_turn` at `/episodes/{id}/lifecycle/mcp` with the separate lifecycle credential. The environment authenticates the full identity from the registered credential, closes/revokes action authority, snapshots the recorder under its lock, then signals the waiter. No MCP callback applies Calendar mutations or emits domain events.
7. The graph/Calendar worker consumes the closed staged snapshot and applies its game policy. Calendar keeps previously staged actions after failure/expiry, as `/1` does. Acceptance alone never closes or completes the recorder.

The lifecycle credential is a random, registry-bound secret separate from the signed action capability. It grants only terminal reporting/duplicate receipts, never game tools, and spends no action-call or message budget. A caller-selected attempt/seat/turn cannot substitute for its registered identity. Identical completions are idempotent; changed terminal bodies fail. Reports after closure return `closed` without modifying snapshots. A brief grace permits duplicate receipts after deadline/action revocation, including episode teardown; it cannot extend execution authority.

### Failure, Retention, And Cleanup

The default start-request timeout is 2 seconds, independent of the environment's longer game deadline (the probe deliberately uses 0.5 seconds). Ambiguous start retries retain the identical body, identity, credentials, and deadline. A valid correlated completion wins over a lost acknowledgement. A confirmed rejection closes promptly. Exhausted ambiguous start delivery and loss of an accepted runtime wait until the environment deadline; neither creates a fresh turn nor triggers `/1` fallback. Completion reporting has its own bounded retries using the same terminal body.

Default runtime bounds are 128 sessions, 128 executing sessions, and 1,024 invocation records. Sessions expire after one hour of their admission unless ended sooner. The environment bounds its pending/terminal registry to 1,024 records. Terminal lifecycle credentials and runtime records have a 5-second post-deadline/closure grace. Active tasks/records are never evicted to make capacity; overload rejects admission. The private provisioning watcher periodically expires ended/expired idle sessions and terminal records. Shutdown cancels/awaits runtime tasks and clears sessions; episode teardown closes all pending waiters and revokes action authority. Crashes lose in-memory state and fail/expire turns, not resume them. No checkpointer or durable turn recovery is installed.

Provider request timeouts remain clipped to the authoritative deadline. Cancelled provider results cannot append history or stage actions, and the provider lock prevents overlap with subsequent model use. No provider credentials belong in the environment's `/2` deployment. The supported agent service selects a game-specific per-session strategy from a private descriptor; integration probes still use controlled policies. Scripted smoke never creates providers. Descriptors allow only type/model/API-format/base/temperature/token configuration, never inline keys or arbitrary strategy code; credentials are resolved only in the agent process.

### Private Compose Probe And Tests

`docker-compose.agent-runtime.yml` adds two services on an internal-only network: `runtime-environment` uses the same environment image as the base Compose stack and runs the executable `StateGraph` probe; `runtime-agent` uses its own Python image with no viewer build, workspace, trace database, results, or Docker socket. Neither runtime publishes ports. The environment writes the one-time descriptor into a private per-seat volume; the agent mounts only that seat's directory read-only, not sibling tickets or environment storage. Files are owner-only and read without following symlinks. Tickets, capabilities, lifecycle credentials, and addresses never enter experiment config or probe results.

Build/run only these integration services (do not start the unrelated default runner/viewer services):

```bash
docker compose --env-file /dev/null --profile agent-runtime -f docker-compose.yml -f docker-compose.agent-runtime.yml up --build -d --wait runtime-environment runtime-agent
```

The probe's local `/status` and `/release` endpoints are deterministic test controls, not game/runtime authoring APIs. The agent stages an action and services an immediate worker-owned tool, then waits on the private release signal while the graph remains pending beyond the short request timeout. Releasing it causes an independent opposite-direction MCP completion, closed-snapshot consumption, and episode-end notification without terminating the long-lived agent process. The standalone `docker-compose.runtime-launch.yml` deployment now provides all-game CLI/viewer provisioning through the ordinary runner; this probe remains a transport test, not a replacement runner.

The real Docker tests create disposable Compose projects, build current images, verify health/network/scoped mounts, hold and complete the turn, kill an accepted runtime to verify deadline closure and snapshot survival, check redaction/session-only episode end/process exit, and remove only their projects' containers/network/volumes in `finally`. Docker unavailability fails this gate rather than skipping it. Run with a build timeout long enough for fresh image dependencies:

```bash
uv run pytest tests/runtime/test_container_turns.py
uv run pytest a2a-engine/tests/test_remote_*.py a2a-agent/tests games/calendar/calendar_game/tests/test_remote_*.py
uv run python scripts/export_turn_contract.py --check
uv run python scripts/export_turn_contract.py --protocol-version a2a-turns/2 --check
```

`docs/a2a-turns-2.schema.json` is a separate generated bundle. Explicit version selection preserves the original default `/1` exporter/check. The engine pins local LangGraph 1.0.10 alongside MCP 1.26.0; there is no LangGraph Server or LangSmith service requirement. Tests compare Calendar `/2` actions, normalized events, final state, and metrics with local and `/1` controlled baselines, and exercise lifecycle races, credential scope, deduplication, overload, expiry, and runtime cleanup.

### Supported Four-Game Deployment

`A2A_RUNTIME_MODE=compose` enables the runner's provisioning manager in both CLI and viewer subprocesses. The environment binds one available port in `8100`–`8131`, advertises the exact `viewer` origin, health-checks the selected agent seats, and creates a separate episode registry with the game's tool set. At most five explicit non-human seats are supported. The agent service watches only its own read-only directory, chooses a per-session Calendar/Word Guess/Buyer-Seller/Negotiation strategy and negotiates `/2` without fallback. The viewer does not need a Docker socket or agent credentials.

Success/failure/graceful cancellation closes environment records, removes private descriptors and causes only that agent session's unfinished tasks to be cancelled. Signal cleanup also prevents a cancelled trace from being persisted as successful. Session/record retention remains bounded; this does not add durable recovery. Concurrent mixed-game launches have separate tools, capabilities, histories and deterministic runner provenance. The supported container suite checks eight parallel CLI episodes, concurrent browser/API launches, a repeated attempt, cancellation, redacted persisted readback and per-seat mounts; run `uv run pytest tests/runtime` for the full required gate.

Only `docker-compose.runtime-provider.yml` enables agent provider egress and injects `OPENROUTER_API_KEY`, never into the viewer/environment. Internal `/credentials` returns provider-presence booleans, not values, so viewer readiness and runner preflight can check the agent service. Do not publish agent ports or print resolved provider Compose configuration. See `docs/LOCAL_STACK.md` for cloud-free startup and the bounded DeepSeek V4 Flash example; model/account availability still requires an explicitly approved live run.
