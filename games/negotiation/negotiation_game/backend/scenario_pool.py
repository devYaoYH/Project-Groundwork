"""
Scenario pool loader for per-round dynamic project rotation.

Loads validated scenarios from scanner output and cosmetic themes,
then provides methods to sample splits and apply theme skins.
"""

import json
import random
from pathlib import Path


class ScenarioPool:
    """Pool of validated scenario splits with cosmetic themes."""

    def __init__(self, pool_path: str, themes_path: str = "data/scenario_themes.json"):
        with open(pool_path) as f:
            data = json.load(f)

        self.themes = self._load_themes(themes_path)

        # Filtered format: {"scenarios": [...], "count": N}
        # Each entry has candidate_id, mc_bucket, oracle_stats, game_config
        self.metadata = {}
        self.scenarios: dict[str, list[dict]] = {}
        for scenario in data["scenarios"]:
            gc = scenario.get("game_config", {})
            # Hoist agent_projects to top level for apply_theme compatibility
            if "agent_projects" not in scenario and "agent_projects" in gc:
                scenario["agent_projects"] = gc["agent_projects"]
            # Extract shared resource config from first scenario
            if not self.metadata and gc:
                self.metadata = {
                    k: gc[k] for k in ("resource_costs", "resource_supply",
                                       "resource_types", "agent_budget")
                    if k in gc
                }
            mc_key = str(round(scenario["mc_bucket"], 1))
            self.scenarios.setdefault(mc_key, []).append(scenario)

    def _load_themes(self, themes_path: str) -> list[dict]:
        path = Path(themes_path)
        if not path.exists():
            return []
        with open(path) as f:
            return json.load(f)

    def pick_theme(self, seed: int | None = None) -> dict:
        """Pick a random cosmetic theme (fixed for entire game).

        Args:
            seed: Random seed for deterministic theme selection
        """
        if not self.themes:
            return {
                "name": "default",
                "resource_names": {"r1": "resource_1", "r2": "resource_2", "r3": "resource_3"},
                "project_names": {},
            }
        if seed is not None:
            rng = random.Random(seed)
            return rng.choice(self.themes)
        return random.choice(self.themes)

    def sample_split(self, mc: float, exclude: list[str] | None = None, seed: int | None = None) -> dict | None:
        """Sample a random validated split for this M/C ratio.

        Args:
            mc: Target M/C ratio (bucketed to nearest 0.1)
            exclude: List of candidate_ids to avoid repeats
            seed: Random seed for deterministic sampling (enables swapped pairs to get same scenario)

        Returns:
            A scenario dict or None if no candidates available.
        """
        mc_key = str(round(mc, 1))
        candidates = self.scenarios.get(mc_key, [])
        if not candidates:
            # Try adjacent buckets
            for offset in (0.1, -0.1):
                alt_key = str(round(mc + offset, 1))
                candidates = self.scenarios.get(alt_key, [])
                if candidates:
                    break

        if not candidates:
            return None

        if exclude:
            exclude_set = set(exclude)
            filtered = [
                c for c in candidates
                if c.get("candidate_id") not in exclude_set
            ]
            if filtered:
                candidates = filtered

        # Use seeded random for deterministic sampling if seed provided
        if seed is not None:
            rng = random.Random(seed)
            return rng.choice(candidates)
        return random.choice(candidates)

    def apply_theme(self, split: dict, theme: dict, named_projects: bool = False) -> dict:
        """Apply cosmetic theme to a split's abstract resource names.

        Args:
            split: Scenario split with agent_projects
            theme: Cosmetic theme with resource_names and project_names
            named_projects: If True, replace generic project_a/b/c with
                themed names from the theme's project_names list.
                Agent 0 gets names 0-2, Agent 1 gets names 3-5.
        """
        resource_names = theme.get("resource_names", {})
        theme_project_names = theme.get("project_names", [])

        def rename_resources(proj: dict, project_name_map: dict | None = None) -> dict:
            themed = dict(proj)
            themed["requirements"] = {
                resource_names.get(r, r): qty
                for r, qty in themed["requirements"].items()
            }
            if project_name_map and themed["name"] in project_name_map:
                themed["name"] = project_name_map[themed["name"]]
            return themed

        # Build per-agent project name mappings when named_projects is enabled
        generic_keys = ["project_a", "project_b", "project_c"]
        agent_projects = split.get("agent_projects", [])
        themed_projects = []
        for agent_idx, agent_projs in enumerate(agent_projects):
            name_map = None
            if named_projects and theme_project_names:
                offset = agent_idx * 3
                name_map = {
                    generic_keys[i]: theme_project_names[offset + i]
                    for i in range(min(len(generic_keys), len(agent_projs)))
                    if offset + i < len(theme_project_names)
                }
            themed_projects.append([rename_resources(p, name_map) for p in agent_projs])

        return {
            **split,
            "agent_projects": themed_projects,
        }

    def get_resource_config(self, theme: dict) -> dict:
        """Get resource types, costs, supply with themed names."""
        resource_names = theme.get("resource_names", {})
        metadata = self.metadata

        base_costs = metadata.get("resource_costs", {"r1": 1.0, "r2": 1.5, "r3": 3.0})
        base_supply = metadata.get("resource_supply", {"r1": 10, "r2": 10, "r3": 6})

        resource_types = [resource_names.get(r, r) for r in sorted(base_costs.keys())]
        resource_costs = {resource_names.get(r, r): v for r, v in base_costs.items()}
        resource_supply = {resource_names.get(r, r): v for r, v in base_supply.items()}

        return {
            "resource_types": resource_types,
            "resource_costs": resource_costs,
            "resource_supply": resource_supply,
            "agent_budget": metadata.get("agent_budget", 18.0),
        }


