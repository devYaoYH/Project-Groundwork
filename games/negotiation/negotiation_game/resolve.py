"""Batch-level config resolution for negotiation.

Ported from ``generate_projects_for_batch`` in a2a-negotiation's
``scripts/run_experiment.py``. The runner calls this once per batch, before
fan-out, via the ``resolve_config`` hook declared at registration.

Why this matters for reproducibility: ``mc_ratio`` drives a *stochastic*
simulated-annealing solver. Under the old runner the generated scenario existed
only in the request body sent to the server, so a run could not be reconstructed
from its experiment YAML. Resolving here writes ``agent_projects`` and
``oracle_stats`` into the run config, which the runner records in the trace — so
the exact scenario is recoverable from the trace alone.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("negotiation_game.resolve")


def resolve_config(config: dict[str, Any]) -> dict[str, Any]:
    """Expand ``mc_ratio`` into a concrete scenario. Returns a new dict."""
    resolved = dict(config)

    # Scenario-pool runs sample projects per round inside the engine, so there is
    # nothing to solve up front — just hand the target ratio to the engine under
    # the name it expects.
    if resolved.get("scenario_pool_path"):
        mc_ratio = resolved.pop("mc_ratio", None)
        if mc_ratio is not None:
            resolved.setdefault("target_mc_ratio", mc_ratio)
        return resolved

    mc_ratio = resolved.pop("mc_ratio", None)
    if mc_ratio is None:
        return resolved

    # Imported lazily: the solver pulls in heavier numeric deps, and configs
    # without mc_ratio should not pay for them.
    from negotiation_game.backend.optimizer import generate_scenario

    resource_types = resolved.get("resource_types", ["wood", "stone", "gold"])
    result = generate_scenario(
        target_mc=mc_ratio,
        resource_types=resource_types,
        resource_costs=resolved.get("resource_costs", {r: 1.0 for r in resource_types}),
        resource_supply=resolved.get("resource_supply", {r: 10 for r in resource_types}),
        cash_per_player=resolved.get("agent_budget", 10),
    )

    resolved["agent_projects"] = result["agent_projects"]
    resolved["oracle_stats"] = result["oracle_stats"]
    # Record the request alongside what the solver actually hit — the solver is
    # approximate, and the realized ratio is the one the game was played at.
    resolved["requested_mc_ratio"] = mc_ratio
    log.info(
        "Generated scenario: M/C = %.3f (target %.3f)",
        result["oracle_stats"]["mc_ratio"], mc_ratio,
    )
    return resolved
