"""Factory for LLM clients with provider auto-detection from model name."""

import os

from a2a_engine.llm.api import LLMClient
from a2a_engine.llm.credentials import ModelCredential
from a2a_engine.llm.providers import LLM_PROVIDERS


def detect_provider(model: str, api_format: str | None = None) -> str | None:
    """Resolve explicit Vertex routing first, then infer from the model name."""
    vertex_provider = {
        "vertexai": "gemini_vertexai",
        "vertexai_anthropic": "claude_vertexai",
        "vertexai_openai": "vertexai_openai",
    }.get(api_format)
    if vertex_provider:
        return vertex_provider
    m = model.lower()
    if m == "gemini-3-flash-preview" or m.startswith("publishers/google/models/gemini-3"):
        return "gemini_vertexai"
    if m == "claude-sonnet-4-6" or ("claude" in m and "@" in m):
        return "claude_vertexai"
    if m.startswith("meta/") and "maas" in m:
        return "vertexai_openai"
    if "/" in model:
        return "openrouter"
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m or m.startswith(("o1-", "o1_", "o3-", "o3_", "o4-", "o4_")):
        return "openai"
    if "gemini" in m:
        return "gemini"
    if "llama" in m or "mistral" in m or "qwen" in m:
        return "ollama"
    return None


# Providers that authenticate with Google Application Default Credentials
# rather than an API key in the release.
ADC_PROVIDERS = frozenset({"gemini_vertexai", "claude_vertexai", "vertexai_openai"})

PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def env_var_for_provider(provider: str) -> str:
    """The release variable a provider reads its key from, if it uses one.

    Exposed so a caller can report *which* variable is missing instead of only
    that some credential was absent.
    """
    if provider in ADC_PROVIDERS:
        return ""
    return PROVIDER_ENV_VARS.get(provider, "")


def get_api_key_for_provider(provider: str, credential: str | None = None) -> str:
    """Read a named credential, or retain legacy provider credential lookup."""
    if credential is not None:
        selected = ModelCredential(credential)
        return os.environ.get(selected.env_var, "") if selected.env_var else ""
    key = os.environ.get(env_var_for_provider(provider), "")
    if not key and provider == "anthropic":
        # An `ant auth` OAuth access token; the request layer sends it as a bearer token.
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    return key


def provider_for_config(cfg: dict) -> str | None:
    """The credential selector fixes both routing and authentication."""
    if "source" in cfg:
        raise ValueError("source was replaced by the single credential selector")
    credential, api_format = cfg.get("credential"), cfg.get("api_format")
    if credential is None:
        return detect_provider(cfg.get("model") or "gpt-4o-mini", api_format)
    selected = ModelCredential(credential)
    if cfg.get("api_key") is not None:
        raise ValueError("credential selects runtime authentication; inline api_key is not allowed")
    if selected is ModelCredential.VERTEXAI_ADC:
        provider = {"vertexai": "gemini_vertexai", "vertexai_anthropic": "claude_vertexai",
                    "vertexai_openai": "vertexai_openai"}.get(api_format)
        if not provider:
            raise ValueError("credential vertexai_adc requires an explicit Vertex api_format")
        return provider
    if any(cfg.get(key) is not None for key in ("adc_file", "vertex_adc_file", "gcp_project", "gcp_location")):
        raise ValueError("Google ADC settings require credential vertexai_adc")
    provider = selected.provider
    expected = LLM_PROVIDERS[provider]["api_format"]
    if api_format and api_format != expected:
        raise ValueError(f"credential {selected.value!r} requires api_format {expected!r}")
    return provider


def credential_env_for_config(cfg: dict) -> str | None:
    """Derive the environment variable from the single credential selector."""
    selected = cfg.get("credential")
    return ModelCredential(selected).env_var if selected is not None else None


def make_llm_client(cfg: dict) -> LLMClient:
    """Build an ``LLMClient`` from a config dict.

    Auto-fills api_base, api_format, and api_key from LLM_PROVIDERS / env when
    not explicitly set. Required: ``model``.
    """
    model = cfg.get("model", "gpt-4o-mini")
    api_base = cfg.get("api_base")
    api_key = cfg.get("api_key")
    api_format = cfg.get("api_format")

    provider = provider_for_config(cfg)
    if provider and provider in LLM_PROVIDERS:
        p = LLM_PROVIDERS[provider]
        api_base = api_base or p["api_base"]
        api_format = api_format or p["api_format"]
        if not api_key:
            api_key = get_api_key_for_provider(provider, credential_env_for_config(cfg))

    from a2a_engine.modalities import validate_seat
    validate_seat({**cfg, "api_format": api_format or "openai"}, cfg.get("required_modalities", ["text"]))
    return LLMClient(
        api_format=api_format or "openai",
        api_base=api_base or "",
        api_key=api_key or "",
        model=model,
        temperature=cfg.get("temperature", 0.7),
        max_tokens=cfg.get("max_tokens"),
        vertex_adc_file=cfg.get("vertex_adc_file") or cfg.get("adc_file"),
        gcp_project=cfg.get("gcp_project"),
        gcp_location=cfg.get("gcp_location"),
        extra=cfg.get("extra", {}),
        capabilities=cfg.get("capabilities") or {},
        asset_root=cfg.get("asset_root"),
    )
