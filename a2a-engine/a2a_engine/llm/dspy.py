"""Optional DSPy adapter: prompt optimization uses the engine's LLM transport.

Import this module only in workflows that install DSPy. DSPy supplies prompt
formatting, history, callbacks and caching; it never selects a provider transport.
"""

import asyncio
from types import SimpleNamespace

from dspy import BaseLM, settings
from dspy.clients.cache import request_cache

from a2a_engine.llm import api
from a2a_engine.llm.factory import get_api_key_for_provider, make_llm_client
from a2a_engine.llm.providers import LLM_PROVIDERS
from a2a_engine.llm.retry import RetryPolicy, call_with_retry


def _completion(*, request, num_retries):
    request = dict(request)
    request.pop("rollout_id", None)  # DSPy cache identity, not a provider option.
    result = call_with_retry(
        lambda: api.call_llm_oneshot_full(**request),
        RetryPolicy(max_attempts=num_retries + 1),
    )
    raw = result["response"]
    usage = raw.get("usage", {})
    if request["api_format"] == "vertexai":
        vertex_usage = raw.get("usage_metadata", {})
        usage = {"prompt_tokens": vertex_usage.get("prompt_token_count", 0),
                 "completion_tokens": vertex_usage.get("candidates_token_count", 0),
                 "total_tokens": vertex_usage.get("total_token_count", 0)}
    elif request["api_format"] in {"anthropic", "vertexai_anthropic"}:
        usage = {"prompt_tokens": usage.get("input_tokens", 0),
                 "completion_tokens": usage.get("output_tokens", 0)}
        usage["total_tokens"] = sum(usage.values())
    # BaseLM consumes the chat-completion shape and maintains DSPy's history.
    return SimpleNamespace(
        model=raw.get("model", request["model"]), usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content=result["text"]))],
    )


_cached_completion = request_cache(
    cache_arg_name="request", ignored_args_for_cache_key=["api_key"],
)(_completion)


class EngineLM(BaseLM):
    """Text-only DSPy LM for the repository's proposer and reflection workflows.

    Accepts their existing LiteLLM provider prefixes and Vertex settings. Unknown
    generation options fail explicitly instead of being silently discarded.
    """

    def __init__(self, model, *, temperature=0.0, max_tokens=1000, cache=True,
                 vertex_project=None, vertex_location=None, num_retries=3, **config):
        super().__init__(model, temperature=temperature, max_tokens=max_tokens, cache=cache)
        self.num_retries = num_retries
        provider, separator, provider_model = model.partition("/")
        if separator and provider == "vertex_ai":
            provider = "claude_vertexai" if provider_model.startswith("claude") else "gemini_vertexai"
        elif separator and provider not in LLM_PROVIDERS:
            raise ValueError(f"Unsupported DSPy provider prefix: {provider}")
        if separator:
            preset = LLM_PROVIDERS[provider]
            defaults = {"api_format": preset["api_format"], "api_base": preset["api_base"]}
            if config.get("credential") is None:
                defaults["api_key"] = get_api_key_for_provider(provider)
            config = {**defaults, **config}
            model = provider_model
        self.engine_config = {**config, "model": model}
        if vertex_project:
            self.engine_config["gcp_project"] = vertex_project
        if vertex_location:
            self.engine_config["gcp_location"] = vertex_location

    def forward(self, prompt=None, messages=None, **kwargs):
        options = {**self.kwargs, **kwargs}
        use_cache = options.pop("cache", self.cache)
        n = options.pop("n", 1)
        if n != 1:
            raise ValueError("EngineLM supports one completion per request")
        unsupported = options.keys() - {"temperature", "max_tokens", "rollout_id"}
        if unsupported:
            raise ValueError(f"Unsupported EngineLM generation options: {sorted(unsupported)}")
        rollout_id = options.pop("rollout_id", None)
        client = make_llm_client({**self.engine_config, **options})
        request = dict(
            api_format=client.api_format, api_base=client.api_base, api_key=client.api_key,
            model=client.model, messages=messages or [{"role": "user", "content": prompt or ""}],
            max_tokens=client.max_tokens, temperature=client.temperature,
            vertex_adc_file=client.vertex_adc_file,
            gcp_project=client.gcp_project, gcp_location=client.gcp_location,
            rollout_id=rollout_id,
        )
        completion = _cached_completion if use_cache else _completion
        response = completion(request=request, num_retries=self.num_retries)
        if not getattr(response, "cache_hit", False) and settings.usage_tracker:
            settings.usage_tracker.add_usage(self.model, response.usage)
        return response

    async def aforward(self, prompt=None, messages=None, **kwargs):
        return await asyncio.to_thread(self.forward, prompt=prompt, messages=messages, **kwargs)
