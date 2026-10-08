"""Resolve scalar topology profiles without widening design dispositions."""

from copy import deepcopy


TOPOLOGY_PROFILES = {
    "complete": {"default": {"graph": "complete", "channels": {"dm": {"enabled": True}}}},
    "ring": {"default": {"graph": "ring", "channels": {"dm": {"enabled": True}}}},
    "star": {"default": {"graph": "star", "hub": 0, "channels": {"dm": {"enabled": True}}}},
    "phase_shift": {
        "default": {"graph": "ring", "channels": {"dm": {"enabled": True}},
                    "budget": {"per_agent_per_round": 5}},
        "phases": {"VOLUNTARY": {"graph": "star", "hub": 0}, "DECISION": {"channels": {}}},
    },
    "silent": {"default": {"graph": "complete", "channels": {}}},
}


def resolve_config(config):
    result = deepcopy(config)
    profile = result.pop("communication_topology", None)
    if profile is None:
        return result
    if profile not in TOPOLOGY_PROFILES:
        raise ValueError(f"unknown communication_topology profile: {profile}")
    communication = result.setdefault("communication", {})
    if "topology" in communication:
        raise ValueError("communication_topology conflicts with direct communication.topology")
    communication["topology"] = deepcopy(TOPOLOGY_PROFILES[profile])
    return result
