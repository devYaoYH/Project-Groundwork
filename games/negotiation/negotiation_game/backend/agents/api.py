"""
Unified LLM API helpers.

Centralises payload construction, header building, SSE parsing and response
extraction for OpenAI-compatible and Anthropic APIs so that every call site
uses the same logic and honours ``model_config.py``.
"""

import asyncio
import json
import logging
import ssl
import time
import urllib.error
import urllib.request

try:
    import certifi
except ImportError:
    certifi = None

try:
    from google import genai as _google_genai
    from google.auth import default as _gcp_default
    from google.auth import load_credentials_from_file as _gcp_load_credentials_from_file
    from google.auth.transport.requests import Request as _GcpRequest
except ImportError:
    _google_genai = None
    _gcp_default = None
    _gcp_load_credentials_from_file = None
    _GcpRequest = None

from negotiation_game.backend.agents.model_config import get_model_config
from negotiation_game.backend.agents.prompts import ANTHROPIC_JSON_SUFFIX

log = logging.getLogger("negotiation")

_ssl_ctx = ssl.create_default_context(cafile=certifi.where()) if certifi else None


# ---------------------------------------------------------------------------
# Streaming (urllib, sync – run via ``loop.run_in_executor``)
# ---------------------------------------------------------------------------

def call_llm_streaming(
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    on_first_chunk=None,
    loop=None,
    thinking_config: dict | None = None,
    vertex_adc_file: str | None = None,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
    timeout: int = 120,
) -> dict:
    """Make a streaming LLM call and return the assembled result.

    Parameters
    ----------
    api_format : ``"openai"`` or ``"anthropic"``
    on_first_chunk : optional async callback fired on the first content token
    loop : asyncio event loop (needed to schedule *on_first_chunk*)

    Returns
    -------
    dict with keys: text, model, duration_s, prompt_tokens, completion_tokens,
    total_tokens (plus Anthropic cache fields when applicable).
    """
    if api_format == "anthropic":
        return _stream_anthropic(
            api_base, api_key, model, messages, max_tokens, temperature,
            on_first_chunk, loop, timeout=timeout,
        )
    if api_format == "vertexai_anthropic":
        return _stream_vertexai_anthropic(
            model,
            messages,
            max_tokens,
            temperature,
            on_first_chunk,
            loop,
            timeout=timeout,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
    if api_format == "vertexai":
        return _stream_vertexai(
            model,
            messages,
            max_tokens,
            temperature,
            on_first_chunk,
            loop,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
    if api_format == "vertexai_openai":
        api_base, token = _get_vertexai_openai_endpoint_and_token(
            project=gcp_project,
            location=gcp_location,
            adc_file=vertex_adc_file,
        )
        return _stream_openai(
            api_base, token, model, messages, max_tokens, temperature,
            on_first_chunk, loop, thinking_config=thinking_config, timeout=timeout,
        )
    return _stream_openai(
        api_base, api_key, model, messages, max_tokens, temperature,
        on_first_chunk, loop, thinking_config=thinking_config, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# One-shot / non-streaming (urllib, sync)
# ---------------------------------------------------------------------------

def call_llm_oneshot(
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: int = 300,
    thinking_budget: int | None = None,
    vertex_adc_file: str | None = None,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
) -> str:
    """Make a non-streaming LLM call and return the text content.

    Raises ``urllib.error.HTTPError`` on HTTP failures.
    """
    model_cfg = get_model_config(model)
    effective_max = max_tokens or model_cfg.default_max_tokens

    if api_format == "anthropic":
        return _oneshot_anthropic(
            api_base, api_key, model, messages, effective_max,
            temperature, timeout, model_cfg,
        )
    if api_format == "vertexai_anthropic":
        text, _ = _oneshot_vertexai_anthropic(
            model,
            messages,
            effective_max,
            temperature,
            timeout,
            model_cfg,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
        return text
    if api_format == "vertexai":
        text, _ = _oneshot_vertexai(
            model,
            messages,
            effective_max,
            temperature,
            thinking_budget,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
        return text
    if api_format == "vertexai_openai":
        api_base, token = _get_vertexai_openai_endpoint_and_token(
            project=gcp_project,
            location=gcp_location,
            adc_file=vertex_adc_file,
        )
        return _oneshot_openai(
            api_base, token, model, messages, effective_max,
            temperature, timeout, model_cfg,
        )
    return _oneshot_openai(
        api_base, api_key, model, messages, effective_max,
        temperature, timeout, model_cfg,
    )


def call_llm_oneshot_with_thinking(
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: int = 300,
    thinking_budget: int | None = None,
    vertex_adc_file: str | None = None,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
) -> tuple[str, str | None]:
    """Like call_llm_oneshot but also returns thinking text as the second element.

    thinking is None for non-vertexai formats or when thinking_budget is not set.
    """
    model_cfg = get_model_config(model)
    effective_max = max_tokens or model_cfg.default_max_tokens

    if api_format == "vertexai":
        return _oneshot_vertexai(
            model,
            messages,
            effective_max,
            temperature,
            thinking_budget,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
    if api_format == "vertexai_anthropic":
        return _oneshot_vertexai_anthropic(
            model,
            messages,
            effective_max,
            temperature,
            timeout,
            model_cfg,
            adc_file=vertex_adc_file,
            project=gcp_project,
            location=gcp_location,
        )
    if api_format == "vertexai_openai":
        text = call_llm_oneshot(
            api_format=api_format,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            vertex_adc_file=vertex_adc_file,
            gcp_project=gcp_project,
            gcp_location=gcp_location,
        )
        return text, None

    text = call_llm_oneshot(
        api_format=api_format, api_base=api_base, api_key=api_key, model=model,
        messages=messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )
    return text, None


# ===================================================================
# Vertex AI (google-genai SDK + ADC) helpers
# ===================================================================

def _load_vertex_credentials(adc_file: str | None = None):
    if adc_file:
        if _gcp_load_credentials_from_file is None:
            raise RuntimeError("google-auth not installed; cannot load Vertex AI ADC file")
        return _gcp_load_credentials_from_file(
            adc_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    if _gcp_default is None:
        raise RuntimeError("google-auth not installed; cannot use Vertex AI ADC")
    return _gcp_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def _get_vertexai_client(
    *,
    project: str | None = None,
    location: str | None = None,
    adc_file: str | None = None,
):
    if _google_genai is None:
        raise RuntimeError("google-genai package not installed. Run: uv add google-genai")
    credentials, adc_project = _load_vertex_credentials(adc_file)
    from negotiation_game.backend.defaults import LLM_PROVIDERS
    cfg = LLM_PROVIDERS.get("gemini_vertexai", {})
    gcp_project = project or cfg.get("gcp_project") or adc_project
    gcp_location = location or cfg.get("gcp_location", "global")
    if not gcp_project:
        raise RuntimeError("No GCP project resolved. Set GOOGLE_CLOUD_PROJECT or pass gcp_project.")
    return _google_genai.Client(
        vertexai=True,
        credentials=credentials,
        project=gcp_project,
        location=gcp_location,
    )


def _get_vertexai_project_location(
    provider_key: str,
    *,
    project: str | None = None,
    location: str | None = None,
    adc_file: str | None = None,
) -> tuple[str, str]:
    _credentials, adc_project = _load_vertex_credentials(adc_file)
    from negotiation_game.backend.defaults import LLM_PROVIDERS
    cfg = LLM_PROVIDERS.get(provider_key, {})
    gcp_project = project or cfg.get("gcp_project") or adc_project
    gcp_location = location or cfg.get("gcp_location", "global")
    if not gcp_project:
        raise RuntimeError("No GCP project resolved. Set GOOGLE_CLOUD_PROJECT or pass gcp_project.")
    return gcp_project, gcp_location


def _get_vertexai_access_token(adc_file: str | None = None) -> str:
    if _GcpRequest is None:
        raise RuntimeError("google-auth not installed; cannot use Vertex AI ADC")
    credentials, _ = _load_vertex_credentials(adc_file)
    credentials.refresh(_GcpRequest())
    return credentials.token


def _get_vertexai_openai_endpoint_and_token(
    *,
    project: str | None = None,
    location: str | None = None,
    adc_file: str | None = None,
) -> tuple[str, str]:
    project, location = _get_vertexai_project_location(
        "llama_vertexai",
        project=project,
        location=location,
        adc_file=adc_file,
    )
    api_base = (
        f"https://{location}-aiplatform.googleapis.com/v1beta1"
        f"/projects/{project}/locations/{location}/endpoints/openapi"
    )
    return api_base, _get_vertexai_access_token(adc_file)


def _messages_to_vertexai(messages: list[dict]) -> tuple[str, list]:
    """Split messages into (system_instruction, contents) for the Vertex AI SDK."""
    system_parts = []
    contents = []
    for m in messages:
        role = m["role"]
        text = m["content"]
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            contents.append(_google_genai.types.Content(role="model", parts=[_google_genai.types.Part(text=text)]))
        else:
            contents.append(_google_genai.types.Content(role="user", parts=[_google_genai.types.Part(text=text)]))
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _messages_to_anthropic_vertexai(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    api_messages: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})
    return "\n\n".join(system_parts), api_messages


def _anthropic_vertexai_url(project: str, location: str, model: str) -> str:
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/anthropic/models/{model}:rawPredict"
    )


def _oneshot_vertexai_anthropic(
    model: str,
    messages: list[dict],
    max_tokens: int | None,
    temperature: float | None,
    timeout: int,
    model_cfg,
    *,
    adc_file: str | None = None,
    project: str | None = None,
    location: str | None = None,
) -> tuple[str, str | None]:
    project, location = _get_vertexai_project_location(
        "claude_vertexai",
        project=project,
        location=location,
        adc_file=adc_file,
    )
    system_text, api_messages = _messages_to_anthropic_vertexai(messages)
    body: dict = {
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": max_tokens,
        "messages": api_messages,
    }
    if system_text:
        body["system"] = system_text
    if model_cfg.supports_temperature and temperature is not None:
        body["temperature"] = temperature

    url = _anthropic_vertexai_url(project, location, model)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_get_vertexai_access_token(adc_file)}",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
            log.error("VERTEX ANTHROPIC API ERROR - HTTP %d - %s", e.code, error_body)
        except Exception:
            log.error("VERTEX ANTHROPIC API ERROR - HTTP %d - (could not read error body)", e.code)
        raise

    duration_s = round(time.monotonic() - t0, 3)
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    thinking = "".join(
        b.get("thinking", "") for b in data.get("content", []) if b.get("type") == "thinking"
    ).strip() or None
    log.info("  <- vertexai_anthropic %d chars in %.1fs", len(text), duration_s)
    return text, thinking


def _stream_vertexai_anthropic(
    model: str,
    messages: list[dict],
    max_tokens: int | None,
    temperature: float | None,
    on_first_chunk=None,
    loop=None,
    timeout: int = 120,
    *,
    adc_file: str | None = None,
    project: str | None = None,
    location: str | None = None,
) -> dict:
    model_cfg = get_model_config(model)
    t0 = time.monotonic()
    text, thinking = _oneshot_vertexai_anthropic(
        model,
        messages,
        max_tokens,
        temperature,
        timeout,
        model_cfg,
        adc_file=adc_file,
        project=project,
        location=location,
    )
    if on_first_chunk and loop:
        asyncio.run_coroutine_threadsafe(on_first_chunk(), loop).result(timeout=5)
    duration_s = round(time.monotonic() - t0, 3)
    result = {
        "text": text,
        "model": model,
        "duration_s": duration_s,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "reasoning_tokens": None,
        "cached_prompt_tokens": None,
        "finish_reason": None,
    }
    if thinking:
        result["reasoning"] = thinking
    return result


def _oneshot_vertexai(model: str, messages: list[dict], max_tokens: int | None, temperature: float | None,
                      thinking_budget: int | None = None, adc_file: str | None = None,
                      project: str | None = None, location: str | None = None) -> tuple[str, str | None]:
    """Returns (text, thinking) — thinking is None when thinking_budget is not set."""
    client = _get_vertexai_client(project=project, location=location, adc_file=adc_file)
    system_instruction, contents = _messages_to_vertexai(messages)

    config_kwargs = {}
    if max_tokens is not None:
        config_kwargs["max_output_tokens"] = max_tokens
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = _google_genai.types.ThinkingConfig(
            include_thoughts=True,
            thinking_budget=thinking_budget,
        )
    gen_config = _google_genai.types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    t0 = time.monotonic()
    response = client.models.generate_content(model=model, contents=contents, config=gen_config)
    duration_s = round(time.monotonic() - t0, 3)
    parts = response.candidates[0].content.parts if response.candidates and response.candidates[0].content else []
    parts = parts or []
    text = "".join(p.text for p in parts if p.text and not p.thought).strip()
    thinking = "".join(p.text for p in parts if p.text and p.thought).strip() or None
    log.info("  <- vertexai %d chars in %.1fs", len(text), duration_s)
    return text, thinking


def _stream_vertexai(model: str, messages: list[dict], max_tokens: int | None, temperature: float | None,
                     on_first_chunk=None, loop=None, adc_file: str | None = None,
                     project: str | None = None, location: str | None = None) -> dict:
    client = _get_vertexai_client(project=project, location=location, adc_file=adc_file)
    system_instruction, contents = _messages_to_vertexai(messages)

    config_kwargs = {}
    if max_tokens is not None:
        config_kwargs["max_output_tokens"] = max_tokens
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    gen_config = _google_genai.types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    t0 = time.monotonic()
    chunks = []
    first = True
    for chunk in client.models.generate_content_stream(model=model, contents=contents, config=gen_config):
        if chunk.text:
            if first:
                first = False
                if on_first_chunk and loop:
                    asyncio.run_coroutine_threadsafe(on_first_chunk(), loop).result(timeout=5)
            chunks.append(chunk.text)

    duration_s = round(time.monotonic() - t0, 3)
    text = "".join(chunks).strip()
    log.info("  <- vertexai stream %d chars in %.1fs", len(text), duration_s)
    return {
        "text": text,
        "model": model,
        "duration_s": duration_s,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "reasoning_tokens": None,
        "cached_prompt_tokens": None,
        "finish_reason": None,
    }


# ===================================================================
# Internal helpers
# ===================================================================

def _stream_openai(api_base, api_key, model, messages, max_tokens,
                   temperature, on_first_chunk, loop, thinking_config=None, timeout=120):
    model_cfg = get_model_config(model)
    url = f"{api_base.rstrip('/')}/chat/completions"
    log.info("  POST %s model=%s msgs=%d (streaming)", url, model, len(messages))

    payload_dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        model_cfg.max_tokens_param: max_tokens,
    }
    if model_cfg.supports_temperature:
        payload_dict["temperature"] = temperature
    if thinking_config:
        payload_dict["thinking_config"] = thinking_config

    req = urllib.request.Request(
        url,
        data=json.dumps(payload_dict).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
    )
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx)
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
            log.error("OpenAI API ERROR - HTTP %d - %s", e.code, error_body)
        except Exception:
            log.error("OpenAI API ERROR - HTTP %d - (could not read error body)", e.code)
        raise
    except urllib.error.URLError as e:
        log.error("OpenAI API network error: %s", e.reason)
        raise

    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    usage: dict = {}
    model_name = model
    first_chunk = True
    finish_reason = None
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            chunk = json.loads(data_str)
            if first_chunk:
                first_chunk = False
                if on_first_chunk and loop:
                    asyncio.run_coroutine_threadsafe(on_first_chunk(), loop).result(timeout=5)
            model_name = chunk.get("model", model_name)
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    chunks.append(content)
                # Extract reasoning content for o-series models (o1, o3, o4)
                reasoning_content = delta.get("reasoning_content")
                if reasoning_content:
                    reasoning_chunks.append(reasoning_content)
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
    finally:
        resp.close()

    duration_s = round(time.monotonic() - t0, 3)
    text = "".join(chunks).strip()
    reasoning_text = "".join(reasoning_chunks).strip() if reasoning_chunks else None
    log.info("  <- %d chars, %d tokens in %.1fs%s%s", len(text), usage.get("total_tokens", 0), duration_s,
             f" (+ {len(reasoning_text)} thinking chars)" if reasoning_text else "",
             f" [stop={finish_reason}]" if finish_reason else "")
    # Extract detailed token breakdowns (OpenAI reasoning models)
    completion_details = usage.get("completion_tokens_details", {}) or {}
    prompt_details = usage.get("prompt_tokens_details", {}) or {}

    result = {
        "text": text,
        "model": model_name,
        "duration_s": duration_s,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "cached_prompt_tokens": prompt_details.get("cached_tokens"),
        "finish_reason": finish_reason,
    }
    if reasoning_text:
        result["reasoning"] = reasoning_text
    return result


def _stream_anthropic(api_base, api_key, model, messages, max_tokens,
                      temperature, on_first_chunk, loop, timeout=120):
    model_cfg = get_model_config(model)
    url = f"{api_base.rstrip('/')}/messages"
    log.info("  POST %s model=%s msgs=%d (streaming)", url, model, len(messages))

    # Anthropic expects system prompt separate from messages
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"] + ANTHROPIC_JSON_SUFFIX
        else:
            api_messages.append(m)

    # Add cache_control breakpoint to the last message
    cached_msg_idx = None
    if api_messages:
        cached_msg_idx = len(api_messages) - 1
        original_content = api_messages[cached_msg_idx]["content"]
        api_messages[cached_msg_idx] = {
            **api_messages[cached_msg_idx],
            "content": [{"type": "text", "text": original_content,
                         "cache_control": {"type": "ephemeral"}}],
        }

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": api_messages,
        "stream": True,
    }
    if model_cfg.supports_temperature:
        body["temperature"] = temperature
    if system_text:
        body["system"] = [{"type": "text", "text": system_text,
                           "cache_control": {"type": "ephemeral"}}]

    payload = json.dumps(body).encode()

    # Restore original message content to keep caller's list clean
    if cached_msg_idx is not None:
        api_messages[cached_msg_idx]["content"] = original_content

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx)
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="replace")
            log.error("ANTHROPIC API ERROR - HTTP %d - %s", e.code, error_body)
        except Exception:
            log.error("ANTHROPIC API ERROR - HTTP %d - (could not read error body)", e.code)
        raise
    except urllib.error.URLError as e:
        log.error("Anthropic API network error: %s", e.reason)
        raise

    chunks: list[str] = []
    thinking_chunks: list[str] = []
    usage: dict = {}
    model_name = model
    first_chunk = True
    current_block_type = None  # Track whether we're in a thinking or text block
    stop_reason = None
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            try:
                event_data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            event_type = event_data.get("type", "")

            if event_type == "message_start":
                msg = event_data.get("message", {})
                model_name = msg.get("model", model_name)
                usage = msg.get("usage", {})
            elif event_type == "content_block_start":
                # Track block type (thinking vs text)
                content_block = event_data.get("content_block", {})
                current_block_type = content_block.get("type", "text")
            elif event_type == "content_block_delta":
                delta = event_data.get("delta", {})
                text_chunk = delta.get("text", "")
                if text_chunk:
                    if first_chunk:
                        first_chunk = False
                        if on_first_chunk and loop:
                            asyncio.run_coroutine_threadsafe(on_first_chunk(), loop).result(timeout=5)
                    # Route to thinking or regular chunks based on block type
                    if current_block_type == "thinking":
                        thinking_chunks.append(text_chunk)
                    else:
                        chunks.append(text_chunk)
            elif event_type == "content_block_stop":
                current_block_type = None  # Reset block type
            elif event_type == "message_delta":
                delta_data = event_data.get("delta", {})
                sr = delta_data.get("stop_reason")
                if sr:
                    stop_reason = sr
                delta_usage = event_data.get("usage", {})
                if delta_usage:
                    usage.update(delta_usage)
            elif event_type == "message_stop":
                break
    finally:
        resp.close()

    duration_s = round(time.monotonic() - t0, 3)
    text = "".join(chunks).strip()
    thinking_text = "".join(thinking_chunks).strip() if thinking_chunks else None
    cache_created = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    log.info("  <- %d chars, %d tokens in %.1fs (cache: created=%d read=%d)%s%s",
             len(text), usage.get("output_tokens", 0), duration_s, cache_created, cache_read,
             f" + thinking ({len(thinking_text)} chars)" if thinking_text else "",
             f" [stop={stop_reason}]" if stop_reason else "")
    result = {
        "text": text,
        "model": model_name,
        "duration_s": duration_s,
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) or None,
        "reasoning_tokens": None,  # Anthropic doesn't break out thinking tokens separately
        "cached_prompt_tokens": cache_read,
        "cache_creation_input_tokens": cache_created,
        "cache_read_input_tokens": cache_read,
        "finish_reason": stop_reason,  # "end_turn", "max_tokens", "stop_sequence", etc.
    }
    if thinking_text:
        result["reasoning"] = thinking_text
    return result


# ---------------------------------------------------------------------------
# One-shot helpers
# ---------------------------------------------------------------------------

def _oneshot_anthropic(api_base, api_key, model, messages, max_tokens,
                       temperature, timeout, model_cfg):
    url = f"{api_base.rstrip('/')}/messages"

    # Anthropic expects system prompt separate from messages
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            api_messages.append(m)

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": api_messages,
    }
    if system_text:
        body["system"] = system_text
    if model_cfg.supports_temperature and temperature is not None:
        body["temperature"] = temperature
    payload = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
        data = json.loads(resp.read())
    return "".join(
        b["text"] for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


def _oneshot_openai(api_base, api_key, model, messages, max_tokens,
                    temperature, timeout, model_cfg):
    url = f"{api_base.rstrip('/')}/chat/completions"
    body: dict = {
        "model": model,
        model_cfg.max_tokens_param: max_tokens,
        "messages": messages,
    }
    if model_cfg.supports_temperature and temperature is not None:
        body["temperature"] = temperature
    payload = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
        raw_body = resp.read()
    log.info("_call_llm response (%d bytes): %s", len(raw_body), raw_body[:500])
    data = json.loads(raw_body)
    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason")

    if message.get("refusal"):
        log.warning("_call_llm model refused: %s", message["refusal"])
        return ""

    content = message.get("content")
    reasoning = message.get("reasoning", "")

    if not content:
        # For reasoning models (minimax, deepseek-r1), check if hit length limit during reasoning
        if finish_reason == "length" and reasoning:
            log.error("_call_llm model hit token limit during reasoning phase (%d reasoning chars, 0 output chars). "
                     "Increase max_tokens or simplify the task. Response: %s",
                     len(reasoning), json.dumps(data)[:500])
        else:
            log.warning("_call_llm empty content in response: %s", json.dumps(data)[:500])
        return ""

    return content.strip()
