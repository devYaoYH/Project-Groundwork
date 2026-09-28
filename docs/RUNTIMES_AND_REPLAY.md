# Environment runtimes and Redis replay

Every shipped environment has two pre-Milestone-2 deliverables:

| Environment | Runtime release | Browser replay |
|---|---|---|
| Calendar | `games/calendar/runtime/release.json` | `/environment-replays/calendar/` |
| Negotiation | `games/negotiation/runtime/release.json` | `/environment-replays/negotiation/` |
| Buyer-Seller | `games/buyer-seller/runtime/release.json` | `/environment-replays/buyer-seller/` |
| Word Guess | `games/word-guess/runtime/release.json` | `/environment-replays/word-guess/` |

Run a reviewed local release with the shared CLI:

```bash
python scripts/run_game_runtime.py --release games/word-guess/runtime/release.json --smoke-test
```

Each runtime directory also contains a `Dockerfile` and `run.sh`. Build it from
the repository root after building the shared `a2a-comm-local:latest` base:

```bash
docker compose build runner
docker build -f games/word-guess/runtime/Dockerfile -t a2a-word-guess-runtime .
docker run --rm --network a2a-comm_default -e A2A_REDIS_URL=redis://redis:6379/0 -e A2A_LAUNCH_ID=my-launch a2a-word-guess-runtime --smoke-test
```

## Event streams and recovery

Compose starts Redis with AOF enabled on the `a2a-redis` volume. When a runner
has `A2A_REDIS_URL` and `A2A_LAUNCH_ID`, each episode writes normalized
`Event` objects to:

```text
a2a:launch:<launch_id>:episode:<experiment>.<cell>.<episode_idx>
```

Redis is a live projection for replay pages, not the recovery source. Every
episode writes the same events to a durable JSONL sink as they happen, and a
dispatched worker also publishes each line to the shared artifact store under
`launches/<launch_id>/episodes/<episode_id>/<episode_uid>/`. That artifact is
what the control plane reads after a worker crash, which is why recovery works
without the reader mounting the worker's filesystem.

Recovering a crashed episode is not a manual step. The control plane's
reconciler settles every open launch from that evidence and persists whatever
the interrupted episode's event log held as a trace with `stopped: true` and
`observability.durable_through` recording how far durability actually reached.
It is evidence for inspection and retry, not a successful experimental result.

The standalone `scripts/recover_redis_stream.py` that used to be documented
here is gone: it referenced an argument it never registered, so every
invocation raised `AttributeError`, and the durable path above replaced the
need for it.

The local control plane proxies a stream at:

```text
GET /api/streams/<url-encoded-stream-name>
```

Open the corresponding environment-local replay URL with `?stream=<stream-name>`
to load and step through the logged events. Browsers never make direct Redis
connections.

A replay page also accepts `?episode=<episode_uid>` where the environment's
renderer can drive a persisted episode, which is what lets it mount beside the
standard lane view on `/episode/`. The retired `/replays/<environment>.html`
entry points are gone with the rest of the hand-rolled viewer; each page now
lives with its environment and is served from there.
