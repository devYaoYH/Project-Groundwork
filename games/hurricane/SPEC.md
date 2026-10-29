# Hurricane landfall, release v2

Three coastal emergency managers share one episode. A hurricane with a hidden
landfall zone approaches a coastline of contiguous zones; each manager owns one
sector of zones and must decide which of them to evacuate before landfall. The
team wins by protecting the people who will be hit while not evacuating people
who will not be, and the only route to that knowledge is joint inquiry: running
instruments, disclosing what they returned through channels with different
audiences, and agreeing on a forecast.

Release v2 implements stage A of the design in `SPEC_v2.md`: the v1 storm and
instruments with a communication structure of bounded action turns, a public
bulletin, private direct messages with reply turns, live convened meetings, a
shared board, and bounded internal memory. Stages B (evolving storm, stations,
jobs, loss decomposition) and C (political dial) remain design proposals.

The design draws on uncertainty-visualization research on hurricane forecasts
(readings are ensembles, not points), on the D-Day forecasting room dramatised
in *Pressure* (scarce precise instruments, disagreeing readings, a hard
deadline), and on Kathryn Schulz's *The Really Big One* (the cost of failing to
prepare dwarfs the cost of preparing, and preparation has a lead time).

## World

A frozen scenario row fixes the coastline and the storm: `populations` (one
integer per zone, zone `0` first), the hidden `landfall_zone`, and the
`impact_radius`. Zone `z` is hit when `|z - landfall_zone| <= impact_radius`.
The bank stores `hit_zones` and `population_at_risk` as its oracle result; both
depend only on frozen item attributes. Zones are split into `num_agents`
contiguous sectors of equal size; manager `i` owns sector `i`.

Two instruments estimate the landfall zone. Each returns an **ensemble** of
`ensemble_size` candidate landfall zones drawn as
`clip(round(landfall_zone + Normal(0, sigma)), 0, num_zones - 1)`.

- `recon` (aircraft reconnaissance): `recon_sigma` (default 0.6), a team-wide
  budget of `recon_budget` flights (default 3) for the whole episode.
- `model` (forecast model run): unlimited, with sigma interpolated linearly
  from `model_sigma_initial` in round 1 (default 3.0) to `model_sigma_final` in
  the final round (default 1.0).

Ensembles are drawn from a generator seeded by `(seed, round, seat, instrument)`
so a given probe returns the same reading regardless of what else happened in
the episode. The episode `seed` changes the readings but never the storm.

## Rounds

Each storm runs `num_rounds` rounds (default 6); landfall happens after the
last round. The executable LangGraph story for one storm is
`next_round -> round -> next_round -> landfall`, where the round
child executes `begin_round -> main_pass -> (meeting)* -> reply_pass ->
close_round`; the season loop around it is described under *Seasons*. Every seat invocation is one explicit `AgentStep` whose response
is a single JSON object holding an ordered action list; the environment
validates and commits the list in order.

1. **Main pass.** Seats act in order. Each has `attention_budget` actions
   (default 3), less any `meeting_attention_cost` owed from meetings attended in
   the previous round. Actions:
   - `{"kind":"probe","instrument":"recon"|"model"}` — once per round; the
     ensemble is private to the prober until shared. Recon without budget is
     rejected; seats act in order, so the first to ask gets the flight.
   - `{"kind":"post","text","share","estimate","recommend"}` — a public
     bulletin message. `share:true` discloses every reading the speaker has not
     yet shown to everyone. `estimate` is a public landfall-zone estimate;
     `recommend` a public list of zones (any sector) to evacuate.
   - `{"kind":"dm","to","text","share","estimate","recommend"}` — a private
     message to one colleague. `share` discloses the sender's readings to the
     recipient only; estimates and recommendations are visible to the pair. The
     recipient gets a reply turn this round.
   - `{"kind":"write_board","text"}` — a durable, authored, public note
     (`board_capacity` entries).
   - `{"kind":"convene","participants","agenda"}` — opens a live meeting this
     round (at most `max_meetings_per_round`, one per convener per round).
   - `{"kind":"evacuate","zones"}` — irreversible orders in the manager's own
     sector; every zone must be unordered or the whole action is rejected. An
     order issued in round `t` is **effective** only when
     `num_rounds - t >= evacuation_lead_time` (default 2).
   - `{"kind":"pass"}` ends the turn.
   Actions beyond the budget are rejected (`attention_exhausted`).
2. **Meetings.** Each convened meeting runs as a live round-robin among its
   attendees, starting with the convener, for up to `meeting_turns` turns each
   (default 2). A meeting turn is one `{"kind":"say","text","share","estimate",
   "recommend"}` or `{"kind":"leave"}`. Meeting messages, shared readings,
   estimates and recommendations are visible to attendees only, now and in
   every later observation. The meeting ends when fewer than two attendees
   remain or the turns are exhausted. Everyone who spoke or stayed owes
   `meeting_attention_cost` actions next round and is charged
   `meeting_cost` (default 0) toward the team cost.
3. **Reply pass.** When `reply_turns` is 1, every seat that received a direct
   message this round takes one turn with a single `dm`, `post` or `pass`.
4. **Landfall** after the final round scores the episode.

Invalid actions are recorded as `invalid_action` with a reason, spend their
attention, and have no default replacement. A malformed response consumes the
turn. A failed remote invocation fails the attempt; the environment never
invents an action.

## Memory

Internal memory is agent-side. `history_window` (default unbounded) caps the
number of past turns an LLM seat carries in its provider context; the seat
harness and runtime strategy enforce it and it is recorded in the resolved
config and passed in the registration. The board is the external memory:
durable, authored, public, and costly to write.

## Seasons: iterated play with the same seats

`storms` (default `[scenario_id]`, at most 12) lists frozen bank rows played in
order by the same three seats within one episode; the executable story becomes
`next_storm -> storm -> next_storm -> finalize` with the storm child running
`begin_storm -> (round)* -> landfall -> (debrief) -> close_storm`. The scenario
fields of the config describe the first storm and later storms are looked up in
the pinned bank; every storm must cross the same number of zones so sectors are
stable. `storms` is a plain config field rather than a design parameter: a
compiled design still selects one storm, and a season is written as an episode
override. Single-storm episodes carry no season plumbing: no `storm_start`,
`storm_end`, `note` or debrief events, no `season` key in observations, and
readings identical to release v2.

Each storm starts afresh: readings, probe log, bulletin, direct messages,
meetings, estimates, recommendations, orders and the recon budget reset, and
instrument noise is keyed by `(seed, storm, round, seat, instrument)` so a
repeated scenario is a fresh storm. Attention debt from a final-round meeting
is forgiven. The board persists across storms unless `board_persists` is
false, because a board that resets every storm is not durable.

What a seat carries from one storm to the next is the ordered design parameter
`season_memory`, recorded in the resolved config and passed in the
registration like `history_window`:

| level | provider history | observation adds | who enforces |
|---|---|---|---|
| `none` | dropped at every storm boundary | `season: {storm, storms_total, memory}` | seat harness and runtime strategy |
| `outcomes` (default) | dropped | `season.record`: every past storm's landfall zone, hit zones, orders with round and effectiveness, protected/exposed/needless populations, costs and regret | environment |
| `notes` | dropped | `season.record` plus `your_notes`: the seat's own notes, one per past storm | environment and seat harness |
| `full` | kept, within `history_window` | `season.record` | nothing dropped |

The record is public world knowledge (everyone saw where the storm hit and who
ordered what), built structurally and never containing another seat's
undisclosed readings, scenario ids or titles (which name where a storm went).
Under `notes`, after a landfall that is not the last, each seat takes one
**debrief** turn seeing `landfall_result` and returning
`{"kind":"note","text"}` (at most `max_note_chars`, default 1000) or
`{"kind":"pass"}`; an empty or over-long note is `invalid_action: rejected`.
Notes are private, recorded with `visibility: private`, and shown only to their
author. The scripted manager writes a fixed note from the public result.

The ladder follows the repeated-game literature: `outcomes` is the information
structure of repeated-game studies with LLMs (moves and payoffs with a fixed
partner), `notes` is the Reflexion-style agent-written summary, `full` is the
hidden-profile release's existing convention, and `none` is the control that
separates memory from the longer prompt that memory implies. Per storm the
trace records `observation_chars_by_storm` as a context-length proxy, since
provider token counts are not visible to the environment.

Scoring: each storm's landfall result is kept in `final_state.storms`. For a
single storm the top-level result is unchanged. For a season the top-level
costs, populations, late orders and regret are sums, `hit_zones_protected_rate`
is pooled over storms, `orders` carry a `storm` key, and zone lists live only
inside each storm's result. Metrics keep their names as season totals or means
(`estimate_error` over every storm's estimates, `estimate_agreement` and
`recommendation_uptake` averaged), with `storms_completed`,
`regret_first_storm`, `regret_last_storm`, `notes_written` and
`observation_chars` added.

## Landfall and scoring

- `evacuation_cost = evacuation_cost_per_capita * sum(population of every
  ordered zone)` (effective or late).
- `casualty_cost = casualty_cost_per_capita * sum(population of hit zones with
  no effective order)`.
- `meeting_cost_total = meeting_cost * attendances`.
- `total_cost = evacuation_cost + casualty_cost + meeting_cost_total`
  (minimize); `oracle_cost = evacuation_cost_per_capita * population_at_risk`;
  `regret = total_cost - oracle_cost`.

Defaults: `evacuation_cost_per_capita = 1`, `casualty_cost_per_capita = 10`.

Further deterministic metrics: `population_exposed`, `population_protected`,
`population_needlessly_evacuated`, `hit_zones_protected_rate`, `late_orders`,
`probes_used`, `recon_used`, `model_used`; the **knowledge partition** of
readings at landfall — `readings_common` (seen by all), `readings_subset`
(seen by some), `readings_private` — with `knowledge_common_fraction` and
`disclosure_rate` (readings seen by anyone besides the prober); `messages` by
channel (`bulletin_messages`, `dm_messages`, `meeting_messages`,
`reply_messages`), `meetings_convened`, `board_writes`, `attention_used`;
`estimates_reported`, `estimate_error` (mean absolute error of each
manager's latest stated estimate in any channel), `estimate_agreement`,
`recommendations`, `recommendation_uptake` (share of ordered zones that
another manager had recommended in a channel the orderer could see),
`dialogue_words`, and `invalid_actions`. Trust, responsiveness and similar
transcript judgments are analyses, not claimed by these metrics.

## Information boundaries

A manager's observation contains: the public coastline (populations, sectors,
orders so far and whether each was effective), its own sector, the instrument
table and remaining recon budget, the public probe log (who ran what, no
readings), **its own readings** with the list of seats that can see each,
every reading disclosed to it (`known_data`), the bulletin, its own inbox and
sent direct messages, the board, every meeting's roster and agenda plus the
transcript of meetings it attended, the estimates and recommendations made in
channels it was party to, its turn kind and budget, and the turn instruction.
It never contains the landfall zone, the hit zones, another manager's
undisclosed readings, direct messages between others, or the transcript of a
meeting it did not attend. Researcher traces record private observations,
probe results and private messages with `visibility: private` and explicit
`to_agents`; this does not expose them to another seat. Registrations carry
role, seat, name and `history_window` only.

## Agents

LLM managers receive the JSON observation and return one JSON object per turn
(`{"actions":[...]}`, a bare action, or a bare list); their provider history is
agent-side and persists across rounds within `history_window`, and across
storms only under `season_memory: full`. Scripted
managers are deterministic from the observation alone: they probe every round
(recon while the budget lasts), post all their readings on the bulletin with
the rounded mean of every ensemble member they know as their estimate and the
zones within `impact_radius` of it as their recommendation, speak once in any
meeting they are invited to and then leave, ignore direct messages, and
evacuate the recommended zones in their own sector in the last effective
round. When no round is effective, they hold.

## Interpretation choices recorded here

- Seats act sequentially within every pass; recon budget is first-come.
- Sharing is all-or-nothing per message for the audience of that channel: a
  manager cannot selectively disclose a convenient subset to one audience,
  though it may choose the audience. Selective reporting is expressed in text,
  estimates and recommendations, which remain free.
- Meeting attendance is implicit for invitees; a seat that leaves before
  speaking pays no attention cost.
- An empty `recommend` list on a post, direct message or meeting turn means no
  recommendation and the message commits; a non-empty list with an unknown or
  repeated zone rejects the message. (Live Haiku runs posted `recommend: []`
  and lost the whole post, including its `share`, before this call was made.)
- Orders in the final `evacuation_lead_time` rounds are accepted and charged
  rather than rejected, because a too-late order is a real decision outcome.
- Scenario populations, radius and landfall are frozen bank rows selected by
  `scenario_id`/`item_id`; all noise, budget, cost, horizon and communication
  knobs are design parameters recorded in the resolved config.
- The season's outcome record is shown under every memory level but `none`,
  including `full`: the landfall is public, so a seat that remembers the whole
  conversation is not also made to forget what happened.
- Notes are agent-written, not harness summaries, so that what a seat chooses
  to keep is itself an observed behavior and comparable across models.
