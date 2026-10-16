# Hurricane landfall, release v1

Three coastal emergency managers share one episode. A hurricane with a hidden
landfall zone approaches a coastline of contiguous zones; each manager owns one
sector of zones and must decide which of them to evacuate before landfall. The
team wins by protecting the people who will be hit while not evacuating people
who will not be, and the only route to that knowledge is joint inquiry: running
instruments, disclosing what they returned, and agreeing on a forecast.

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
the episode. The episode `seed` therefore changes the readings but never the
storm; the storm is item-selected.

## Rounds

The episode runs `num_rounds` rounds (default 6); landfall happens after the
last round. The executable LangGraph story is
`next_round -> round -> next_round -> landfall -> finalize`, where the round
child executes `begin_round -> experiment -> discussion -> decision ->
close_round`. Each phase child is a bounded round-robin loop with one explicit
`AgentStep` per manager turn, in seat order.

1. **Experiment.** Each manager either runs one instrument
   (`{"kind":"probe","instrument":"recon"|"model"}`) or passes. Recon with no
   remaining budget is invalid. Seats act in order, so the first seat to ask
   gets the flight. The *fact* that a manager probed and which instrument is
   public; the ensemble is private to the prober.
2. **Discussion.** `discussion_cycles` cycles (default 1) give every manager one
   `{"kind":"say", "text", "share", "estimate", "recommend"}` or
   `{"kind":"pass"}`. `share: true` discloses every one of the speaker's
   not-yet-shared readings to the whole group as structured data. `estimate` is
   a public landfall-zone estimate. `recommend` is a public list of zones (any
   sector) the speaker thinks should be evacuated. Text is free-form and capped
   at `max_message_chars`.
3. **Decision.** Each manager issues `{"kind":"evacuate","zones":[...]}` or
   `{"kind":"hold"}`. Every zone must lie in the manager's own sector and not
   already be under an order; otherwise the whole action is invalid and no order
   is recorded. Orders are irreversible. An order issued in round `t` is
   **effective** only when `num_rounds - t >= evacuation_lead_time` (default 2):
   later orders still cost the evacuation but protect nobody.

Invalid actions consume the turn, are recorded as `invalid_action`, and have no
default replacement. A failed remote invocation fails the attempt; the
environment never invents an action.

## Landfall and scoring

After the final round the storm makes landfall. One shared team score:

- `evacuation_cost = evacuation_cost_per_capita * sum(population of every
  ordered zone)` (effective or late).
- `casualty_cost = casualty_cost_per_capita * sum(population of hit zones with
  no effective order)`.
- `total_cost = evacuation_cost + casualty_cost` (minimize).
- `oracle_cost = evacuation_cost_per_capita * population_at_risk` is the cost of
  evacuating exactly the hit zones in time; `regret = total_cost - oracle_cost`.

Defaults are `evacuation_cost_per_capita = 1` and
`casualty_cost_per_capita = 10`.

Further deterministic metrics: `population_exposed`, `population_protected`,
`population_needlessly_evacuated`, `hit_zones_protected_rate`, `late_orders`,
`probes_used`, `recon_used`, `model_used`, `disclosure_rate` (readings shared /
readings taken), `estimates_reported`, `estimate_error` (mean absolute error of
each manager's latest public estimate), `estimate_agreement` (share of
estimating managers who agree with the modal estimate),
`recommendation_uptake` (share of ordered zones that another manager had
recommended earlier), `messages`, `dialogue_words`, and `invalid_actions`.
These structured fields exist so that disclosure, influence, and agreement are
measured from recorded actions rather than inferred from prose. Trust,
responsiveness and similar transcript judgments are analyses, not claimed by
these metrics.

## Information boundaries

A manager's observation contains: the public coastline (populations, sectors,
orders so far, and whether each order was effective), its own sector, the
instrument table and remaining recon budget, the public probe log (who ran what,
no readings), **its own readings**, every reading any manager has shared, public
estimates and recommendations, the public discussion, phase, remaining turns
and the phase instruction. It never contains the landfall zone, the hit zones,
or another manager's unshared readings. Researcher traces record private
observations and probe results with `visibility: private`; this does not expose
them to another seat. Registrations carry role, seat and name only.

## Agents

LLM managers receive the JSON observation and return one JSON object per turn;
their provider history is agent-side and persists across rounds. Scripted
managers (used for smoke, tests and browser fixtures) are deterministic from the
observation alone: they run recon while the budget lasts and otherwise a model
run, always share their readings, estimate the landfall zone as the rounded
mean of every ensemble member they know (own readings plus shared data),
recommend the zones within `impact_radius` of that estimate, and evacuate the
recommended zones in their own sector in the last effective round. When no
round is effective (`evacuation_lead_time >= num_rounds`), they hold.

## Interpretation choices recorded here

- Seats act sequentially within every phase; recon budget is first-come.
- Sharing is all-or-nothing per turn and covers every unshared reading, so a
  manager cannot selectively disclose a convenient subset; selective reporting
  is expressed in text and estimates, which remain free.
- Orders in the final `evacuation_lead_time` rounds are accepted and charged
  rather than rejected, because a too-late order is a real decision outcome.
- Scenario populations, radius, and landfall are frozen bank rows selected by
  `scenario_id`/`item_id`; all noise, budget, cost and horizon knobs are design
  parameters recorded in the resolved config.
