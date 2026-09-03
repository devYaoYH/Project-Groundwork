"""
Simulated annealing scenario generator for project-based resource allocation games.

Ported from frontend/static/optimizer.html. Generates balanced environment scenarios
targeting a specific M/C ratio (collaboration efficiency metric).
"""

import copy
import json
import logging
import math
import os
import random
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

import certifi

log = logging.getLogger("negotiation")


@dataclass
class SynergyDef:
    resource: str
    threshold: int
    bonus: int


@dataclass
class ProjectDef:
    name: str
    requirements: dict[str, int]
    reward: int


def cost_per_run(project: ProjectDef, resource_costs: dict[str, float]) -> float:
    return sum(resource_costs.get(r, 0) * qty for r, qty in project.requirements.items())


def max_runs(project: ProjectDef, cash: float, resource_costs: dict[str, float],
             resource_supply: dict[str, int]) -> int:
    by_res = float('inf')
    for r, qty in project.requirements.items():
        if qty > 0:
            by_res = min(by_res, resource_supply.get(r, 0) // qty)
    cpr = cost_per_run(project, resource_costs)
    by_cash = int(cash // cpr) if cpr > 0 else 9999
    return min(int(by_res) if by_res != float('inf') else 0, by_cash)


def check_synergy(syn: SynergyDef | None, my_total: int, other_total: int) -> bool:
    if not syn:
        return False
    return my_total >= 1 and other_total >= 1 and my_total + other_total >= syn.threshold


def _count_resource_types(projects: list[ProjectDef], runs: list[int]) -> int:
    """Count distinct resource types purchased across project runs."""
    types_used = set()
    for p, n in zip(projects, runs):
        if n > 0:
            for r, qty in p.requirements.items():
                if qty > 0:
                    types_used.add(r)
    return len(types_used)


def compute_individual_max(projects: list[ProjectDef], cash: float,
                           resource_costs: dict[str, float],
                           resource_supply: dict[str, int],
                           max_types: int = 99) -> dict:
    """Compute max reward for a single player optimizing alone (general N projects)."""
    if not projects:
        return {"reward": 0, "detail": "—", "spent": 0, "res_used": {}}

    best = {"reward": 0, "detail": "—", "spent": 0, "res_used": {}}
    caps = [min(max_runs(p, cash, resource_costs, resource_supply), 30) for p in projects]
    cprs = [cost_per_run(p, resource_costs) for p in projects]
    runs = [0] * len(projects)

    def _fits_supply() -> bool:
        used: dict[str, int] = {}
        for i, n in enumerate(runs):
            if n <= 0:
                continue
            for r, qty in projects[i].requirements.items():
                used[r] = used.get(r, 0) + n * qty
                if used[r] > resource_supply.get(r, 0):
                    return False
        return True

    # Precompute max remaining reward from each index onward for pruning
    max_reward_from = [0] * (len(projects) + 1)
    for i in range(len(projects) - 1, -1, -1):
        max_reward_from[i] = max_reward_from[i + 1] + caps[i] * projects[i].reward

    # Track resource usage incrementally
    used_supply: dict[str, int] = {r: 0 for r in resource_supply}

    def _enumerate(idx: int, cash_left: float, current_reward: int) -> None:
        nonlocal best
        if idx == len(projects):
            if _count_resource_types(projects, runs) > max_types:
                return
            sp = sum(runs[i] * cprs[i] for i in range(len(projects)))
            if current_reward > best["reward"]:
                detail = ", ".join(
                    f"{projects[i].name} x{runs[i]}" for i in range(len(projects)) if runs[i] > 0
                ) or "—"
                ru: dict[str, int] = {}
                for i in range(len(projects)):
                    if runs[i] > 0:
                        for r, qty in projects[i].requirements.items():
                            if qty > 0:
                                ru[r] = ru.get(r, 0) + runs[i] * qty
                best = {"reward": current_reward, "detail": detail, "spent": sp, "res_used": ru}
            return
        # Prune: even if we max out all remaining projects, can't beat best
        if current_reward + max_reward_from[idx] <= best["reward"]:
            return
        max_n = min(caps[idx], int(cash_left / cprs[idx]) if cprs[idx] > 0 else caps[idx])
        # Further cap by supply
        for r, qty in projects[idx].requirements.items():
            if qty > 0:
                avail = resource_supply.get(r, 0) - used_supply.get(r, 0)
                max_n = min(max_n, avail // qty)
        for n in range(max_n + 1):
            runs[idx] = n
            if n > 0:
                for r, qty in projects[idx].requirements.items():
                    used_supply[r] = used_supply.get(r, 0) + qty * n
            _enumerate(idx + 1, cash_left - n * cprs[idx], current_reward + n * projects[idx].reward)
            if n > 0:
                for r, qty in projects[idx].requirements.items():
                    used_supply[r] = used_supply.get(r, 0) - qty * n
        runs[idx] = 0

    _enumerate(0, cash, 0)
    return best


def compute_collab_max(projects_a: list[ProjectDef], projects_b: list[ProjectDef],
                       cash_a: float, cash_b: float,
                       resource_costs: dict[str, float],
                       resource_supply: dict[str, int],
                       synergy: SynergyDef | None = None,
                       max_types: int = 99) -> dict:
    """Compute collaborative maximum reward (joint optimization including synergy).

    Synergy is scenario-level: if both players buy ≥1 of synergy resource AND
    combined total ≥ threshold, each player gets a flat +bonus.
    """
    if not projects_a and not projects_b:
        return {"total": 0, "detail": "—", "base_score_a": 0, "base_score_b": 0, "synergy_bonus": 0, "res_used": {}, "collab_detail": "—", "optimal_count": 0, "best_p1_favor": 0, "best_p2_favor": 0}

    rkeys = list(resource_supply.keys())
    best = {"total": 0, "detail": "—", "base_score_a": 0, "base_score_b": 0, "synergy_bonus": 0, "res_used": {r: 0 for r in rkeys}, "collab_detail": "—"}
    optimal_count = 0
    best_p1_favor = 0
    best_p2_favor = 0

    caps_a = [min(max_runs(p, cash_a, resource_costs, resource_supply), 10) for p in projects_a]
    caps_b = [min(max_runs(p, cash_b, resource_costs, resource_supply), 10) for p in projects_b]

    def try_alloc(r1, r2):
        nonlocal best, optimal_count, best_p1_favor, best_p2_favor
        used = {r: 0 for r in rkeys}
        c1, c2 = 0.0, 0.0

        for i, n in enumerate(r1):
            p = projects_a[i]
            for r, qty in p.requirements.items():
                used[r] += n * qty
            c1 += n * cost_per_run(p, resource_costs)

        for i, n in enumerate(r2):
            p = projects_b[i]
            for r, qty in p.requirements.items():
                used[r] += n * qty
            c2 += n * cost_per_run(p, resource_costs)

        if not all(used.get(r, 0) <= resource_supply.get(r, 0) for r in rkeys):
            return
        if c1 > cash_a or c2 > cash_b:
            return

        # Check max resource types per player
        if _count_resource_types(projects_a, r1) > max_types:
            return
        if _count_resource_types(projects_b, r2) > max_types:
            return

        base_a = sum(n * projects_a[i].reward for i, n in enumerate(r1))
        base_b = sum(n * projects_b[i].reward for i, n in enumerate(r2))
        syn_bon = 0

        if synergy:
            # Per-player resource usage for synergy check
            pu1 = {r: 0 for r in rkeys}
            pu2 = {r: 0 for r in rkeys}
            for i, n in enumerate(r1):
                for r, qty in projects_a[i].requirements.items():
                    pu1[r] += n * qty
            for i, n in enumerate(r2):
                for r, qty in projects_b[i].requirements.items():
                    pu2[r] += n * qty
            my_t = pu1.get(synergy.resource, 0)
            other_t = pu2.get(synergy.resource, 0)
            if check_synergy(synergy, my_t, other_t):
                syn_bon = synergy.bonus * 2  # flat bonus per player × 2 players

        tot = base_a + base_b + syn_bon
        if base_a >= base_b and tot > best_p1_favor:
            best_p1_favor = tot
        if base_b >= base_a and tot > best_p2_favor:
            best_p2_favor = tot
        if tot > best["total"]:
            optimal_count = 1
            parts = [f"{projects_a[i].name}x{n}" for i, n in enumerate(r1) if n > 0]
            parts += [f"{projects_b[i].name}x{n}" for i, n in enumerate(r2) if n > 0]
            # Per-player resource usage for negotiation pressure analysis
            pru1: dict[str, int] = {}
            for i, n in enumerate(r1):
                if n > 0:
                    for r, qty in projects_a[i].requirements.items():
                        if qty > 0:
                            pru1[r] = pru1.get(r, 0) + n * qty
            pru2: dict[str, int] = {}
            for i, n in enumerate(r2):
                if n > 0:
                    for r, qty in projects_b[i].requirements.items():
                        if qty > 0:
                            pru2[r] = pru2.get(r, 0) + n * qty
            best = {
                "total": tot,
                "detail": ", ".join(parts) or "—",
                "base_score_a": base_a,
                "base_score_b": base_b,
                "synergy_bonus": syn_bon,
                "res_used": dict(used),
                "res_used_a": pru1,
                "res_used_b": pru2,
                "collab_detail": ", ".join(parts) or "—",
            }
        elif tot == best["total"] and tot > 0:
            optimal_count += 1

    cprs_a = [cost_per_run(p, resource_costs) for p in projects_a]
    cprs_b = [cost_per_run(p, resource_costs) for p in projects_b]
    r1_arr = [0] * len(projects_a)
    r2_arr = [0] * len(projects_b)

    def loop_b(i2: int, cash_left_b: float) -> None:
        if i2 == len(projects_b):
            try_alloc(list(r1_arr), list(r2_arr))
            return
        max_n = min(caps_b[i2], int(cash_left_b / cprs_b[i2]) if cprs_b[i2] > 0 else caps_b[i2])
        for n in range(max_n + 1):
            r2_arr[i2] = n
            loop_b(i2 + 1, cash_left_b - n * cprs_b[i2])
        r2_arr[i2] = 0

    def loop_a(i1: int, cash_left_a: float) -> None:
        if i1 == len(projects_a):
            loop_b(0, cash_b)
            return
        max_n = min(caps_a[i1], int(cash_left_a / cprs_a[i1]) if cprs_a[i1] > 0 else caps_a[i1])
        for n in range(max_n + 1):
            r1_arr[i1] = n
            loop_a(i1 + 1, cash_left_a - n * cprs_a[i1])
        r1_arr[i1] = 0

    loop_a(0, cash_a)
    best["optimal_count"] = optimal_count
    best["best_p1_favor"] = best_p1_favor
    best["best_p2_favor"] = best_p2_favor
    return best


def action_space_size(projects: list[ProjectDef], cash: float,
                      resource_costs: dict[str, float],
                      resource_supply: dict[str, int],
                      max_types: int = 99) -> int:
    """Count the number of valid allocation vectors for a player."""
    if not projects:
        return 0
    caps = [min(max_runs(p, cash, resource_costs, resource_supply), 30) for p in projects]
    cprs = [cost_per_run(p, resource_costs) for p in projects]
    count = 0
    runs = [0] * len(projects)

    def _enumerate(idx: int, cash_left: float) -> None:
        nonlocal count
        if idx == len(projects):
            if _count_resource_types(projects, runs) > max_types:
                return
            # Check supply fits
            used: dict[str, int] = {}
            for i, n in enumerate(runs):
                if n <= 0:
                    continue
                for r, qty in projects[i].requirements.items():
                    used[r] = used.get(r, 0) + n * qty
                    if used[r] > resource_supply.get(r, 0):
                        return
            count += 1
            return
        max_n = min(caps[idx], int(cash_left / cprs[idx]) if cprs[idx] > 0 else caps[idx])
        for n in range(max_n + 1):
            runs[idx] = n
            _enumerate(idx + 1, cash_left - n * cprs[idx])
        runs[idx] = 0

    _enumerate(0, cash)
    return count


def _check_affordability(projects: list[ProjectDef], cash: float,
                         resource_costs: dict[str, float],
                         resource_supply: dict[str, int]) -> int:
    """Count how many projects can be run at least once individually.

    Each project is checked independently (not competing for resources).
    Returns the number of affordable projects.
    """
    affordable = 0
    for p in projects:
        cpr = cost_per_run(p, resource_costs)
        if cpr > cash:
            continue
        # Check resource supply allows at least 1 run
        feasible = all(
            resource_supply.get(r, 0) >= qty
            for r, qty in p.requirements.items() if qty > 0
        )
        if feasible:
            affordable += 1
    return affordable


def _resource_overlap(ru1: dict[str, int], ru2: dict[str, int]) -> float:
    """Jaccard overlap of resource types used (by quantity > 0)."""
    s1 = {r for r, v in ru1.items() if v > 0}
    s2 = {r for r, v in ru2.items() if v > 0}
    if not s1 and not s2:
        return 0.0
    inter = len(s1 & s2)
    return inter / len(s1 | s2)


def _resource_divergence(solo_res: dict[str, int], collab_res: dict[str, int]) -> float:
    """How much a player's resource profile changed from solo to collab (1 - Jaccard)."""
    s1 = {r for r, v in solo_res.items() if v > 0}
    s2 = {r for r, v in collab_res.items() if v > 0}
    if not s1 and not s2:
        return 0.0
    inter = len(s1 & s2)
    jaccard = inter / len(s1 | s2)
    return 1.0 - jaccard


def evaluate_scenario(projects: list[ProjectDef], assignment: dict[str, str],
                      cash: float, resource_costs: dict[str, float],
                      resource_supply: dict[str, int],
                      synergy: SynergyDef | None = None,
                      max_types: int = 99) -> dict:
    """Evaluate a scenario: compute V1, V2, C, M, M/C ratio."""
    pp1 = [p for p in projects if assignment.get(p.name) == "p1"]
    pp2 = [p for p in projects if assignment.get(p.name) == "p2"]
    r1_result = compute_individual_max(pp1, cash, resource_costs, resource_supply, max_types=max_types)
    r2_result = compute_individual_max(pp2, cash, resource_costs, resource_supply, max_types=max_types)
    r1 = r1_result["reward"]
    r2 = r2_result["reward"]
    combined = r1 + r2
    cm = compute_collab_max(pp1, pp2, cash, cash, resource_costs, resource_supply, synergy=synergy, max_types=max_types)
    mc_ratio = cm["total"] / combined if combined > 0 else 1
    asc1 = action_space_size(pp1, cash, resource_costs, resource_supply, max_types=max_types)
    asc2 = action_space_size(pp2, cash, resource_costs, resource_supply, max_types=max_types)
    swap_fairness = min(cm["best_p1_favor"], cm["best_p2_favor"]) / max(cm["best_p1_favor"], cm["best_p2_favor"], 1)
    # Affordability: count projects each player can run at least once
    afford1 = _check_affordability(pp1, cash, resource_costs, resource_supply)
    afford2 = _check_affordability(pp2, cash, resource_costs, resource_supply)

    # Negotiation pressure: solo strategies conflict but joint-optimal requires change
    solo_overlap = _resource_overlap(r1_result.get("res_used", {}), r2_result.get("res_used", {}))
    div1 = _resource_divergence(r1_result.get("res_used", {}), cm.get("res_used_a", {}))
    div2 = _resource_divergence(r2_result.get("res_used", {}), cm.get("res_used_b", {}))
    collab_divergence = (div1 + div2) / 2
    negotiation_pressure = solo_overlap * collab_divergence

    return {
        "combined": combined,
        "collab": cm["total"],
        "mc_ratio": mc_ratio,
        "gap": abs(r1 - r2),
        "r1": r1,
        "r2": r2,
        "base1": cm.get("base_score_a", 0),
        "base2": cm.get("base_score_b", 0),
        "unaffordable": (len(pp1) - afford1) + (len(pp2) - afford2),
        "optimal_count": cm.get("optimal_count", 0),
        "solo_overlap": solo_overlap,
        "collab_divergence": collab_divergence,
        "negotiation_pressure": negotiation_pressure,
        "asc1": asc1,
        "asc2": asc2,
        "best_p1_favor": cm["best_p1_favor"],
        "best_p2_favor": cm["best_p2_favor"],
        "swap_fairness": swap_fairness,
    }


def _score(ev: dict, target_mc: float) -> float:
    ratio_diff = abs(ev["mc_ratio"] - target_mc)
    # Hard constraint: M/C must be within ±0.05 of target, soft penalty within that band
    ratio_err = (500 + ratio_diff * 100) if ratio_diff > 0.05 else ratio_diff * 100
    leak_penalty = 300 if (target_mc <= 1.0 and ev["mc_ratio"] > 1.0) else 0
    # Hard fairness: V1 must equal V2
    gap_penalty = 500 if ev["gap"] > 0 else 0
    trivial_penalty = 200 if ev["combined"] < 2 else 0
    score_penalty = abs(ev["combined"] - 40) * 1.5
    # superPenalty: for collaborative scenarios, reward when each player's collab base ≥ solo max
    super_penalty = 0
    if target_mc > 1.0:
        if ev.get("base1", 0) >= ev["r1"]:
            super_penalty += 100
        if ev.get("base2", 0) >= ev["r2"]:
            super_penalty += 100
    card_penalty = abs(ev.get("asc1", 0) - ev.get("asc2", 0)) / max(ev.get("asc1", 1), ev.get("asc2", 1), 1) * 30
    swap_penalty = 500 if ev.get("swap_fairness", 1.0) < 1.0 else 0
    # Heavy penalty for unaffordable projects (each project must be runnable at least once)
    afford_penalty = ev.get("unaffordable", 0) * 500
    ambiguity_penalty = 150 if ev.get("optimal_count", 0) < 2 else 0
    negotiation_bonus = ev.get("negotiation_pressure", 0) * 200
    return ratio_err + leak_penalty + 0.5 * gap_penalty + trivial_penalty + score_penalty + super_penalty + card_penalty + swap_penalty + afford_penalty + ambiguity_penalty - negotiation_bonus


def _deep_clone_projects(projects: list[ProjectDef]) -> list[ProjectDef]:
    return [ProjectDef(
        name=p.name,
        requirements=dict(p.requirements),
        reward=p.reward,
    ) for p in projects]


def _player_covers_all_types(projects: list[ProjectDef], assignment: dict[str, str],
                              zone: str, resource_types: list[str]) -> bool:
    """Check that a player's projects collectively cover all resource types."""
    used = set()
    for p in projects:
        if assignment[p.name] == zone:
            for r, qty in p.requirements.items():
                if qty > 0:
                    used.add(r)
    return all(r in used for r in resource_types)


def _has_duplicate_projects(projects: list[ProjectDef], assignment: dict[str, str], zone: str) -> bool:
    """Check if any two projects assigned to the same player are identical (same requirements and reward)."""
    zp = [p for p in projects if assignment[p.name] == zone]
    for i in range(len(zp)):
        for j in range(i + 1, len(zp)):
            if zp[i].reward == zp[j].reward and zp[i].requirements == zp[j].requirements:
                return True
    return False


def _neighbour(projects: list[ProjectDef], assignment: dict[str, str],
               cash: float, resource_types: list[str],
               allow_synergy: bool,
               synergy: SynergyDef | None = None,
               resource_costs: dict[str, float] | None = None) -> tuple[list[ProjectDef], dict[str, str], float, SynergyDef | None]:
    p2 = _deep_clone_projects(projects)
    a2 = dict(assignment)
    syn2 = SynergyDef(synergy.resource, synergy.threshold, synergy.bonus) if synergy else None

    move = random.randint(0, 4 if allow_synergy else 3)

    if move == 0:
        # Reassign
        zones = ["p1", "p2"]
        p = random.choice(p2)
        from_zone = a2[p.name]
        from_count = sum(1 for x in p2 if a2[x.name] == from_zone)
        if from_count > 3:
            prev_zone = a2[p.name]
            idx = zones.index(from_zone)
            a2[p.name] = zones[(idx + 1) % 2]
            if not _player_covers_all_types(p2, a2, "p1", resource_types) or \
               not _player_covers_all_types(p2, a2, "p2", resource_types) or \
               _has_duplicate_projects(p2, a2, "p1") or \
               _has_duplicate_projects(p2, a2, "p2"):
                a2[p.name] = prev_zone
    elif move == 1:
        # Reward perturbation
        p = random.choice(p2)
        prev_reward = p.reward
        p.reward = max(1, min(10, p.reward + random.choice([-2, -1, 1, 2])))
        if _has_duplicate_projects(p2, a2, a2[p.name]):
            p.reward = prev_reward
    elif move == 2:
        # Quantity perturbation
        p = random.choice(p2)
        rkeys = list(p.requirements.keys())
        if rkeys:
            r = random.choice(rkeys)
            prev = p.requirements[r]
            p.requirements[r] = max(1, min(6, p.requirements[r] + random.choice([-1, 1])))
            if (resource_costs and cost_per_run(p, resource_costs) > cash) or \
               not _player_covers_all_types(p2, a2, "p1", resource_types) or \
               not _player_covers_all_types(p2, a2, "p2", resource_types) or \
               _has_duplicate_projects(p2, a2, a2[p.name]):
                p.requirements[r] = prev
    elif move == 3:
        # Player swap
        pp1 = [p for p in p2 if a2[p.name] == "p1"]
        pp2_list = [p for p in p2 if a2[p.name] == "p2"]
        if len(pp1) >= 3 and len(pp2_list) >= 3:
            pa = random.choice(pp1)
            pb = random.choice(pp2_list)
            a2[pa.name] = "p2"
            a2[pb.name] = "p1"
            if not _player_covers_all_types(p2, a2, "p1", resource_types) or \
               not _player_covers_all_types(p2, a2, "p2", resource_types) or \
               _has_duplicate_projects(p2, a2, "p1") or \
               _has_duplicate_projects(p2, a2, "p2"):
                a2[pa.name] = "p1"
                a2[pb.name] = "p2"
    else:
        # Scenario-level synergy perturbation
        if not syn2:
            syn2 = SynergyDef(
                resource=random.choice(resource_types),
                threshold=random.randint(4, 10),
                bonus=random.randint(1, 4),
            )
        else:
            sub = random.randint(0, 2)
            if sub == 0:
                syn2 = None
            elif sub == 1:
                syn2.threshold = max(2, syn2.threshold + random.choice([-2, -1, 1, 2]))
            else:
                syn2.bonus = max(1, min(8, syn2.bonus + random.choice([-1, 1])))

    return p2, a2, cash, syn2


def generate_scenario(target_mc: float,
                      resource_types: list[str],
                      resource_costs: dict[str, float],
                      resource_supply: dict[str, int],
                      cash_per_player: float,
                      num_projects: int = 6,
                      max_iter: int = 5000,
                      max_types: int = 2) -> dict:
    """Run simulated annealing to generate a balanced scenario.

    Returns dict with: projects (list of dicts), assignment, cash_per_player, oracle_stats
    """
    # Initialize projects with resource coverage retry (up to 200 attempts)
    projects = []
    assignment = {}
    for attempt in range(200):
        projects = []
        for i in range(num_projects):
            n_res = random.randint(1, min(2, len(resource_types)))
            chosen = random.sample(resource_types, n_res)
            req = {}
            for r in chosen:
                max_by_supply = resource_supply.get(r, 4)
                max_by_cash = int(cash_per_player / resource_costs.get(r, 1)) if resource_costs.get(r, 0) > 0 else 4
                upper = max(1, min(4, max_by_supply, max_by_cash))
                req[r] = random.randint(1, upper)
            # Retry if project is unaffordable
            while cost_per_run(ProjectDef(name="", requirements=req, reward=0), resource_costs) > cash_per_player:
                req = {}
                for r in chosen:
                    req[r] = random.randint(1, 4)
            projects.append(ProjectDef(
                name=f"p{i}",
                requirements=req,
                reward=random.randint(2, 6),
            ))
        assignment = {}
        for i, p in enumerate(projects):
            assignment[p.name] = "p1" if i < 3 else "p2"
        if _player_covers_all_types(projects, assignment, "p1", resource_types) and \
           _player_covers_all_types(projects, assignment, "p2", resource_types) and \
           not _has_duplicate_projects(projects, assignment, "p1") and \
           not _has_duplicate_projects(projects, assignment, "p2"):
            break

    allow_synergy = target_mc > 1.0
    synergy: SynergyDef | None = None

    cash = cash_per_player
    cur_ev = evaluate_scenario(projects, assignment, cash, resource_costs, resource_supply, synergy, max_types=max_types)
    cur_score = _score(cur_ev, target_mc)
    best_state = {
        "projects": _deep_clone_projects(projects),
        "assignment": dict(assignment),
        "cash": cash,
        "ev": cur_ev,
        "score": cur_score,
        "synergy": SynergyDef(synergy.resource, synergy.threshold, synergy.bonus) if synergy else None,
    }

    T0 = 80.0
    Tmin = 0.5
    alpha = (Tmin / T0) ** (1.0 / max_iter)
    T = T0
    improvements = 0
    iterations_run = 0

    for _ in range(max_iter):
        iterations_run += 1
        if best_state["score"] < 0.5:
            break

        for _ in range(12):
            nb_projs, nb_assign, nb_cash, nb_syn = _neighbour(
                projects, assignment, cash, resource_types, allow_synergy, synergy,
                resource_costs=resource_costs
            )
            nb_ev = evaluate_scenario(nb_projs, nb_assign, nb_cash, resource_costs, resource_supply, nb_syn, max_types=max_types)
            nb_score = _score(nb_ev, target_mc)
            diff = nb_score - cur_score
            if diff < 0 or random.random() < math.exp(-diff / T):
                projects = nb_projs
                assignment = nb_assign
                cash = nb_cash
                synergy = nb_syn
                cur_ev = nb_ev
                cur_score = nb_score
                if cur_score < best_state["score"]:
                    improvements += 1
                    best_state = {
                        "projects": _deep_clone_projects(projects),
                        "assignment": dict(assignment),
                        "cash": cash,
                        "ev": cur_ev,
                        "score": cur_score,
                        "synergy": SynergyDef(synergy.resource, synergy.threshold, synergy.bonus) if synergy else None,
                    }
            T *= alpha

    # Build result
    bp = best_state["projects"]
    ba = best_state["assignment"]
    ev = best_state["ev"]
    best_synergy = best_state["synergy"]

    # Convert to agent_projects format (exactly 3 per player)
    p1_projects = [p for p in bp if ba[p.name] == "p1"][:3]
    p2_projects = [p for p in bp if ba[p.name] == "p2"][:3]

    # Rename to project_a, project_b, project_c
    for i, p in enumerate(p1_projects):
        p.name = f"project_{'abc'[i]}"
    for i, p in enumerate(p2_projects):
        p.name = f"project_{'abc'[i]}"

    def proj_to_dict(p: ProjectDef) -> dict:
        return {
            "name": p.name,
            "requirements": dict(p.requirements),
            "reward": p.reward,
        }

    # Compute collab details for the final best state
    cm = compute_collab_max(p1_projects, p2_projects,
                            best_state["cash"], best_state["cash"],
                            resource_costs, resource_supply,
                            synergy=best_synergy, max_types=max_types)

    scenario_synergy = None
    if best_synergy:
        scenario_synergy = {
            "resource": best_synergy.resource,
            "threshold": best_synergy.threshold,
            "bonus": best_synergy.bonus,
        }

    result = {
        "agent_projects": [
            [proj_to_dict(p) for p in p1_projects],
            [proj_to_dict(p) for p in p2_projects],
        ],
        "scenario_synergy": scenario_synergy,
        "cash_per_player": best_state["cash"],
        "oracle_stats": {
            "v1": ev["r1"],
            "v2": ev["r2"],
            "combined": ev["combined"],
            "collab_max": ev["collab"],
            "mc_ratio": ev["mc_ratio"],
            "base_score_a": cm.get("base_score_a", 0),
            "base_score_b": cm.get("base_score_b", 0),
            "synergy_bonus": cm.get("synergy_bonus", 0),
            "collab_detail": cm.get("collab_detail", "—"),
            "res_used": cm.get("res_used", {}),
            "optimal_paths": cm.get("optimal_count", 0),
            "asc1": ev.get("asc1", 0),
            "asc2": ev.get("asc2", 0),
            "best_p1_favor": ev.get("best_p1_favor", 0),
            "best_p2_favor": ev.get("best_p2_favor", 0),
            "swap_fairness": ev.get("swap_fairness", 0),
            "solo_overlap": ev.get("solo_overlap", 0),
            "collab_divergence": ev.get("collab_divergence", 0),
            "negotiation_pressure": ev.get("negotiation_pressure", 0),
        },
        "solver_stats": {
            "candidates": iterations_run * 12,
            "improvements": improvements,
        },
    }

    # Try to name projects via LLM
    try:
        old_names = [p["name"] for agent_projs in result["agent_projects"] for p in agent_projs]
        result["agent_projects"] = name_projects(result["agent_projects"])
        new_names = [p["name"] for agent_projs in result["agent_projects"] for p in agent_projs]
        # Update collab_detail with new names
        detail = result["oracle_stats"]["collab_detail"]
        for old, new in zip(old_names, new_names):
            detail = detail.replace(old, new)
        result["oracle_stats"]["collab_detail"] = detail
    except Exception:
        pass  # Keep generic names on failure

    return result


def name_projects(agent_projects: list[list[dict]]) -> list[list[dict]]:
    """Use Gemini Flash to generate creative 2-word names for projects.

    Tries Vertex AI (ADC) first, then falls back to GOOGLE_API_KEY.
    On any failure, returns projects unchanged.
    """
    # Collect all projects with their indices for mapping back
    all_projects = []
    for agent_idx, projects in enumerate(agent_projects):
        for proj_idx, proj in enumerate(projects):
            all_projects.append(proj)

    if not all_projects:
        return agent_projects

    # Build prompt — label each project with a sequential index
    project_lines = []
    for i, p in enumerate(all_projects):
        reqs = ", ".join(f"{r} x{q}" for r, q in p["requirements"].items())
        project_lines.append(f"- Project {i+1}: requires [{reqs}], reward={p['reward']}")

    prompt = (
        "You are naming projects in a resource trading environment. "
        f"There are {len(all_projects)} projects total. "
        "Given their resource requirements and rewards, "
        "generate a creative, thematic 2-word name for each.\n\n"
        + "\n".join(project_lines) + "\n\n"
        f"Return ONLY a JSON array of exactly {len(all_projects)} strings, one name per project, in the same order. "
        "Each name must be exactly 2 words, title-cased, evocative and distinct. "
        "Example: [\"Iron Forge\", \"Gold Rush\", \"Stone Keep\", \"Timber Vale\"]"
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 1024},
    }
    payload = json.dumps(body).encode()

    # Try Vertex AI first (Cloud Run with ADC)
    url = None
    headers = {"Content-Type": "application/json"}

    try:
        import google.auth
        import google.auth.transport.requests
        credentials, project = google.auth.default()
        credentials.refresh(google.auth.transport.requests.Request())
        token = credentials.token
        url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        # Fall back to API key
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return agent_projects
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        log.error("Project naming HTTP %d: %s", e.code, error_body)
        return agent_projects

    # Parse response
    text = result["candidates"][0]["content"]["parts"][0]["text"]
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        names = json.loads(text)
    except json.JSONDecodeError:
        # Try to fix truncated JSON arrays: extract quoted strings manually
        names = re.findall(r'"([^"]+)"', text)
        if len(names) != len(all_projects):
            log.error("Project naming JSON parse failed | raw text: %s", text)
            return agent_projects
        log.info("Project naming recovered %d names from malformed JSON", len(names))
    if not isinstance(names, list) or len(names) != len(all_projects):
        log.error("Project naming unexpected response: expected %d names, got %s", len(all_projects), names)
        return agent_projects

    # Map names back
    named = copy.deepcopy(agent_projects)
    i = 0
    for agent_idx, projects in enumerate(named):
        for proj_idx, proj in enumerate(projects):
            proj["name"] = names[i]
            i += 1

    return named


def _build_oracle_prompt(
    agent_projects: list[list[dict]],
    resource_types: list[str],
    resource_supply: dict[str, int],
    resource_costs: dict[str, float],
    agent_budget: float,
    scenario_synergy: dict | None,
    max_resource_types: int = 2,
    max_tokens: int | None = None,
) -> str:
    """Build the oracle prompt for joint-optimal resource allocation."""
    lines = [
        "ORACLE RESOURCE ALLOCATION — MAXIMISE JOINT SCORE",
        "",
        "You are an omniscient oracle with full visibility into both players' projects.",
        "Your goal is to determine the purchase vector for each player that MAXIMISES the combined total reward (P1 score + P2 score), including any synergy bonus.",
        "",
    ]
    if max_tokens:
        lines.append(f"TOKEN BUDGET: You have {max_tokens:,} tokens total for reasoning + output. Be concise.")
        lines.append("")
    lines.append("SHARED RESOURCE POOL:")
    for r in resource_types:
        lines.append(f"  {r}: {resource_supply[r]} units total, ${resource_costs[r]}/unit")
    lines.append("")
    lines.append(f"CASH BUDGET: ${agent_budget} per player (independent budgets)")
    lines.append("")

    for idx, label in enumerate(["PLAYER 1", "PLAYER 2"]):
        lines.append(f"{label} PROJECTS:")
        for p in agent_projects[idx]:
            reqs = " + ".join(f"{q} {r}" for r, q in p["requirements"].items())
            lines.append(f"  {p['name']}: requires {reqs} per run | reward: {p['reward']}/run")
        lines.append("")

    if scenario_synergy:
        syn = scenario_synergy
        lines.append("SYNERGY BONUS:")
        lines.append(f"  Trigger: combined {syn['resource']} purchased by both players >= {syn['threshold']} AND each player bought >= 1 unit of {syn['resource']}")
        lines.append(f"  Reward: flat +{syn['bonus']} added to EACH player's score for the round (independent of run count)")
    else:
        lines.append("SYNERGY BONUS: none active.")
    lines.append("")

    res_keys_json = json.dumps({r: 0 for r in resource_types})

    # Build project name examples for JSON template
    p1_proj_json = json.dumps({p["name"]: 0 for p in agent_projects[0]})
    p2_proj_json = json.dumps({p["name"]: 0 for p in agent_projects[1]})

    lines.extend([
        "MECHANICS:",
        "  Each player purchases resources and allocates them across their projects.",
        "  A player chooses how many times to run each project. Running a project N times",
        "  CONSUMES N * required[r] of each resource r. The total resources consumed across",
        "  ALL of a player's project runs must not exceed their purchases:",
        "    sum over all projects p: runs(p) * p.required[r]  <=  purchased[r]  for every resource r",
        "  score_i = sum of (runs * reward_per_run) across player i's projects + synergy bonus if triggered",
        "  joint_score = score_1 + score_2  <-- THIS IS WHAT YOU ARE MAXIMISING",
        "",
        "HARD CONSTRAINTS (violating ANY of these invalidates the solution):",
        f"  1. RESOURCE TYPE LIMIT: each player may purchase AT MOST {max_resource_types} distinct resource types.",
        f"     A player's purchases must have at most {max_resource_types} non-zero entries. This is strictly enforced.",
        "  2. Pool constraint: p1_purchases[r] + p2_purchases[r] <= pool[r] for all r",
        "  3. Cash constraint: sum of (units * cost) <= budget for each player independently",
        "",
        "Think step by step:",
        f"  1. First, note that each player can only buy {max_resource_types} resource types — plan project runs around this limit.",
        "  2. Determine how many runs of each project each player should do.",
        "  3. Compute the total resources consumed: for each resource r, sum up runs(p) * p.required[r] across ALL projects.",
        "     The purchases for resource r must be >= total consumed. Do NOT assume each project's resources are independent.",
        "  4. Verify ALL constraints: resource type limit, pool, cash budget, and that purchases cover all project runs.",
        "  5. Output JSON.",
        "",
        "NOTE: Keep your reasoning concise to stay within your token budget.",
        "",
        "Respond ONLY with raw JSON (no markdown):",
        "{",
        '  "reasoning": "step-by-step explanation of why this allocation maximises joint score",',
        f'  "player1": {{"purchases": {res_keys_json}, "projects": {p1_proj_json}}},',
        f'  "player2": {{"purchases": {res_keys_json}, "projects": {p2_proj_json}}}',
        "}",
        "Include all resources and projects even if 0. The projects field maps project name to number of runs.",
    ])
    return "\n".join(lines)


def _call_llm(agent_config: dict, prompt: str) -> str:
    """Make a one-shot LLM call using the agent's config. Returns raw text."""
    from negotiation_game.backend.agents.api import call_llm_oneshot
    from negotiation_game.backend.agents.factory import detect_provider, get_api_key_for_provider

    api_base = agent_config.get("api_base", "")
    api_key = agent_config.get("api_key", "")
    model = agent_config.get("model", "")
    api_format = agent_config.get("api_format", "")

    # Auto-detect if not provided
    # if not api_base or not api_key or not api_format:
    provider = detect_provider(model)
    if not api_base:
        api_base = {
            "anthropic": "https://api.anthropic.com/v1",
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "ollama": "http://localhost:11434/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }.get(provider, "")
    if not api_key:
        api_key = get_api_key_for_provider(provider)
    if not api_format:
        api_format = "anthropic" if provider == "anthropic" else "openai"

    return call_llm_oneshot(
        api_format=api_format,
        api_base=api_base,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=agent_config.get("temperature", 0.7),
    )


def oracle_solve(
    agent_projects: list[list[dict]],
    resource_types: list[str],
    resource_supply: dict[str, int],
    resource_costs: dict[str, float],
    agent_budget: float,
    scenario_synergy: dict | None,
    agent_config: dict,
    max_resource_types: int = 2,
) -> dict:
    """Use an LLM as an oracle to find the joint-optimal allocation."""
    # Get token limit for the model
    from negotiation_game.backend.agents.model_config import get_model_config
    model = agent_config.get("model", "")
    model_cfg = get_model_config(model)
    max_tokens = model_cfg.default_max_tokens

    prompt = _build_oracle_prompt(
        agent_projects, resource_types, resource_supply,
        resource_costs, agent_budget, scenario_synergy,
        max_resource_types=max_resource_types,
        max_tokens=max_tokens,
    )

    try:
        raw = _call_llm(agent_config, prompt)
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log.error("Oracle solve LLM call failed: HTTP %d - %s", e.code, error_body or e)
        return {"error": f"LLM call failed: HTTP {e.code}: {error_body or e}", "raw": "", "prompt": prompt}
    except Exception as e:
        log.error("Oracle solve LLM call failed: %s: %s", type(e).__name__, e)
        return {"error": f"LLM call failed: {type(e).__name__}: {e}", "raw": "", "prompt": prompt}

    if not raw or not raw.strip():
        return {"error": "LLM returned an empty response", "raw": raw or "", "prompt": prompt}

    # Parse JSON
    clean = re.sub(r"```json|```", "", raw).strip()
    parsed = None

    # Try direct parse
    try:
        parsed = json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting first JSON object
    if parsed is None:
        match = re.search(r'\{[\s\S]*\}', clean)
        if match:
            try:
                parsed = json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                # Try repair
                repaired = re.sub(r',\s*([}\]])', r'\1', match.group())
                opens = repaired.count('{') - repaired.count('}')
                repaired += '}' * max(0, opens)
                try:
                    parsed = json.loads(repaired)
                except (json.JSONDecodeError, ValueError):
                    pass

    if parsed is None:
        return {"error": f"Failed to parse LLM response as JSON", "raw": raw, "prompt": prompt}

    # Extract purchases and project runs
    p1_purchases = {}
    p2_purchases = {}
    p1_project_runs = {}
    p2_project_runs = {}
    if "player1" in parsed:
        if "purchases" in parsed["player1"]:
            p1_purchases = {k: int(v) for k, v in parsed["player1"]["purchases"].items()}
        if "projects" in parsed["player1"]:
            p1_project_runs = {k: int(v) for k, v in parsed["player1"]["projects"].items()}
    if "player2" in parsed:
        if "purchases" in parsed["player2"]:
            p2_purchases = {k: int(v) for k, v in parsed["player2"]["purchases"].items()}
        if "projects" in parsed["player2"]:
            p2_project_runs = {k: int(v) for k, v in parsed["player2"]["projects"].items()}

    # Score using LLM-provided project runs (explicit allocation) rather than greedy assignment
    def compute_score(purchases: dict, projects: list[dict], project_runs: dict, other_purchases: dict, player_label: str) -> dict:
        total_reward = 0
        details = []
        # Track cumulative resource consumption across all project runs
        resource_consumed: dict[str, int] = {}

        for p in projects:
            # Use LLM-provided runs if available, otherwise fall back to greedy
            if project_runs:
                runs = max(0, project_runs.get(p["name"], 0))
            else:
                runs = min(
                    purchases.get(r, 0) // q
                    for r, q in p["requirements"].items()
                )
                runs = max(0, runs)

            # Validate that purchases cover the resource requirements for these runs
            if runs > 0:
                for r, q in p["requirements"].items():
                    needed = runs * q
                    resource_consumed[r] = resource_consumed.get(r, 0) + needed

            rew = runs * p["reward"]
            if runs > 0:
                details.append(f"{p['name']} x{runs} = {rew}")
            total_reward += rew

        # Check that total resource consumption doesn't exceed purchases
        for r, consumed in resource_consumed.items():
            available = purchases.get(r, 0)
            if consumed > available:
                warnings.append(
                    f"{player_label} project runs require {r}: {consumed} but only purchased {available}"
                )

        syn_bonus = 0
        if scenario_synergy:
            syn = scenario_synergy
            my_t = purchases.get(syn["resource"], 0)
            other_t = other_purchases.get(syn["resource"], 0)
            if my_t >= 1 and other_t >= 1 and my_t + other_t >= syn["threshold"]:
                syn_bonus = syn["bonus"]
                total_reward += syn_bonus

        spent = sum(resource_costs.get(r, 0) * v for r, v in purchases.items())
        return {"reward": total_reward, "spent": spent, "details": details, "synergy_bonus": syn_bonus}

    # Validate constraints
    warnings = []

    s1 = compute_score(p1_purchases, agent_projects[0], p1_project_runs, p2_purchases, "P1")
    s2 = compute_score(p2_purchases, agent_projects[1], p2_project_runs, p1_purchases, "P2")
    for r in resource_types:
        if p1_purchases.get(r, 0) + p2_purchases.get(r, 0) > resource_supply.get(r, 0):
            warnings.append(f"Pool exceeded for {r}")
    if s1["spent"] > agent_budget:
        warnings.append(f"P1 over budget (${s1['spent']} > ${agent_budget})")
    if s2["spent"] > agent_budget:
        warnings.append(f"P2 over budget (${s2['spent']} > ${agent_budget})")
    p1_nonzero = sum(1 for v in p1_purchases.values() if v > 0)
    p2_nonzero = sum(1 for v in p2_purchases.values() if v > 0)
    if p1_nonzero > max_resource_types:
        warnings.append(f"P1 exceeded max {max_resource_types} resource types ({p1_nonzero} used)")
    if p2_nonzero > max_resource_types:
        warnings.append(f"P2 exceeded max {max_resource_types} resource types ({p2_nonzero} used)")

    return {
        "player1": {"purchases": p1_purchases, "project_runs": p1_project_runs, **s1},
        "player2": {"purchases": p2_purchases, "project_runs": p2_project_runs, **s2},
        "combined": s1["reward"] + s2["reward"],
        "reasoning": parsed.get("reasoning", ""),
        "warnings": warnings,
        "prompt": prompt,
    }
