"""Compatibility exports; model API parameters are owned by a2a-engine."""

from a2a_engine.llm.model_config import (
    DEFAULT_CONFIG, MODEL_REGISTRY, ModelConfig, get_model_config,
)

__all__ = ["DEFAULT_CONFIG", "MODEL_REGISTRY", "ModelConfig", "get_model_config"]
