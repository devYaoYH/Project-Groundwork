"""Prompt templates for the LLM judge pipeline."""

import hashlib
from negotiation_judge.schema import CheapTalkTurn, JudgeGameContext, RoundData, RoundOutcome

# The complete text is derived from `_golden_context`; retaining its digest
# keeps the check compact while still pinning every byte of both messages.
_GOLDEN_RENDER_SHA256 = "85c887f1f2284c39c646cf49d80b9b4abb1b40ca23c81ebab52454a7f32b1729"


def _golden_context() -> JudgeGameContext:
    """Return a small, deterministic context covering every prompt branch.

    A golden prompt must be buildable offline. Depending on a live-LLM trace
    made judge startup impossible in a clean checkout, so this fixture is a
    typed synthetic environment rather than an external corpus artifact.
    """
    return JudgeGameContext(
        episode_uid="golden-environment",
        model_a="model-a",
        model_b="model-b",
        mode="shifting",
        shifting_agent="agent_b",
        mc_ratio=0.8,
        oracle_optimum=24.0,
        optimal_allocation="agent_a: alpha; agent_b: beta",
        rounds=[RoundData(
            round_number=1,
            round_outcome=RoundOutcome.suboptimal,
            joint_efficiency=0.5,
            reward_a=6.0,
            reward_b=6.0,
            cheap_talk=[CheapTalkTurn(
                speaker="agent_a", turn=1, thinking="I can take alpha.",
                speech="I will take alpha.",
            )],
            allocation_a={"alpha": 2},
            allocation_b={"beta": 1},
            auto_allocated_b=True,
        )],
    )


def _render_golden() -> str:
    """Render the full judge prompt from the deterministic golden context."""
    ctx = _golden_context()
    messages = build_judge_messages(ctx)
    parts = [f"=== role: {msg['role']} ===\n{msg['content']}" for msg in messages]
    return "\n\n".join(parts)


def get_prompt_version() -> str:
    """Return a stable content hash of the checked-in golden prompt contract.

    Checks that the rendered judge prompt matches the pinned digest. This
    catches changes to prompts.py, extractor.py, schema.py, or any other code
    that shapes the judge prompt — not just edits to this file.

    If the golden is stale, raises RuntimeError with instructions to regenerate:
        update ``_GOLDEN_RENDER_SHA256`` from ``_render_golden`` and commit it.

    The content hash works from source archives and installed wheels, unlike a
    git-log based version. It also changes whenever the rendered prompt does.
    """
    current = _render_golden()
    digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if digest != _GOLDEN_RENDER_SHA256:
        raise RuntimeError(
            "Judge prompt output has changed. Regenerate "
            "_GOLDEN_RENDER_SHA256 from negotiation_judge.prompts._render_golden and commit it."
        )
    return digest[:12]

# --- Phase 1: Whole-environment pattern discovery (one call per environment, output per round) ---

JUDGE_SYSTEM_PROMPT = """\
You are an expert analyst of multi-agent negotiation dialogues.
You will analyze ALL rounds of a single resource negotiation environment and
identify the small number of key coordination patterns — generalizable
behavioral classes — that best explain why each round succeeded or failed.

## Environment Conditions

Every environment runs in one of two partner-stability conditions (see the `mode`
field in the environment context):

- **stable** — Both agents keep their full conversation history across all
  rounds. Look for cumulative trust-building, path dependence, and
  precedent-anchoring effects.

- **shifting** — One agent retains context (stable role) while the other's
  context is reset each round (shifting role). The shifting agent meets a
  "fresh" partner each round. Cross-round learning only accrues on the
  stable side; do not attribute "learning" to the shifting agent.

In stable mode, a second-round failure after a first-round success is often
a regression or defection. In shifting mode, the same surface pattern may
simply reflect a fresh partner who never saw the prior agreement.

## Agent Incentive Structure (CRITICAL)

Each agent is instructed to **maximize their own individual reward**, not joint
welfare. Self-interested behavior — an agent securing the allocation that
maximizes their personal score even at the expense of joint efficiency — is
**rational and expected**, not a failure mode.

Do NOT label individually-rational choices as `status_quo_bias`,
`self-interest`, or failure patterns simply because the joint outcome is
suboptimal. A suboptimal joint outcome is only a coordination failure if:
1. A Pareto-improving alternative existed that both agents could have agreed to,
   AND
2. The agents had enough information and opportunity to reach it.

Use the oracle stats to determine whether a better joint outcome was achievable
given the agents' constraints. If each agent was already maximizing their own
reward given what the other chose, the outcome is a Nash equilibrium — not a
bias or failure.

## What You See

- Each agent's private thinking trace (if available)
- Each agent's public speech messages
- The final resource allocations submitted
- The round outcome and oracle ground truth for every round

## Pattern Naming Rules (CRITICAL)

Your patterns must be **generalizable behavioral classes**, not narrations of
specific environment events. Ask yourself: "Would this same class of behavior appear
in a completely different multi-agent coordination setting (collaborative
coding, scheduling, planning) with different objects?" If no, it is too
instance-specific — abstract it up.

**Good pattern names** (generalizable coordination mechanisms):
- `contested_constraint_anchoring` — agents coordinate around the scarcest shared dependency first
- `precedent_locking` — a prior agreement is treated as binding without renegotiating
- `stated_action_divergence` — agent's final action contradicts their stated plan
- `complementary_specialization` — agents voluntarily split into non-overlapping scopes
- `risk_averse_underclaim` — agent deliberately claims less than agreed to avoid joint failure

**Bad pattern names** (too instance-specific or narrative):
- "Agent B firmly insists on 6-obsidian/4-magma split" ← describes one event
- "victory lap confirmation bias" ← journalistic framing, not a mechanism
- "protocol固化" ← mixed language, same as precedent_locking
- "established_routine_exploitation in the brass/copper-aether split" ← names specific objects
- "resource_optimization_under_supply" ← names a task-type artifact; rename to `differentiated_strategy_exploration`
- "titanium_hoarding" ← names a specific object; rename to `contested_constraint_monopolization`

**The domain-vocabulary test**: if your pattern name contains any object name,
task artifact, or quantity specific to this setting, it fails. Strip those out
and name the underlying decision-making behavior instead. Forbidden tokens in
pattern names include: `resource`, `allocation`, `supply`, `budget`,
`annulment`, `overdraw`, `bid`, `auction`, `pool`, `bottleneck`, `project`,
`reward`, `recipe`, `inventory`, `negotiate`, `negotiation`. Ask: "What is the
agent *doing* strategically?" not "What did they buy?"

## How Many Patterns Per Round

Identify the **1–2 most important** patterns per round. Do not list every
observable behavior. Choose only the patterns that most directly explain
the round outcome (success, failure, or overdraw). If multiple behaviors
are variants of the same mechanism, merge them into one pattern with a
broader description.

## Cross-Round Name Consistency (CRITICAL)

You are analyzing ALL rounds at once. **Reuse the exact same `name` when the
same mechanism recurs across rounds.** If you named a pattern
`resource_monopolization` in Round 2 because Agent B locked up the obsidian
supply, and Agent B does the same thing in Round 3, call it
`resource_monopolization` again — not `obsidian_control` or
`supply_concentration`. Consistent naming is what makes the taxonomy useful.

Only introduce a new name if the mechanism is genuinely different from anything
you have already named in an earlier round of this environment.

## Assessment Per Pattern

For each pattern:
- **effectiveness**: did it help or hurt coordination? (positive/negative/neutral)
- **intent**: cooperative / self-interested / ambiguous (infer from thinking trace if available)
- **speech_allocation_coherent**: did the agent's final submission match their stated plan?

Ground your attribution in the oracle stats: compare what was achieved
against the theoretical optimum to determine whether a pattern was
actually effective or just felt effective.

Respond ONLY with a valid JSON object matching the schema below. No markdown fences,
no preamble, no trailing text.

## Output Schema

Pattern `name` must be a short snake_case label (2–4 words) naming a
generalizable behavioral class, e.g. `bottleneck_anchoring`,
`precedent_locking`, `speech_allocation_divergence`.
Pattern `description` explains how the class manifested **in this round**
(1–2 sentences, grounded in evidence).

{
  "episode_uid": "string",
  "model_a": "string",
  "model_b": "string",
  "mode": "stable | shifting",
  "mc_ratio": float | null,
  "rounds": [
    {
      "episode_uid": "string",
      "round_number": int,
      "model_a": "string",
      "model_b": "string",
      "mode": "stable | shifting",
      "mc_ratio": float | null,
      "round_outcome": "optimal | suboptimal | overdrawn",
      "joint_efficiency": float,
      "patterns": [
        {
          "name": "free-text label you invent",
          "description": "what the pattern is and how it manifested",
          "evidence": [
            {"speaker": "agent_a | agent_b", "type": "thinking | speech | allocation", "quote": "..."}
          ],
          "effectiveness": "positive | negative | neutral",
          "intent": "cooperative | self-interested | ambiguous",
          "speech_allocation_coherent": true | false,
          "coherence_note": "optional — explain any gap between speech and allocation"
        }
      ],
      "round_attribution": "1-2 sentence explanation of why this round succeeded or failed",
      "prior_round_influence": "optional — did prior rounds shape this round? e.g., 'repaired overdraw from R1', 'learned from R2 failure', 'regressed despite R1 success'"
    }
  ],
  "game_attribution": "2-3 sentence narrative of the whole environment: arc, key turning points, overall coordination quality"
}
"""


def _format_transcript(turns: list) -> str:
    """Format cheap talk turns into a readable transcript block."""
    lines = []
    for turn in turns:
        label = "turn_decision" if turn.is_decision else f"turn_{turn.turn}"
        if turn.thinking:
            lines.append(f"[{turn.speaker} {label} thinking (private)]: {turn.thinking}")
        if turn.speech:
            lines.append(f"[{turn.speaker} {label} speech (public)]: {turn.speech}")
    return "\n".join(lines) if lines else "(no cheap talk this round)"


def _format_allocation(alloc: dict[str, int]) -> str:
    if not alloc:
        return "(none)"
    return ", ".join(f"{res}: {qty}" for res, qty in sorted(alloc.items()))


def _format_round_block(rnd: RoundData) -> str:
    """Format a single round into the user prompt."""
    auto_note = ""
    if rnd.auto_allocated_a or rnd.auto_allocated_b:
        agents = []
        if rnd.auto_allocated_a:
            agents.append("Agent A")
        if rnd.auto_allocated_b:
            agents.append("Agent B")
        auto_note = (
            f"\n⚠ Note: {' and '.join(agents)} had auto-allocated projects "
            "(the engine assigned projects greedily, not the agent's strategic choice).\n"
        )

    return f"""\
## Round {rnd.round_number}
- Outcome: {rnd.round_outcome.value}
- Joint efficiency: {rnd.joint_efficiency:.0%}
- Agent A reward: {rnd.reward_a}, Agent B reward: {rnd.reward_b}
{auto_note}
### Cheap Talk Transcript (Round {rnd.round_number})
{_format_transcript(rnd.cheap_talk)}

### Final Allocations Submitted (Round {rnd.round_number})
- Agent A: {_format_allocation(rnd.allocation_a)}
- Agent B: {_format_allocation(rnd.allocation_b)}
"""


def _shifting_agent_note(ctx: JudgeGameContext) -> str:
    """Build a one-line callout about which agent has a reset context."""
    if ctx.mode != "shifting":
        return ""
    if ctx.shifting_agent == "agent_a":
        stable = "Agent B"
        shifting = "Agent A"
    elif ctx.shifting_agent == "agent_b":
        stable = "Agent A"
        shifting = "Agent B"
    else:
        # Shouldn't happen in practice, but be safe
        return "\n- Shifting role: unknown (one agent's context resets each round)"

    return (
        f"\n- Shifting role: **{shifting}** has its context reset each round "
        f"(meets a 'fresh' partner each round). **{stable}** retains full history. "
        f"Cross-round learning only accrues on the {stable} side."
    )


def build_judge_user_prompt(ctx: JudgeGameContext) -> str:
    """Build the user prompt for a whole-environment judgment."""
    rounds_text = "\n\n---\n\n".join(_format_round_block(r) for r in ctx.rounds)

    return f"""\
## Environment Context
- Environment ID: {ctx.episode_uid}
- Models: Agent A = {ctx.model_a}, Agent B = {ctx.model_b}
- Partner mode: {ctx.mode}{_shifting_agent_note(ctx)}
- Goal compatibility (M/C ratio): {ctx.mc_ratio}
- Oracle optimal allocation: {ctx.optimal_allocation or "unknown"}
- Theoretical max joint reward: {ctx.oracle_optimum}
- Number of rounds: {len(ctx.rounds)}

{rounds_text}

## Your Task
Analyze ALL {len(ctx.rounds)} rounds above. For each round, identify the 2–4
most important generalizable behavioral patterns (not environment-specific narrations)
that explain the outcome — aim for 1–2 per round, maximum 2. Use short snake_case names for patterns. Note any
cross-round influence (repair, learning, regression) in `prior_round_influence`.
Provide a round-level attribution sentence, then a environment-level narrative.
Return JSON matching the schema.
"""


def build_judge_messages(ctx: JudgeGameContext) -> list[dict]:
    """Build the full message list for a whole-environment judge LLM call."""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": build_judge_user_prompt(ctx)},
    ]


# --- Phase 2a: Taxonomy consolidation (Step 1 — define categories) ---

CONSOLIDATION_SYSTEM_PROMPT = """\
You are a qualitative coding expert building a domain-neutral behavioral
taxonomy. You will receive pattern names and descriptions discovered by an
analyst reviewing multi-agent coordination transcripts. The inputs were
produced in one specific task setting, but your taxonomy must capture the
underlying coordination mechanisms — not the task's surface vocabulary.

Split the taxonomy into two buckets:
1. `negative_patterns` — patterns that consistently hurt joint outcomes
2. `positive_patterns` — patterns that consistently help agents reach
   efficient, mutually acceptable outcomes

## Domain Neutrality (HARD REQUIREMENT)

Every `id`, `label`, and `description` must make equal sense across settings
like negotiation, collaborative coding, multi-agent planning, and scheduling.
If a field only makes sense in one task type, rewrite it in abstract
coordination language — or drop the category.

The following words are FORBIDDEN in any `id`, `label`, or `description`:

  resource, allocation, supply, budget, annulment, overdraw, bid, auction,
  pool, bottleneck, project, reward, recipe, inventory, negotiate, negotiation

When a forbidden word appears in the raw input, translate it to the mechanism
it stands for:

  resource / inventory         → shared capacity, shared dependency
  allocation                   → claimed scope, action scope
  supply (as a constraint)     → shared capacity constraint
  budget                       → available capacity
  annulment / overdraw         → coordination failure, joint-constraint violation
  bid / auction                → competitive claim
  bottleneck                   → contested constraint
  project                      → task, objective
  reward                       → outcome value, payoff
  negotiate / negotiation      → coordinate / coordination

## Taxonomy Rules

- Aim for about 10 categories per bucket (soft target). Produce fewer if the
  data doesn't support more. Hard max: 10 per bucket.
- Every category must lean positive or negative overall — no neutral bucket.
- If valence flips by context (e.g., "Persistent Advocacy" is positive when
  defending the best solution but negative when defending a suboptimal one),
  set `context_dependent: true` and place it in the bucket fitting its
  dominant use.
- Merge synonyms and near-duplicates.
- Each canonical pattern: snake_case `id`, human-readable `label`, clear
  `description`, and `context_dependent` boolean.
- DO NOT include an `aliases` field — aliases are assigned in a separate step.
- Avoid categories that can be read directly from outcome fields of a trace
  (e.g., "agents exceeded shared capacity" is a measurable event, not a
  behavioral pattern — what's the behavior that caused it?).

## Abstraction Examples (BEFORE → AFTER)

BEFORE: "Supply Limit Oversight" — Agents focus on negotiation without
verifying total demand against supply limits, leading to overdraw.
AFTER:  "Shared-Capacity Blindness" — Agents fail to track how their joint
decisions consume a shared constraint, causing a collective violation that
either party could have prevented by checking the combined impact.

BEFORE: "Efficient Budget Utilization" — Both agents fully utilize their
budgets on complementary resources, maximizing total project runs.
AFTER:  "Full-Capacity Coordination" — Agents jointly account for available
capacity and converge on non-conflicting claims that leave no achievable joint
value on the table.

BEFORE: "Transparent Resource Declaration" — Agents honestly disclose their
primary resource needs and project priorities upfront.
AFTER:  "Private-State Disclosure" — Agent voluntarily reveals private
constraints or preferences, enabling the partner to reason about feasibility
without guessing.

## Self-Audit (Required Before Returning)

Before returning your JSON:
1. Scan every `id`, `label`, and `description` for words in the forbidden list.
2. For each violation, rewrite the field using the abstract equivalents above.
   Do not substitute vague filler ("agents behaved suboptimally") — use a
   specific abstract term.
3. Re-scan once more to confirm zero violations.

## Output Schema

{
  "version": 1,
  "negative_patterns": [
    {
      "id": "snake_case_id",
      "label": "Human Readable Label",
      "description": "Domain-neutral description of the coordination mechanism",
      "context_dependent": false
    }
  ],
  "positive_patterns": [
    {
      "id": "snake_case_id",
      "label": "Human Readable Label",
      "description": "Domain-neutral description of the coordination mechanism",
      "context_dependent": false
    }
  ]
}

Respond ONLY with the JSON object — no markdown fences, no preamble.
"""


# --- Phase 2b: Alias mapping (Step 2 — assign every raw name to a category) ---

ALIAS_MAPPING_SYSTEM_PROMPT = """\
You are a qualitative coding assistant. You will receive:
1. A canonical taxonomy of coordination pattern categories (id + label + description).
2. A list of raw pattern names discovered in the corpus (sorted alphabetically with occurrence counts).

Your job is to assign every raw pattern name to exactly one canonical category id.
Use "other" only for names that genuinely do not fit any category.

Rules:
- Every name in the input list must appear exactly once in the output mapping.
- Use the canonical `id` values exactly as given (snake_case).
- If a name is ambiguous between two categories, pick the best fit based on the description.
- Do not invent new categories.

Respond ONLY with a valid JSON object:
{"mappings": {"raw_pattern_name": "canonical_id", ...}}
"""


def build_consolidation_prompt(patterns: list[dict]) -> list[dict]:
    """Build Step 1 messages: pattern names+counts → canonical taxonomy categories (no aliases)."""
    lines = []
    for p in patterns:
        count = p.get("count", 1)
        desc = p.get("description")
        if desc:
            lines.append(f"- {p['name']} (n={count}): {desc}")
        else:
            lines.append(f"- {p['name']} (n={count})")
    pattern_list = "\n".join(lines)
    return [
        {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Here are {len(patterns)} unique pattern names discovered across all rounds "
            f"(sorted alphabetically, n = total occurrences):\n\n"
            f"{pattern_list}\n\n"
            "Define a canonical taxonomy (max 10 categories per bucket). "
            "Do NOT include aliases — that is done separately. Return JSON."
        )},
    ]


def build_alias_mapping_prompt(taxonomy_categories: list[dict], patterns: list[dict]) -> list[dict]:
    """Build Step 2 messages: taxonomy categories + all raw names → {name: canonical_id} mapping."""
    category_lines = []
    for cat in taxonomy_categories:
        category_lines.append(f"- {cat['id']}: {cat['label']} — {cat['description']}")
    categories_text = "\n".join(category_lines)

    name_lines = [f"- {p['name']} (n={p.get('count', 1)})" for p in patterns]
    names_text = "\n".join(name_lines)

    return [
        {"role": "system", "content": ALIAS_MAPPING_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"## Canonical Categories\n\n{categories_text}\n\n"
            f"## Raw Pattern Names to Map ({len(patterns)} total)\n\n{names_text}\n\n"
            "Map every name above to a canonical_id. Return JSON: "
            '{{"mappings": {{"raw_name": "canonical_id", ...}}}}'
        )},
    ]
