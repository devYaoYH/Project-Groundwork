"""Explicit model, adapter and transport admission before provider spend."""
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class ModalityCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    input: set[str] = Field(default_factory=lambda: {"text"})
    # Required for non-text support: pinned documentation or deployment contract.
    evidence: str | None = None

    @field_serializer("input")
    def stable_inputs(self, value):
        return sorted(value)

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value):
        if value is not None and not value.strip():
            raise ValueError("capability evidence must not be blank")
        return value


ADAPTER_INPUTS = {key: {"text", "image"} for key in
                  ("openai", "anthropic", "vertexai", "vertexai_anthropic", "vertexai_openai")}
TRANSPORT_INPUTS = {"in_process": {"text", "image"}, "local_process": {"text", "image"},
                    "external": {"text", "image"}}


def require_modalities(required, capabilities, *, api_format=None, runtime="in_process", llm=True):
    required = set(required)
    caps = ModalityCapabilities.model_validate(capabilities or {})
    missing = required - caps.input
    if missing or (required - {"text"} and not caps.evidence):
        raise ValueError(f"input modalities {sorted(required)} require explicit verified capabilities; "
                         f"missing {sorted(missing)} or capability evidence")
    if not required <= TRANSPORT_INPUTS.get(runtime, {"text"}):
        raise ValueError(f"transport {runtime!r} does not support input modalities {sorted(required)}")
    if llm and not required <= ADAPTER_INPUTS.get(api_format, {"text"}):
        raise ValueError(f"provider adapter {api_format!r} does not support input modalities {sorted(required)}")


def validate_seat(spec, required):
    from .llm.factory import provider_for_config
    provider_for_config(spec)
    api_format = spec.get("api_format")
    if api_format is None and spec.get("model"):
        from .llm.factory import provider_for_config
        from .llm.providers import LLM_PROVIDERS
        api_format = LLM_PROVIDERS.get(provider_for_config(spec), {}).get("api_format")
    require_modalities(required, spec.get("capabilities"), api_format=api_format,
                       runtime=spec.get("runtime", "in_process"), llm=spec.get("type", "llm") == "llm")


def validate_config(config):
    requirements = set(config.get("required_modalities", ["text"]))
    declaration = None
    if config.get("environment_id"):
        from .registry import get_environment_spec
        try:
            declaration = get_environment_spec(config["environment_id"]).declaration
        except KeyError:
            pass
    if declaration is not None:
        requirements |= declaration.required_modalities
    possible_requirements = requirements | (set().union(*(role.required_modalities for role in declaration.roles))
                                            if declaration is not None else set())
    if possible_requirements - {"text"} and not config.get("agents"):
        raise ValueError("non-text environments require an explicit capable seat roster")
    for index, spec in enumerate(config.get("agents", [])):
        try:
            seat_requirements = requirements | set(spec.get("required_modalities", []))
            if declaration is not None and declaration.roles:
                seat_roles = config.get("seat_roles", [])
                role_id = spec.get("role") or (seat_roles[index] if index < len(seat_roles) else None)
                if role_id is not None:
                    role = next((role for role in declaration.roles if role.id == role_id), None)
                    if role is None:
                        raise ValueError(f"unknown declared role {role_id!r}")
                    seat_requirements |= role.required_modalities
                else:
                    # A loose roster has no role assignment; explicit text-only
                    # requirements cannot override a possible image role.
                    seat_requirements |= set().union(*(role.required_modalities for role in declaration.roles))
            validate_seat(spec, seat_requirements)
        except ValueError as exc:
            raise ValueError(f"agents[{index}]: {exc}") from exc
    from .content import ImagePart, image_bytes
    for raw in config.get("image_assets", []):
        image_bytes(ImagePart.model_validate(raw), config.get("asset_root"))


def policy_modalities(policy):
    model = getattr(policy, "model", None) or getattr(policy, "client", None)
    caps = getattr(model, "capabilities", None) or getattr(policy, "capabilities", None)
    supported = set(getattr(policy, "supported_input_modalities", {"text"}))
    declared = ModalityCapabilities.model_validate(caps or {})
    if declared.input - {"text"} and not declared.evidence:
        return {"text"}
    return declared.input & supported
