"""PROPOSAL — not yet wired in. The shared core vocabulary for a2a-comm.

Design in one sentence: `metrics` stays a free-form per-game bag, and everything
that must be comparable moves into a typed, validated `core` field that a game
physically cannot get wrong.

Scope: exactly one `CoreMetrics` per `GameTraceBase`, i.e. per run. The run is
the unit of everything downstream — one leaderboard row, one judgment, one
rating event, one row in `to_core_df()`. Per-round detail stays in
`metrics`/`final_state`.

The polarity law, which is the whole point:

    Anything in `components`, or any key ending in `_score`, is in [0, 1] and
    higher is better. Always. No exceptions.

    Raw quantities keep their natural names, units and polarity, and live in
    `metrics` — `realized_cost`, `turns_used`, `chars_total`.

That law exists because `efficiency` currently means "joint surplus over
first-best" in buyer-seller (higher better) and "turns consumed over budget" in
word-guess (higher WORSE). A cross-game groupby on it returns a number that
looks fine and means nothing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CORE_SCHEMA_VERSION = 1

#: Tolerance for the score-consistency check. Loose enough for float summation
#: over a handful of components, tight enough to catch a real arithmetic bug.
_SCORE_TOLERANCE = 1e-6


class AgentOutcome(BaseModel):
    """Per-agent result within one run.

    WHY FIRST-CLASS: this is the join key between per-game analytics ("which
    agent did what"), OpenSkill ratings (`player_id` + `score` + `rank`), and
    the eventual cross-game leaderboard. Deriving it later from free-form
    metrics would mean re-deriving it three times, differently.

    WHY NOT PARALLEL LISTS: calendar currently stores ~20 per-agent quantities
    as lists indexed by position (`per_agent_realized_cost`,
    `per_agent_excess_burden`, ...). Any filter or reorder applied to one list
    silently misaligns it against the other nineteen, and nothing detects it.
    Typed objects make `agent_id` explicit and impossible to separate from its
    own data.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: int = Field(
        description=(
            "Engine-level index, 0..num_agents-1. WHEN: always. WHY: the stable "
            "key the engine, viewer and event stream all use to refer to this "
            "agent. HOW: the same index the game passes to its own observation "
            "builders — it must match `config.agents[agent_id]`."
        )
    )

    role: str | None = Field(
        default=None,
        description=(
            "Semantic role, e.g. 'seller', 'guesser', 'agent_a'. WHEN: whenever "
            "the game gives agents distinct, named jobs — which is most of them. "
            "WHY: games disagree on what an agent is. Calendar has interchangeable "
            "indexed agents; buyer-seller and word-guess have asymmetric roles "
            "where 'mean score across agents' is meaningless but 'mean score by "
            "role' is the actual result. Role is what a per-game analyst groups "
            "by; agent_id is what the engine joins on. HOW: a short stable "
            "lowercase string, identical across every run of the game."
        ),
    )

    player_id: str = Field(
        description=(
            "Rating identity — the thing whose skill is being estimated. WHEN: "
            "always. WHY: this is what OpenSkill pools over, so it must "
            "distinguish agents that differ in any way that should be rated "
            "separately. The same model under a redteam prompt variant vs "
            "baseline is TWO players, not one; keying on model name alone "
            "silently merges them and destroys the comparison. HOW: the agent "
            "spec's `rating_player_id` if set, else `model`, else `type` — the "
            "precedence already implemented in calendar_game.ratings._agent_model."
        )
    )

    score: float = Field(
        ge=0.0, le=1.0,
        description=(
            "This agent's contribution to the outcome, [0,1] higher better. "
            "WHEN: always. WHY: the per-agent analogue of `CoreMetrics.score`, "
            "and the quantity OpenSkill consumes. HOW: game-defined, but it must "
            "be comparable across agents WITHIN a run — that is what makes "
            "ranking meaningful. It need NOT be comparable across games. For "
            "symmetric games this is usually the agent's share of the joint "
            "outcome; for asymmetric ones, normalised achievement against that "
            "role's own best case (a seller's realised surplus over its maximum "
            "extractable surplus), because otherwise the role with the larger "
            "surplus always 'wins'."
        )
    )

    rank: int | None = Field(
        default=None,
        description=(
            "1-based finishing position within this run; ties share a rank. "
            "WHEN: set it whenever the game has a defensible ordering. WHY: "
            "OpenSkill updates on ORDERING, not magnitudes, so an explicit rank "
            "lets a game state the ordering it means rather than having the "
            "rating layer infer it by sorting `score` — which would silently "
            "invent an order for genuinely tied or non-comparable agents. HOW: "
            "leave None for cooperative games where all agents share one outcome "
            "(calendar); the rating layer then treats them as a drawn team."
        ),
    )

    messages_sent: int = Field(
        default=0, ge=0,
        description=(
            "Messages this agent emitted. WHEN: always, even if zero. WHY: this "
            "is a multi-agent COMMUNICATION framework — who talked how much is "
            "the primary scientific object, not telemetry. HOW: count events "
            "this agent authored that carry {speaker, text}; see "
            "`CoreMetrics.messages_total` for what counts as a message."
        ),
    )

    chars_sent: int = Field(
        default=0, ge=0,
        description=(
            "Total characters emitted. WHEN: always. WHY: message COUNT and "
            "message VOLUME dissociate — an agent can hit a low message cap and "
            "still flood, and 'terse but frequent' vs 'rare but verbose' are "
            "different strategies that a single counter cannot separate. HOW: "
            "sum of len(text) over the same events counted by messages_sent."
        ),
    )


class CoreMetrics(BaseModel):
    """Run-level outcome, in a vocabulary shared by every game.

    Everything here is either (a) required for reproducible per-game analytics,
    or (b) required to compare/aggregate runs. Anything game-specific belongs in
    `GameTraceBase.metrics`, which stays free-form and unbounded — calendar's
    100+ metrics do not move and nothing is taken away.
    """

    model_config = ConfigDict(extra="forbid")  # the core is a contract, not a bag

    schema_version: int = Field(
        default=CORE_SCHEMA_VERSION,
        description=(
            "WHEN: stamped automatically. WHY: a trace must self-identify its "
            "vintage independently of the sink it came from. The Firestore "
            "document carries a version but the trace does not, which is exactly "
            "what made the negotiation backfill hard — a bare trace could not "
            "say which envelope it was written under. HOW: bump on any breaking "
            "change to this model; readers branch on it."
        ),
    )

    # --- termination -------------------------------------------------------

    success: bool = Field(
        description=(
            "Did the run achieve the game's stated objective. WHEN: always. "
            "WHY: the single unambiguous filter every analyst reaches for first, "
            "and the thing 'won'/'agreement'/'coordination_rate' currently say "
            "in four incompatible ways. HOW: GAME-DEFINED, and the game must say "
            "what it means in `terminated_reason` — do NOT define it as "
            "`success_gate == 1.0`. Calendar scheduling 2 of 3 meetings is a "
            "partial success worth 0.67 whose bool-ness is a judgement only the "
            "game can make."
        )
    )

    success_gate: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Fraction of the objective completed; multiplies the composite. "
            "WHEN: always — use 1.0 for pass/fail games that succeeded, 0.0 that "
            "failed. WHY: separates DEGREE of completion from QUALITY of play, so "
            "partial credit is expressible without contaminating the quality "
            "components. HOW: calendar's `meetings_scheduled / num_meetings` is "
            "the reference; buyer-seller's `units_sold / num_items`."
        )
    )

    terminated_reason: str = Field(
        min_length=1,
        description=(
            "Why the run ended, e.g. 'deadline_reached', 'inventory_exhausted'. "
            "WHEN: always. WHY: 'score = 0' has many causes — deadline, protocol "
            "violation, API failure, agent forfeit — and they demand completely "
            "different follow-up. Without this, failure analysis starts by "
            "re-reading raw event logs. It is also where a game documents what "
            "`success` means for it. HOW: a short stable snake_case token from a "
            "small vocabulary the game defines and holds fixed across runs; "
            "prose belongs in `metrics`."
        )
    )

    # --- the single number -------------------------------------------------

    score: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Headline result: `success_gate * score_ungated`. [0,1] higher "
            "better. WHEN: always. WHY: the one number a leaderboard, a "
            "regression or a sanity check can use without knowing the game. HOW: "
            "never compute it independently — it is a derived quantity, and the "
            "validator below enforces consistency with the parts, so a game "
            "cannot report a headline that disagrees with its own components."
        )
    )

    score_ungated: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Quality of play IGNORING task completion: `Σ wᵢ·componentᵢ`. "
            "WHEN: always. WHY: gating destroys information. An agent that "
            "scheduled nothing but communicated efficiently and leaked nothing "
            "scores gated 0 — and so does an agent that was terrible at "
            "everything. Gated alone cannot tell 'nearly succeeded' from "
            "'catastrophically bad', which is the first question in any failure "
            "analysis. Statistically it also matters: gated scores are "
            "zero-inflated and behave badly in regression, so analysis usually "
            "wants ungated as the quality term plus success as a separate term — "
            "possible only if both are stored. HOW: the weighted sum, before "
            "multiplying by the gate."
        )
    )

    components: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Named sub-scores, EACH in [0,1] higher better. WHEN: whenever the "
            "headline is a composite of separable skills — which is most "
            "interesting games. WHY: the headline says how well; components say "
            "at what. They also make the score RECOMPUTABLE: because components "
            "are stored, changing the weighting never requires re-running. HOW: "
            "clip to [0,1] and INVERT anything cost-like, so 'communication "
            "cost' becomes a 'communication efficiency' component. Names are "
            "game-defined but must be stable across runs. Calendar's "
            "cost/privacy/efficiency triple is the reference."
        ),
    )

    weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Weight per component; must sum to 1.0 and cover exactly the keys of "
            "`components`. WHEN: always when components exist. WHY: the tradeoff "
            "between sub-skills is a RESEARCH CHOICE, and recording it per run is "
            "what keeps historical results interpretable after you change your "
            "mind. Reweighting ⅓/⅓/⅓ to ½/¼/¼ silently changes the meaning of "
            "every past number unless each run says what it was scored under. "
            "Because components are stored too, you rescore from disk rather "
            "than re-running. HOW: it changes the reported result, so by the "
            "reproducibility rule it is experiment-settable config and lands in "
            "the resolved config as well as here."
        ),
    )

    methods: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "How each component was actually computed, e.g. "
            "{'efficiency': 'dm_budget'}. WHEN: REQUIRED for any component whose "
            "formula is conditional on config or data availability. WHY: this "
            "guards against silently pooling incomparable numbers. Calendar's "
            "efficiency divides by `num_meetings*num_agents*dm_cap` when dm_cap "
            "is set and by `total_comm + meetings_scheduled` when it is not — "
            "same component name, completely different denominators, chosen by "
            "whether config happened to set a field. Averaging across a mixed "
            "batch without this produces a confident, meaningless number. HOW: a "
            "short stable token naming the branch taken; make it a groupby key in "
            "any analysis that aggregates that component."
        ),
    )

    # --- effort ------------------------------------------------------------

    rounds_used: int = Field(
        ge=0,
        description=(
            "Rounds/turns actually consumed. WHEN: always. WHY: time-to-outcome "
            "is a first-order result in every one of these games, and it is the "
            "denominator-free companion to the deadline. HOW: count the game's "
            "own natural round unit, not LLM calls."
        )
    )

    rounds_budget: int | None = Field(
        default=None,
        description=(
            "Maximum rounds allowed. WHEN: whenever the game has a deadline. "
            "WHY: `rounds_used` alone is uninterpretable across configs — 8 "
            "rounds is fast under a 20 budget and at the wire under 8, and only "
            "the pair distinguishes 'converged quickly' from 'ran out of time'. "
            "HOW: the config value, so the ratio is computable without re-reading "
            "config."
        ),
    )

    # --- communication: the framework's primary object ---------------------

    messages_total: int = Field(
        default=0, ge=0,
        description=(
            "Messages exchanged in the run. WHEN: always. WHY: this framework's "
            "subject IS agent-to-agent communication, so volume is a result, not "
            "instrumentation. It is also the one quantity every game here "
            "genuinely shares. HOW: count events carrying {speaker, text} — the "
            "same definition `to_messages_df()` uses, so the number always "
            "reconciles with the transcript. Calendar currently has ~8 different "
            "message counters (dms, groupchat, participant groupchat, cheap "
            "talk...) and must pick ONE total that equals its own transcript "
            "length; the breakdown stays in `metrics`."
        ),
    )

    chars_total: int = Field(
        default=0, ge=0,
        description=(
            "Characters exchanged. WHEN: always. WHY: count and volume "
            "dissociate; see AgentOutcome.chars_sent. HOW: must equal "
            "`sum(len(text))` over the same events as messages_total."
        ),
    )

    # --- per-agent ---------------------------------------------------------

    agents: list[AgentOutcome] = Field(
        default_factory=list,
        description=(
            "One entry per agent, ordered by agent_id. WHEN: always. WHY: see "
            "AgentOutcome. HOW: length must equal config.num_agents — the "
            "validator enforces it, because a short list here is the exact "
            "failure mode that made parallel per-agent lists dangerous."
        ),
    )

    # --- validators: what makes this a contract rather than a convention ---

    @model_validator(mode="after")
    def _weights_cover_components(self) -> "CoreMetrics":
        if not self.components:
            return self
        missing = set(self.components) - set(self.weights)
        extra = set(self.weights) - set(self.components)
        if missing or extra:
            raise ValueError(
                f"weights must cover exactly the components. "
                f"missing weights for {sorted(missing)}; "
                f"weights for unknown components {sorted(extra)}"
            )
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total!r}")
        return self

    @model_validator(mode="after")
    def _score_is_consistent_with_its_parts(self) -> "CoreMetrics":
        """The headline must equal what its own parts imply.

        This is the validator that earns the whole design. A game can no longer
        report a headline that disagrees with the components it published —
        which is otherwise an easy and completely invisible bug, since both
        numbers look plausible in isolation.
        """
        if self.components:
            expected_ungated = sum(
                self.weights[name] * value for name, value in self.components.items()
            )
            if abs(expected_ungated - self.score_ungated) > _SCORE_TOLERANCE:
                raise ValueError(
                    f"score_ungated={self.score_ungated!r} does not match the "
                    f"weighted sum of components ({expected_ungated!r})"
                )
        expected = self.success_gate * self.score_ungated
        if abs(expected - self.score) > _SCORE_TOLERANCE:
            raise ValueError(
                f"score={self.score!r} must equal success_gate * score_ungated "
                f"({expected!r})"
            )
        return self

    @model_validator(mode="after")
    def _agents_are_dense_and_ordered(self) -> "CoreMetrics":
        ids = [a.agent_id for a in self.agents]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ValueError(f"agents must be unique and ordered by agent_id, got {ids}")
        return self

    @model_validator(mode="after")
    def _communication_totals_reconcile(self) -> "CoreMetrics":
        """Run totals must equal the sum over agents, when agents report any.

        Catches the classic mismatch where a game counts broadcasts once at the
        run level and once per recipient at the agent level.
        """
        if not self.agents:
            return self
        agent_messages = sum(a.messages_sent for a in self.agents)
        if agent_messages and agent_messages != self.messages_total:
            raise ValueError(
                f"messages_total={self.messages_total} != sum over agents "
                f"({agent_messages}); if broadcasts are counted once per "
                f"recipient, say so in metrics rather than disagreeing here"
            )
        return self


# ---------------------------------------------------------------------------
# Provenance — the other half of "reproducible from the trace alone"
# ---------------------------------------------------------------------------


class Invocation(BaseModel):
    """How this run was launched.

    Per the layered-defence decision: sink separation (a staging SQLite file, a
    `*_staging` Firestore collection, a separate bucket) is the FIRST line — it
    makes pollution structurally impossible rather than filterable. This is the
    second line, and it records intent that no enum could: the actual command,
    so a run that turns out to be junk is traceable to what produced it.
    """

    model_config = ConfigDict(extra="allow")

    mode: Literal["live", "dry_run", "smoke_test", "replay"] = Field(
        description=(
            "WHEN: always, set by the runner not the game. WHY: a scripted-agent "
            "trace and a live-model trace are otherwise indistinguishable in the "
            "corpus, and `--smoke-test` persists real traces. HOW: the runner "
            "derives it; games must not override it."
        )
    )

    argv: list[str] = Field(
        default_factory=list,
        description=(
            "The command line, REDACTED. WHEN: always for CLI runs. WHY: captures "
            "operator intent that no enum anticipates — a shard index, an "
            "overridden sink, a one-off flag. HOW: redact the same credential-"
            "shaped tokens `manifest.redact_config` already strips, and redact "
            "the VALUE following any --*key/--*token/--*secret flag. argv is "
            "user-controlled text and routinely carries secrets."
        ),
    )

    entrypoint: str | None = Field(
        default=None,
        description=(
            "Import path of the caller when there is no argv. WHEN: library and "
            "notebook callers. WHY: argv is meaningless when the runner is driven "
            "programmatically, which is how most analysis-time replays happen; "
            "without a fallback those runs record empty provenance. HOW: e.g. "
            "'expt_runner.run_experiment.main' or the notebook path."
        ),
    )

    agents_actual: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "What actually played, versus what config declared. WHEN: always. "
            "WHY: in dry-run and smoke-test the config says 'gpt-4o-mini' while "
            "`_ScriptedSeller` really played, so the manifest currently states "
            "something false about provenance — the trace lies rather than "
            "merely omitting. HOW: record the concrete class name per agent, plus "
            "model id when an LLM genuinely served the turn."
        ),
    )
