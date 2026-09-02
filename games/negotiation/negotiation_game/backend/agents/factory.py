"""
Agent factory for creating agent instances from configuration dictionaries.

Handles provider auto-detection and API credential loading from environment.
"""

import os
from negotiation_game.backend.agents.heuristic import HeuristicAgent
from negotiation_game.backend.agents.llm import LLMAgentStdlib
from negotiation_game.backend.agents.human import HumanAgent
from negotiation_game.backend.defaults import LLM_PROVIDERS


def detect_provider(model: str) -> str | None:
    """Detect LLM provider from model name.

    Args:
        model: Model identifier (e.g., "claude-3-haiku-20240307", "gpt-4o-mini")

    Returns:
        Provider key matching config.LLM_PROVIDERS, or None if not detected.

    Examples:
        >>> detect_provider("claude-3-haiku-20240307")
        'anthropic'
        >>> detect_provider("gpt-4o-mini")
        'openai'
        >>> detect_provider("gemini-2.0-flash")
        'gemini'
        >>> detect_provider("llama3")
        'ollama'
        >>> detect_provider("meta-llama/llama-3.3-70b-instruct")
        'openrouter'
    """
    model_lower = model.lower()

    if (
        model_lower == "gemini-3-flash-preview"
        or model_lower.startswith("publishers/google/models/gemini-")
    ):
        return "gemini_vertexai"
    if (
        model_lower == "llama-4-maverick-17b-128e-instruct-maas"
        or model_lower == "meta/llama-4-maverick-17b-128e-instruct-maas"
    ):
        return "llama_vertexai"

    # OpenRouter: Check FIRST (before substring checks to avoid false positives)
    # OpenRouter model IDs use provider/model-name format (e.g., meta-llama/llama-3.3-70b-instruct)
    if "/" in model:
        return "openrouter"

    if "claude" in model_lower or "anthropic" in model_lower:
        return "claude_vertexai"
    # OpenAI models: gpt-*, o1-*, o3-*, o4-*, etc.
    if "gpt" in model_lower or "openai" in model_lower or model_lower.startswith(("o1-", "o1_", "o3-", "o3_", "o4-", "o4_")):
        return "openai"
    if "gemini" in model_lower:
        return "gemini"
    if "llama" in model_lower or "mistral" in model_lower or "qwen" in model_lower:
        return "ollama"
    return None


def get_api_key_for_provider(provider: str) -> str:
    """Get API key from environment variables for a given provider.

    Args:
        provider: Provider key from config.LLM_PROVIDERS

    Returns:
        API key from environment, or empty string if not set.

    Environment variables checked:
        - anthropic: ANTHROPIC_API_KEY
        - openai: OPENAI_API_KEY
        - gemini: GOOGLE_API_KEY
        - ollama: OLLAMA_API_KEY (usually not needed)
        - openrouter: OPENROUTER_API_KEY
    """
    if provider in {"gemini_vertexai", "claude_vertexai", "llama_vertexai"}:
        return ""

    env_keys = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "ollama": "OLLAMA_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    env_var = env_keys.get(provider, "")
    return os.environ.get(env_var, "")


def make_agent(cfg: dict):
    """Factory function to create an agent from a config dict.

    For LLM agents, automatically detects provider from model name and fills in
    api_base, api_key, and api_format from config.LLM_PROVIDERS and environment.

    Args:
        cfg: Agent configuration dict with at minimum {"type": "llm"|"human"|"heuristic"|"random"}
             For LLM agents, should include {"model": "model-name"}.
             Can optionally override {"api_base", "api_key", "api_format", "temperature"}.

    Returns:
        Agent instance implementing the AgentInterface.

    Examples:
        >>> # Minimal config with auto-detection
        >>> agent = make_agent({"type": "llm", "model": "claude-3-haiku-20240307"})
        >>> # Will use ANTHROPIC_API_KEY from environment

        >>> # Explicit configuration
        >>> agent = make_agent({
        ...     "type": "llm",
        ...     "model": "gpt-4o-mini",
        ...     "api_base": "https://api.openai.com/v1",
        ...     "api_key": "sk-...",
        ...     "api_format": "openai",
        ... })
    """
    t = cfg.get("type", "random")
    if t == "human":
        return HumanAgent()
    if t == "llm":
        model = cfg.get("model", "gpt-4o-mini")
        api_base = cfg.get("api_base")
        api_key = cfg.get("api_key")
        api_format = cfg.get("api_format")

        # Auto-detect provider if not explicitly configured
        provider = detect_provider(model)
        if provider and provider in LLM_PROVIDERS:
            provider_cfg = LLM_PROVIDERS[provider]
            api_base = api_base or provider_cfg["api_base"]
            api_format = api_format or provider_cfg["api_format"]
            if not api_key:
                api_key = get_api_key_for_provider(provider)
            vertex_adc_file = cfg.get("vertex_adc_file") or cfg.get("adc_file") or provider_cfg.get("vertex_adc_file")
            gcp_project = cfg.get("gcp_project") or provider_cfg.get("gcp_project")
            gcp_location = cfg.get("gcp_location") or provider_cfg.get("gcp_location")
        else:
            vertex_adc_file = cfg.get("vertex_adc_file") or cfg.get("adc_file")
            gcp_project = cfg.get("gcp_project")
            gcp_location = cfg.get("gcp_location")

        return LLMAgentStdlib(
            api_base=api_base or "",
            api_key=api_key or "",
            model=model,
            temperature=cfg.get("temperature", 0.7),
            api_format=api_format or "openai",
            max_tokens=cfg.get("max_tokens"),
            vertex_adc_file=vertex_adc_file,
            gcp_project=gcp_project,
            gcp_location=gcp_location,
        )
    return HeuristicAgent()
