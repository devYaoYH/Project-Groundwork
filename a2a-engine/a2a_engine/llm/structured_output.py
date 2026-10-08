"""Recover the existing structured action format without a game dependency."""

import json
import re

try:
    from json_repair import repair_json
except ImportError:  # pragma: no cover
    repair_json = None


def parse_actions(text: str, *, repair=repair_json) -> tuple[list[dict], str | None]:
    if not text:
        return [], None

    def extract(parsed):
        if isinstance(parsed, dict) and "actions" in parsed:
            actions = parsed["actions"]
            if isinstance(actions, list):
                return [a for a in actions if isinstance(a, dict)], parsed.get("thinking") or None
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict)], None
        return None

    stripped = re.sub(r"```(?:json)?\s*\n?(.*?)\n?\s*```", r"\1", text, flags=re.DOTALL).strip()
    for candidate in (text.strip(), stripped):
        try:
            result = extract(json.loads(candidate))
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass
    if repair is not None:
        try:
            result = extract(repair(stripped, return_objects=True))
            if result is not None:
                return result
        except Exception:
            pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, stripped, re.DOTALL)
        if match and repair is not None:
            try:
                result = extract(repair(match.group(), return_objects=True))
                if result is not None:
                    return result
            except Exception:
                pass
    return [], None


def parse_reflection_deltas(text, num_slots, *, repair=repair_json):
    deltas = [None for _ in range(num_slots)]
    if text is None or not text.strip():
        return deltas
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            parsed = repair(stripped, return_objects=True) if repair else None
        except Exception:
            parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("states"), list):
        parsed = parsed["states"]
    if isinstance(parsed, dict) and isinstance(parsed.get("deltas"), list):
        parsed = parsed["deltas"]
    if isinstance(parsed, list):
        for index, value in enumerate(parsed[:num_slots]):
            try:
                delta = int(value)
            except (TypeError, ValueError):
                continue
            deltas[index] = max(-3, min(3, delta))
        return deltas
    for index, value in enumerate(re.findall(r"(?<!\d)-?[0-3](?!\d)", stripped)[:num_slots]):
        deltas[index] = int(value)
    return deltas


def supports_logprobs(model):
    explicit = getattr(model, "supports_logprobs", None)
    extra = getattr(model, "extra", None)
    if explicit is None and isinstance(extra, dict):
        explicit = extra.get("supports_logprobs")
    if explicit is not None:
        return bool(explicit)
    api_format = str(getattr(model, "api_format", "") or "").lower()
    name = str(getattr(model, "model", "") or "").lower()
    if api_format in {"anthropic", "vertexai_anthropic", "vertexai_openai", "gemini", "vertexai"} or "gemini" in name:
        return False
    return api_format == "openai"


def extract_binary_logprobs(raw, num_slots):
    """Best-effort extraction for 0/1 token logprobs in generated slot order."""
    found = []

    def token_key(token):
        text = str(token).strip()
        return text if text in {"0", "1"} else None

    def collect(obj):
        if isinstance(obj, dict):
            top = obj.get("top_logprobs")
            if isinstance(top, list):
                local = {"0": None, "1": None, "_top_logprob_floor": None}
                key = token_key(obj.get("token"))
                if key is not None and isinstance(obj.get("logprob"), (int, float)):
                    local[key] = float(obj["logprob"])
                for entry in top:
                    if not isinstance(entry, dict):
                        continue
                    if isinstance(entry.get("logprob"), (int, float)):
                        floor = local["_top_logprob_floor"]
                        value = float(entry["logprob"])
                        local["_top_logprob_floor"] = value if floor is None else min(floor, value)
                    key = token_key(entry.get("token"))
                    if key is not None and isinstance(entry.get("logprob"), (int, float)):
                        local[key] = float(entry["logprob"])
                if local["0"] is not None or local["1"] is not None:
                    found.append(local)
            for value in obj.values():
                collect(value)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)

    collect(raw)
    found = found[:num_slots]
    while len(found) < num_slots:
        found.append({"0": None, "1": None, "_top_logprob_floor": None})
    return found
