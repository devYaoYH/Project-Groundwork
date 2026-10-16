# Hurricane landfall, release v2 — design specification

Status: design proposal for review, built in stages. **Stage A (§4–§5, the
communication layer on the v1 storm) is implemented as release v2 and
described normatively in `SPEC.md`.** Stages B and C (§2–§3, §6–§7) remain
proposals. v2 keeps the engine contracts (typed release, frozen item bank
with oracle, executable LangGraph program, `a2a-turns/2` seats, normalized
events, deterministic metrics); §11 gives the build order.

Two findings from the shipped games shaped the implementation and supersede
the wording below where they differ: (1) no shipped seat runs a
call→result→model loop inside a turn — every turn is **one model response
holding an ordered action list**, with read-only data (board, inbox, bulletin,
meeting transcripts) supplied in the observation rather than fetched by tools;
(2) bidirectional dialogue is therefore a property of the *workflow*: a
convened meeting is a live round-robin among attendees within the round, and
direct messages deliver immediately with a reply pass for recipients before the
round closes. The bulletin and board are visible to everyone immediately.

## 1. Why v2

v1 has one hidden scalar (the landfall zone), two instruments that measure it
at different precisions, and three interchangeable seats. Recon strictly
dominates, the correct inference is "average everything", and the scripted
baseline reaches zero regret. There is nothing to argue about. v2 is designed
so that four properties hold, each as a property of the *world* rather than of
agent prompts:

1. **Multiple legitimate methods.** Instruments observe different components
   of a structured, evolving storm. Dead-reckoning from a precise fix, a
   dynamical model under a regime assumption, and a statistical analog are
   each the best method under some conditions and none is best under all.
   The group's epistemic work is deciding which applies now.
2. **Political disagreement derived from position.** Seats have computable
   stakes (sector populations, evacuation costs, lead times, a team/sector
   weighting). A researcher can compute what a rational agent in each seat
   should want, so observed disagreement decomposes into an epistemic part
   (different information) and a political part (different stakes).
3. **Communication as structure.** Turns are bounded tool use. Channels are
   world objects with costs and visibility partitions (bulletin, direct
   message, convened meeting), there is a shared external memory with
   recorded reads and writes, and internal memory can be bounded. Division of
   labor is emergent and measured, not prescribed.
4. **A multi-step pipeline with distinct jobs.** Data flows every step from
   stations to modelers to decision makers. Each job has its own epistemic
   problem and adds a distinct kind of value, and the final cost decomposes
   into where it was lost: gathering, modeling, transmission, or decision.

## 2. World

### 2.1 Coast and sectors

The coast is the segment `x ∈ [0, Z)` at `y = 0`; zone `z` covers
`[z, z+1)`. Each zone carries frozen attributes:

| attribute | meaning |
|---|---|
| `population` | people at risk if hit and unprotected |
| `evacuation_cost_multiplier` | per-capita cost relative to the design-level base (poorer or denser zones cost more to move) |
| `lead_time` | rounds an effective order must precede landfall (island or single-bridge zones need longer) |
| `vulnerability` | casualty multiplier if hit unprotected (hospital, low-lying) |
| `sector` | the manager seat that owns the zone |

Zones are contiguous within a sector; sectors need not be equal in size.

### 2.2 Storm state and dynamics

State at step `t`: position `(x_t, y_t)`, heading `θ_t` (0 = straight toward
the coast, positive = turning toward higher `x`), speed `v_t`, intensity
`I_t ∈ [1, 5]`, and a hidden steering regime `r ∈ {straight, recurve}` with an
onset step `t_r` for the recurve case.

```
θ_{t+1} = θ_t + κ(r, t) + ε_θ,     κ = turn_rate if r = recurve and t ≥ t_r else 0
v_{t+1} = clip(v_t + ε_v, v_min, v_max)
I_{t+1} = clip(I_t + ε_I, 1, 5)
x_{t+1} = x_t + v_t · sin θ_t
y_{t+1} = y_t − v_t · cos θ_t
```

Landfall occurs at the first step with `y_t ≤ 0` (or at `max_steps`); the
landfall zone is `floor(x)` at crossing and the impact radius is a frozen
function of `I` at crossing (`1` below `I = 3`, `2` at or above). Zones within
the radius are hit.

The whole trajectory is generated once by a documented generator and frozen
in the item bank with the oracle result (`landfall_zone`, `hit_zones`,
`population_at_risk`, and the per-step true state used for forecast scoring).
The episode `seed` drives only observation noise and tie-breaks. This keeps
the oracle invariant of `docs/CONTRACTS.md`: nothing in the bank depends on a
design parameter.

### 2.3 Stations and instruments

Every observation is a documented function of the true state plus noise drawn
from a generator keyed by `(seed, step, source, seat)`, so a reading never
depends on the order of play.

| source | cadence and cost | returns | blind spot |
|---|---|---|---|
| **weather station** `s` at `(x_s, y_s)` | automatic every step, free, delivered to its owner only | pressure `p_env − ΔP(I)·exp(−d/L) + bias_s + ε`, wind speed `W(I)·g(d) + ε`, wind direction (cyclonic tangent + ε); `d`, bearing from station to center | local; biased or noisy stations exist and only the owner knows a station's quality class |
| **satellite fix** | automatic every step, free, public | `(x, y) + ε_sat` (coarse) | heading, speed, intensity, regime |
| **recon flight** | shared budget `recon_budget` per episode; allocation by the coordinator or a vote | exact `(x, y, θ, v, I)` | the future turn |
| **dynamical model run** (modeler tool) | one per modeler per step | an ensemble of trajectories integrated from the *modeler's submitted state estimate* under an assumed regime (`straight`, `recurve`, or `hedge` = mixture at the model's own regime prior, which the item may bias) | garbage in, garbage out: the estimate and the assumption are the modeler's |
| **statistical analog** (modeler tool) | one per modeler per step | landfall distribution from a frozen climatology table indexed by heading bin, position bin and steps-to-coast, marginal over regime at the base rate | wide; ignores today's regime evidence |
| **sounding** | cheap, one per modeler per step, limited total | a binary regime signal with accuracy `q` | position and intensity |

Design intent: early in the episode the analog is the honest forecast; a
sounding and the station pattern discriminate the regime; the dynamical run
is sharp once the regime is known; recon plus dead-reckoning wins late. A
`hedge` model run is bimodal when the regime is open, and the mean of a
bimodal ensemble points at the one place the storm will not go.

### 2.4 Default information access

| information | who receives it without any communication |
|---|---|
| satellite fix, board, bulletin, public orders, meeting rosters | everyone |
| a station's readings and quality class | its owner |
| a model run, analog or sounding result | the modeler who ran it |
| a recon fix | the seat that received the allocation |
| a sector's zone attributes and compliance state | everyone (they are public facts about the coast) |

Everything else moves only through the channels in §5, and the environment
records the recipient set of every message, so it knows at every step which
seats could have known each fact.

## 3. Seats and jobs

| job | count (first cut) | holds | tools beyond the common set | epistemic problem | distinct value |
|---|---|---|---|---|---|
| **observer** | 1 | station streams, calibration knowledge | `forward_readings`, `flag_station` | which readings are trustworthy and worth transmitting under limited attention | data quality and latency |
| **modeler** | 2, one dynamical, one statistical by expertise (both may run either tool; expertise sets the noise of the other) | runs, soundings, assimilated data | `run_model`, `run_analog`, `sounding`, `publish_forecast` | which method fits the regime; how to represent a bimodal future honestly | forecast skill per horizon |
| **manager** | 2 (3 in-process) | a sector's zones, lead times, costs, compliance | `evacuate`, `vote` | when to commit under a forecast that improves if they wait | decision timing |
| **coordinator** | 0 or 1 (otherwise managers vote) | recon budget, meeting authority | `allocate_recon`, `convene` (free) | whose question the scarce flight should answer | allocation |

Capabilities deliberately overlap: a manager can read raw station data posted
to the board and do back-of-envelope inference; modelers can disagree on
method; the observer can choose what to forward. "Trust the modeler" versus "I
can see the pressure dropping in my sector" is a legitimate disagreement, not
a role violation. The containerized runtime admits five non-human seats, so
the default roster is observer, two modelers, two managers, with recon
allocated by vote.

## 4. Rounds and turns

Each round is one storm step.

1. **Advance.** The environment steps the storm (from the frozen trajectory),
   delivers station readings to owners and the satellite fix to everyone, and
   delivers bulletin/DM/board writes from the previous round.
2. **Turns**, in seat order (observer, modelers, managers, coordinator). A turn
   is bounded tool use over `a2a-turns/2`: up to `attention_budget` calls
   (default 3). Reads (`read_board`, `read_inbox`, `get_observation`) are
   free and resolve immediately; writes, forwards, runs and soundings each
   spend one call; `evacuate` and `pass` end the turn. Unused calls are lost.
3. **Meetings.** `convene(seats)` in a turn opens a meeting channel for this
   round. Invited seats accept or decline on their own turn; attendees' remaining
   calls this round can only be spent inside the meeting (`say` to the meeting,
   `read`), so a meeting round is a round they did not probe or publish. A
   meeting may also carry a design-level money cost. Meeting messages are
   visible to attendees only, forever.
4. **Decisions.** `evacuate(zones)` is irreversible, charged immediately, and
   protects a zone only if issued at least `lead_time(zone)` rounds before
   landfall. `vote(seat)` allocates the next recon flight by plurality among
   managers when there is no coordinator.
5. **Landfall** ends the episode and scores it.

Invalid calls are rejected with a code, spend the call, and are recorded;
there is no default replacement, and a failed remote invocation fails the
attempt (v1 policy).

## 5. Communication and memory

| channel | cost | visibility | delivery |
|---|---|---|---|
| **bulletin** | one call | everyone | next round |
| **direct message** | one call | sender and recipient | next round |
| **meeting** | attendees' remaining calls this round (+ optional money cost) | attendees | immediate within the round |
| **board** (external memory) | one call per write; reads free | everyone; entries are authored and timestamped; reads are logged | immediate |

Internal memory is agent-side and persists across rounds, as in v1. v2 adds
the design parameter `history_window`: the number of past turns a seat's
harness keeps in its own context. Unbounded history makes the board
decorative; a tight window makes it the team's only durable record and forces
choices about what is worth writing down. The parameter is agent-side
behavior, so it is recorded in the resolved config and enforced by the seat
harness and runtime strategy, not by the environment.

### 5.1 Computation as a tool

The instruments of §2.3 are fixed estimators. v2 also lets a seat **write and
evaluate code** against the data it can see, so that "different legitimate
inference mechanisms" are literal: a modeler can write its own assimilator, a
manager can fit a quick regression of pressure on distance, an observer can
script a calibration check. `run_analysis(code)` executes the seat's code in
an environment-owned sandbox with that seat's current information set bound
as a JSON object (`data`), returns stdout and a structured result, and spends
one call. The sandbox is a subprocess with no network, a CPU/time/memory
cap, a fixed import allow-list (standard library plus `numpy`), and no
filesystem beyond a scratch directory; it is the same shape as the
environment's other tools and never sees anything outside the seat's access
set, so the information boundaries of §8 are unchanged.

Why environment-owned rather than agent-side: the code is then a recorded,
inspectable artifact of the inquiry. A seat may post a script to the board or
send it in a meeting, which makes **method sharing** a third level of
disclosure alongside sharing data and sharing conclusions; another seat can
run a colleague's script on its own data. Measures: fraction of forecasts
backed by an analysis, method reuse across seats, and the skill gap between a
seat's scripted estimator and the reference rule on the same information
set. Agent-side execution (the seat runtime running code privately) remains
possible for harnesses that want it, but produces no trace and no sharing.

Every message event carries `speaker`, `text`, `channel`, `to_agents` and
`round`; the viewer already renders recipients. Observation builders filter
meeting and DM traffic by membership exactly as v1 filters readings, and the
same test pattern (serialize a seat's observation, assert the absent secret)
covers them.

## 6. Scoring and the political dial

Per zone: evacuation cost `= base_evacuation_cost · multiplier · population`
for any ordered zone; casualty cost `= casualty_cost · vulnerability ·
population` for a hit zone without an effective order. Sector cost is the sum
over the sector; team cost is the sum over sectors.

Each seat's utility is `−(λ · team_cost + (1 − λ) · own_sector_cost)` with
`λ ∈ [0, 1]` a design parameter (`sector_cost` for observer, modelers and
coordinator is the team cost, so `λ` only separates managers). `λ = 1` is the
fully cooperative v1 game; `λ = 0` is pure parochialism; `0.5` is politics.
Because it is declared, "how much misalignment" is an experimental axis.

Deferred to v3 but designed for: a **season** of several storms in one
episode, where a needless evacuation lowers that sector's `compliance`, which
scales how much of a later order's population is actually protected. This
makes reluctance under uncertainty a legitimate defense of future credibility
and makes a colleague's "just evacuate everything" an expenditure of someone
else's political capital. A **regional declaration** vote (unlocks shelters
or is required for shared-road sectors) is the second v3 item.

## 7. Measures

All measures are deterministic functions of the recorded trace. Transcript
judgments (trust, humility, responsiveness) remain analyses on top of them.

### 7.1 Outcome

`total_cost`, `evacuation_cost`, `casualty_cost`, `oracle_cost` (evacuating
exactly the hit zones at the last effective step), `regret`,
`population_exposed`, `population_protected`, `population_needlessly_evacuated`,
`late_orders`, per-sector costs, and each seat's realized utility at the
episode's `λ`.

### 7.2 Per-job scorecards

- **observer**: `data_latency` (mean rounds between a reading's arrival and
  its first forwarding or board post), `forwarded_fraction`, and
  `flag_precision` (flagged stations that were in fact biased).
- **modeler**: `forecast_skill[h]` — CRPS of each published landfall ensemble
  against the frozen truth, by horizon `h` (steps to landfall), and
  `regime_belief_accuracy[t]` when a regime claim is published.
- **manager**: `decision_timing` (rounds between the first step at which the
  reference decision rule on the manager's own information set would have
  ordered a zone and the actual order), `late_orders`, `vote_alignment` (did
  recon votes track own-sector stake or team stake).
- **coordinator**: `allocation_value` (reduction in reference-forecast CRPS
  attributable to each recon flight versus allocating it to the next-best
  seat).

### 7.3 Process

- **knowledge partition**: per round, the fraction of gathered evidence that
  is common knowledge, held by a strict subset, or private.
- **disclosure** (v1 metric generalized): readings and runs that reached any
  channel, by channel.
- **information loss in disclosures**: for each published summary of an
  ensemble, the divergence between the summary and the full ensemble it
  summarized (a mean posted for a bimodal run scores badly).
- **meeting structure**: convene counts, rosters, repeated dyads, seats never
  invited; board authorship concentration; board read coverage (who read what
  before deciding).
- **recommendation uptake** and **estimate agreement** as in v1, scoped to
  the channel through which the recommendation arrived.
- `messages`, `dialogue_words`, `invalid_actions`.

### 7.4 Loss decomposition

Let `D*(I, stake)` be the environment's **reference decision rule**: a
documented, versioned assimilator (particle filter over the dynamics of §2.2
with the frozen climatology as prior) applied to an information set `I`,
followed by the expected-cost-minimizing evacuation plan for the given stake.
`D*` is part of the release (`oracle_version`), never shown to seats, and is
what makes the counterfactuals computable. With

- `I_all(t)`: every observation anyone gathered up to `t`,
- `I_m(t)`: what manager `m` actually had access to (own data plus everything
  delivered to it through any channel),

the realized team cost decomposes as

```
gathering loss     = cost(D*(I_all)) − oracle_cost
transmission loss  = Σ_m [ cost_m(D*(I_m)) − cost_m(D*(I_all)) ]
decision loss      = Σ_m [ cost_m(actual_m) − cost_m(D*(I_m)) ]
```

and modeling quality is reported beside it as the **skill gap** of each
published forecast: `CRPS(published) − CRPS(D*'s forecast on that modeler's
own information set)`. A silent all-knowing observer shows up as transmission
loss; a brilliant modeler nobody convened shows up as transmission loss on the
managers' side; a team that meets constantly but never probes shows up as
gathering loss; a manager who had the forecast and waited shows up as
decision loss.

### 7.5 Disagreement decomposition

For managers `i` and `j` at step `t`, the difference between their
recommended plans decomposes as

```
epistemic  = D*(I_i, stake_i) − D*(I_j, stake_i)    # same stake, different information
political  = D*(I_j, stake_i) − D*(I_j, stake_j)    # same information, different stake
residual   = actual_i − actual_j − epistemic − political
```

so a researcher can say how much of a dispute was about the storm, how much
about position, and how much about neither.

## 8. Information boundaries

A seat's observation never contains: the true state or trajectory, the
regime or onset step, the hit zones, another seat's station readings unless
forwarded, another modeler's runs unless published, meeting traffic it did
not attend, direct messages between others, or any output of `D*`. Researcher
traces record private observations, readings and meeting traffic with
`visibility: private` or the recipient set; this never exposes them to
another seat. Registrations carry role, seat and name only.

## 9. Scripted baselines

Deterministic from the observation alone, and shipped as contrasts rather
than a single "baseline strategy":

| baseline | behavior | what it demonstrates |
|---|---|---|
| **assimilator** | runs the reference filter on its own information set, publishes full ensembles, forwards everything | the best achievable play per information set |
| **mean-pooler** | averages every ensemble member it knows, publishes the mean | fails on bimodal runs; shows information loss |
| **extrapolator** | dead-reckons from the latest fix, ignores regime evidence | wins late, loses to recurves |
| **analog-only** | trusts climatology, never updates on soundings | calibrated but wide; late decisions |
| **hoarder** | gathers but never forwards or publishes | isolates transmission loss |
| **parochial manager** | plays `λ = 0` regardless of the episode's `λ` | isolates political disagreement |

Tests pair these against frozen items to assert the loss decomposition
attributes each failure to the right stage, and that every boundary in §8
holds for every roster.

## 10. Frozen item attributes versus design parameters

| frozen in the bank (oracle-relevant) | design parameters (resolved config) |
|---|---|
| coast: zone populations, multipliers, lead times, vulnerability, sectors | `base_evacuation_cost`, `casualty_cost`, `λ` |
| storm trajectory, regime, onset, per-step truth; climatology table | observation noise scales, station quality assignment seed |
| station positions and quality classes; model regime-prior bias | `recon_budget`, `attention_budget`, `meeting_cost`, `history_window`, `max_steps`, roster and expertise assignment |
| oracle: landfall zone, hit zones, population at risk | reference rule version is pinned in the release, not a knob |

## 11. Build staging

Each stage is independently testable and leaves the game runnable.

**Stage A — communication layer on the v1 world.** Replace the v1 phase
actions with bounded tool turns (common tool set, bulletin, DM, meeting,
board, `history_window`). Add the knowledge-partition, meeting and board
measures. Tests: membership boundaries, meeting cost, call budgets, recipient
recording, partition arithmetic. The v1 storm stays, so outcome tests carry
over.

**Stage B — dynamical world, stations and jobs.** Introduce the storm of
§2.2, the station and instrument models of §2.3, the trajectory generator and
climatology table with a regenerated bank, the four jobs, the reference
assimilator, forecast scoring, and the loss decomposition. Tests: generator
determinism and oracle freezing, observation models, bimodality of hedged
runs, baseline contrasts of §9, decomposition identities (the three losses sum
to `total_cost − oracle_cost` on every trace).

**Stage C — political dial.** `λ`, heterogeneous sector profiles, recon votes,
the disagreement decomposition, and the parochial baseline. Then the v3 items:
seasons with compliance, and the regional declaration.

## 12. Open questions for collaborators

1. Regime as binary (`straight`/`recurve`, crisp discriminating experiments)
   or continuous (turn angle, more realistic)? Recommendation: binary first.
2. Seat-specific instrument access (forces interdependence) or everyone may
   run everything (cleaner method comparison)? Recommendation: seat-specific.
3. `λ` as a per-episode factor or per-seat (some managers parochial, some
   not, hidden-role flavor)? Recommendation: per-episode first.
4. Should seats be allowed to publish distributions rather than point
   estimates so calibration can be scored directly? Recommendation: yes, as
   ensembles or zone histograms.
5. Is the reference assimilator acceptable as the counterfactual decision
   rule, given that it is itself a modeling choice? The alternative is to
   report the decomposition under two reference rules and treat the
   difference as sensitivity.
6. Human seats: the platform has no human-seat runtime yet; the tool-turn
   contract of §4 is the one a human bridge would implement.
