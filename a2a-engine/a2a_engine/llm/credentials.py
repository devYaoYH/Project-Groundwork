"""Mutually exclusive model access methods; values are selectors, never secrets."""

from enum import StrEnum


class ModelCredential(StrEnum):
    ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
    ANTHROPIC_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
    VERTEXAI_ADC = "vertexai_adc"
    OPENAI_API_KEY = "OPENAI_API_KEY"
    OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
    GOOGLE_API_KEY = "GOOGLE_API_KEY"
    OLLAMA_API_KEY = "OLLAMA_API_KEY"

    @property
    def provider(self) -> str | None:
        return {
            self.ANTHROPIC_API_KEY: "anthropic",
            self.ANTHROPIC_AUTH_TOKEN: "anthropic",
            self.OPENAI_API_KEY: "openai",
            self.OPENROUTER_API_KEY: "openrouter",
            self.GOOGLE_API_KEY: "gemini",
            self.OLLAMA_API_KEY: "ollama",
        }.get(self)

    @property
    def env_var(self) -> str | None:
        return None if self is self.VERTEXAI_ADC else self.value
