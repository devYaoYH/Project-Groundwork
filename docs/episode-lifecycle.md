# Shared Episode Lifecycle

The runner opens an attempt-scoped session and owns its close. Games run setup (including `game_start`), enter `session.started(registrations)`, and execute their play-only graph. The shared stage waits for every remote seat against the join deadline, registers in mapping order, and sends best-effort `episode_end` only to registered seats on exit. Missing or failed seats raise `SeatsUnavailable`; no stopped episode is stored. Closing revokes credentials, reaps children, and removes descriptors.

## External Join Links

An `a2a-join:` link carries the join URL, seat, environment, expiration, and the full 256-bit one-use ticket. It is an encoded credential, not encryption. Keep it in owner-only channels; never put it in logs, launch records, the viewer, chat, or argv. Descriptors remain atomically published, owner-owned 0600 files in 0700 directories. The print helper rejects symlinks, nonregular files, unsafe permissions, oversized files, and expired descriptors.

From an owner terminal, pipe the private link directly into the agent:

```sh
uv run python scripts/print_join_link.py results/private-joins/EPISODE-seat-0.json | uv run a2a-agent --join-link
```

Alternatively, `--join-link` reads `A2A_JOIN_LINK` if set. Do not pass the link as a flag value or paste it into a shell command recorded in history. Unset stale `A2A_JOIN_LINK` when using stdin. The environment in the link selects the game strategy; admission still performs the existing signed hello/nonce challenge, protocol negotiation, ticket consumption, deadline enforcement and ready handshake. Links do not enable late joins, seat replacement, new endpoints, or weaker network policy. Public episode handles contain addresses and identifiers only.

## Follow-Ups (Not Implemented)

Room codes must be single-use, rate-limited and expire no later than the join deadline. Redemption exchanges a code for the existing full-entropy ticket; it must not substitute a short code as handshake authority.

A human bridge will be an external seat whose agent-side runtime holds the private link and answers the challenge on behalf of a person. No human runtime or UI is introduced here.
