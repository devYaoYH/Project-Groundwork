"""Cell-level config resolution for negotiation.

Ported from ``generate_projects_for_cell`` in a2a-negotiation's
``scripts/run_experiment.py``. The runner calls this once per cell, before
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
from copy import deepcopy
from typing import Any

log = logging.getLogger("negotiation_game.resolve")


def resolve_config(config: dict[str, Any]) -> dict[str, Any]:
    """Expand ``mc_ratio`` into a concrete scenario. Returns a new dict."""
    resolved = dict(config)

    # A compiled design selects a row of the pinned bank. Materialize that
    # row before locking; its ratio describes the item, not a solver request.
    if resolved.get("item_id"):
        if (
            resolved.get("mc_ratio") is None
            and resolved.get("agent_projects")
            and resolved.get("oracle_stats")
        ):
            return resolved
        if resolved.get("scenario_pool_path"):
            raise ValueError("A pinned item cannot also select a per-round scenario pool")
        from a2a_engine.items import ItemBank

        from negotiation_game.declaration import _BANK_PATH, DECLARATION

        bank = ItemBank.load(_BANK_PATH, expected_sha256=DECLARATION.item_policy.item_bank_sha256)
        item = bank.get(str(resolved["item_id"]))
        resolved.update(deepcopy(item.params))
        resolved["oracle_stats"] = deepcopy(item.oracle_result)
        resolved["item_bank_sha256"] = bank.item_bank_sha256
        resolved.pop("mc_ratio", None)
        return resolved

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
    # approximate, and the realized ratio is the one the environment was played at.
    resolved["requested_mc_ratio"] = mc_ratio
    log.info(
        "Generated scenario: M/C = %.3f (target %.3f)",
        result["oracle_stats"]["mc_ratio"],
        mc_ratio,
    )
    return resolved
