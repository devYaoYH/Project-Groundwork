"""Platform-funded model admission limits, recorded in frozen launch inputs.

Token and duration limits bound requests; they are not a currency estimate.
Administrators may further restrict the published model names by environment.
"""
from __future__ import annotations

import os


def apply_policy(configs: list[dict]) -> dict:
    """Validate each execution input and make the output cap explicit."""
    maximum = int(os.getenv("PLATFORM_MAX_OUTPUT_TOKENS", "4096"))
    if not 1 <= maximum <= 4096:
        raise ValueError("PLATFORM_MAX_OUTPUT_TOKENS must be between 1 and 4096")
    allowed = {name.strip() for name in os.getenv("PLATFORM_ALLOWED_MODELS", "").split(",") if name.strip()}
    for config in configs:
        for agent in config.get("agents", []):
            if agent.get("runtime") == "external" or agent.get("type") == "human":
                raise ValueError("Hosted experiments currently support in-process or local-process agents")
            extra = agent.get("extra") or {}
            if not isinstance(extra, dict):
                raise ValueError("Agent extra options must be a mapping")
            if agent.get("api_key") or extra.get("api_key"):
                raise ValueError("Provider credentials must come from the platform worker environment")
            if agent.get("type", "llm") != "llm":
                continue
            model = agent.get("model") or "gpt-4o-mini"
            if allowed and model not in allowed:
                raise ValueError(f"Model {model!r} is not enabled for platform-funded experiments")
            agent["model"] = model
            requested = agent.get("max_tokens")
            if requested is None:
                requested = maximum
            if isinstance(requested, bool) or not isinstance(requested, int) or not 1 <= requested <= maximum:
                raise ValueError(f"Agent max_tokens must be between 1 and {maximum}")
            # Additional provider fields must not bypass the explicit cap.
            for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                if key in extra:
                    value = extra[key]
                    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                        raise ValueError(f"Agent {key} must be between 1 and {maximum}")
            agent["max_tokens"] = requested
    return {"max_output_tokens_per_request": maximum, "allowed_models": sorted(allowed) or None,
            "max_launch_episodes": 100, "max_concurrent_launches_per_user": 2,
            "max_launch_duration_seconds": 3600}
