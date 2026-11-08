# Repeated hidden profiles, release v1

One episode contains four stable participants and at least two decision rounds.
The same agent objects persist across rounds; what each agent carries from one
round to the next is the design parameter `round_memory` (below). New task
facts replace previous facts for scoring. This paradigm uses a four-person
group, not a director/matcher dyad.

Each round presents three alternatives (A/B/C) and one private packet per seat.
Facts carry a natural-language statement, candidate and signed additive value.
Each distinct verified fact contributes once to the union. Repeating shared
facts never increases their weight. Ties favor A, then B, then C.

The executable LangGraph story is `next_round -> round -> next_round -> finalize`.
The actual round child executes `receive_packets -> pre_report -> discussion ->
post_report -> commitment -> score_round -> (debrief)`. Each phase is an
executable child with one explicit AgentStep in a bounded round-robin loop; the
debrief runs only under `round_memory: notes` and only before another round.

## Cross-round memory

`round_memory` is an ordered design parameter recorded in the resolved config
and passed in the registration, with the same levels as the hurricane game's
`season_memory`:

| level | provider history | observation adds | who enforces |
|---|---|---|---|
| `none` | dropped when a new round begins | `memory` | seat harness and runtime strategy |
| `outcomes` | dropped | `record`: each finished round's committed choice (or null), its proposer and how commitment ended | environment |
| `notes` | dropped | `record` plus `your_notes`, the seat's own notes, one per past round | environment and seat harness |
| `full` (default) | kept whole, the release v1 convention | `record` | nothing dropped |

The record holds only what every participant already witnessed during
commitment; whether the choice was optimal is oracle data and never enters an
observation, so no level feeds correctness back to the group (human repeated
hidden-profile studies give none either). Under `notes`, after `score_round`
each participant takes one debrief turn seeing `last_round_result` and
returning `{"kind":"note","text"}` (at most `max_note_chars`, default 1000) or
`{"kind":"pass"}`; an empty or over-long note is an invalid action. Notes are
private, recorded with `visibility: private`, and shown only to their author.
The scripted participant writes a fixed note from the public result. Episodes
recorded before this parameter existed behaved as `full` without the `memory`
and `record` observation keys; `round_end` events now also carry `proposer`.

1. Every participant returns a sealed initial choice and estimated A/B/C scores.
2. Six discussion cycles by default give each participant one free say/pass
   action per cycle. There is no mandatory evidence ledger or disclosure rule.
3. Every participant returns a sealed postdiscussion choice and score estimate.
4. Three commitment cycles by default allow propose/endorse/pass. A new proposal
   clears endorsements. Proposal alone is not endorsement: every participant,
   including its proposer, must explicitly endorse the active candidate.
5. Four distinct endorsements commit. Exhaustion records no consensus. There is
   no majority vote or scripted/oracle fallback. Invalid actions consume a turn
   and are recorded; a failed remote invocation fails the attempt.

Speaker labels honor configured participant identities and otherwise use
`participant_0` through `participant_3`. Sealed reports include a private
researcher-facing text summary; this never enters peer discussion/history.

Private observations contain only the seat's packet, current-round public
messages, phase, limits, and public commitment state. Canonical fact IDs,
allocation metadata, pooled/local oracle rankings, other packets, and other
sealed reports are absent. Prior permitted dialogue and each agent's own sealed
reports remain in its agent-side context. Researcher traces record private
observations and reports with `visibility: private`; this does not expose them
to another agent. Registrations carry role/seat only, never packets.

The pinned bank freezes hidden caucus and relief rows plus matched full-information
controls for both. Hidden and full sequences retain the same allocation condition
through every round. Caucus local scores are (6,5,1), union (6,11,4), and relief local
scores (6,5,3), union (0,8,9). Oracle validation checks pooled and packet rankings
against the same frozen evidence/allocation. Bank rows specify an iterated
round sequence selected by scalar `sequence_id`. `condition` is frozen item
metadata, not a runtime override. Original bank snapshots remain under
`hidden_profile/items/historical/` for existing live traces.

Primary deterministic metrics are optimal-choice rate, no-consensus count,
decision regret over committed rounds, private preference revisions, score
absolute error, message/word counts, and invalid actions. Missing reports are
recorded separately in each round; absent reports are never counted as correct.
A no-consensus round has null regret, not zero individual regret. Aggregate
regret is the sum over committed rounds and must be interpreted with the
no-consensus count. Mention, acknowledgment and integration are transcript
analyses, not claimed by these deterministic metrics.

All behavior knobs (round sequence, discussion/commitment budgets, cross-round
memory and note length, message length and remote turn deadline) are resolved
config fields. Text inputs are
sufficient; exact OpenRouter DeepSeek V4.1 Flash capability declarations are
retained in the live example despite this game's text-only requirement.
