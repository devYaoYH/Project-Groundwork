# Agent Runtime Contract: a2a-turns/1

Calendar's environment owns the clock, ordering, validation, calendar commits, message routing, seeds, and event log. An admitted runtime receives signed HTTP pushes and acts through two episode-scoped Streamable HTTP MCP endpoints. The reference package requires Python 3.11+ and pins MCP SDK 1.26.0; the application protocol `a2a-turns/1` is separate from the SDK's negotiated MCP transport version.

## Admission And Identity

The runner provisions one ticket per remote seat and attempt. `local_process` children receive only their ticket and join URL through environment variables. `external` owners receive an exclusive mode-0600 JSON descriptor in a dedicated owner-only provisioning directory; this is not an experiment artifact or public log. Descriptors contain `join_url`, `seat`, `expires_at` (Unix seconds), and `join_ticket`, and are removed on teardown.

1. The agent posts a `JoinRequest` to `/episodes/{id}/join`, including its ticket, literal loopback callback base URL, supported versions, and bounded self-reported `agent_info`.
2. The environment atomically consumes the ticket before posting `/hello` with a nonce, signed using the ticket as bootstrap key. No game state is included. The runtime verifies the signature and returns the nonce with `accept: true`; a failed challenge does not make the ticket reusable.
3. After the successful challenge, the environment returns a separately minted seat secret, the two MCP URLs, negotiated protocol, and `ready_url`.
4. The runtime installs the secret and acknowledges `ready_url` using `Authorization: Bearer <seat_secret>`. No game data is dispatched before this barrier.

Tickets cannot authenticate later pushes. URLs must use HTTP with literal loopback addresses and explicit ports, without credentials, query, fragment, or redirects. Host and Origin must match the actual bound server. A generic private IP, Compose service name, or public address is not trusted by this spike. In Compose, children share the runner/viewer container's loopback namespace; MCP and callback ports are never published. TLS/mTLS and separate agent containers remain future work.

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
