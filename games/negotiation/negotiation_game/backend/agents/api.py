"""Compatibility adapters for legacy Negotiation imports.

Provider transports live exclusively in :mod:`a2a_engine.llm.api`. Keep the
historical Anthropic streaming prompt suffix here because it is a Negotiation
prompt choice, not a provider requirement.
"""

from a2a_engine.llm import api as _api
from a2a_engine.llm.api import call_llm_oneshot, call_llm_oneshot_with_thinking
from negotiation_game.backend.agents.prompts import anthropic_streaming_messages

__all__ = ["call_llm_streaming", "call_llm_oneshot", "call_llm_oneshot_with_thinking"]


def call_llm_streaming(api_format, api_base, api_key, model, messages,
                       max_tokens, temperature, on_first_chunk=None, loop=None,
                       thinking_config=None, vertex_adc_file=None,
                       gcp_project=None, gcp_location=None, timeout=120):
    """Delegate transport while preserving the legacy streaming prompt."""
    return _api.call_llm_streaming(
        api_format=api_format, api_base=api_base, api_key=api_key, model=model,
        messages=anthropic_streaming_messages(messages, api_format),
        max_tokens=max_tokens, temperature=temperature,
        on_first_chunk=on_first_chunk, loop=loop, thinking_config=thinking_config,
        vertex_adc_file=vertex_adc_file, gcp_project=gcp_project,
        gcp_location=gcp_location, timeout=timeout,
    )
