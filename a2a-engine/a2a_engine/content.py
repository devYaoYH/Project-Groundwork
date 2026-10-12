"""Portable seat-visible content. Image bytes travel only on authenticated I/O.

An asset reference contains no bank row, filename, evaluator label or seat name.
The environment selects the visible parts; this layer never selects bank rows.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    type: Literal["image"] = "image"
    asset: str = Field(pattern=r"^asset://[0-9a-f]{64}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    # Inline data is a transient wire field, never a persisted text payload.
    data: str | None = Field(default=None, repr=False, max_length=5592408)

    @model_validator(mode="after")
    def integrity(self):
        if self.asset != f"asset://{self.sha256}":
            raise ValueError("asset reference and digest disagree")
        if self.data is not None:
            verify_image(self, base64.b64decode(self.data, validate=True))
        return self


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]
_parts = TypeAdapter(list[ContentPart], config=ConfigDict(hide_input_in_errors=True))


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    role: str
    content: str | list[ContentPart] | None


class SeatObservation(BaseModel):
    """Typed reserved content plus environment-owned, seat-projected fields."""
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    content: str | list[ContentPart] | None = None


def contains_image(value):
    if isinstance(value, ImagePart):
        return True
    if isinstance(value, dict):
        return value.get("type") == "image" or any(contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_image(item) for item in value)
    return False


def validate_observation(value):
    # Images outside reserved content would otherwise be stringified or hidden.
    incidental = {key: item for key, item in value.items() if key != "content"}
    if contains_image(incidental):
        raise ValueError("seat images must appear in observation.content")
    # Preserve the legacy dictionary exactly when reserved content is absent.
    if "content" not in value:
        return value
    validated = SeatObservation.model_validate(value)
    return validated.model_dump(exclude_none=True)


def parts(content):
    if isinstance(content, str):
        return [TextPart(text=content)]
    return _parts.validate_python(content)


def verify_image(image, payload):
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds size limit or is empty")
    if hashlib.sha256(payload).hexdigest() != image.sha256:
        raise ValueError("image asset digest mismatch")
    signatures = {"image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
                  "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
                  "image/webp": payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"}
    if not signatures[image.mime_type]:
        raise ValueError("image asset MIME does not match bytes")
    return payload


class AssetStore:
    """Content-addressed directory, independent of host/container absolute paths."""
    def __init__(self, root):
        self.root = Path(root).resolve()

    def read(self, image: ImagePart):
        if image.data is not None:
            return verify_image(image, base64.b64decode(image.data, validate=True))
        path = (self.root / image.sha256).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("asset escapes store")
        return verify_image(image, path.read_bytes())

    def put(self, payload: bytes, mime_type: str):
        digest = hashlib.sha256(payload).hexdigest()
        image = ImagePart(asset=f"asset://{digest}", sha256=digest, mime_type=mime_type)
        verify_image(image, payload)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / digest
        if path.exists():
            self.read(image)
        else:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".asset-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            try:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    self.read(image)
            finally:
                temporary.unlink()
        return image

    def package(self, images, destination):
        target = AssetStore(destination)
        for image in images:
            target.put(self.read(image), image.mime_type)


def image_bytes(image, asset_root=None):
    if image.data is None and asset_root is None:
        raise ValueError("image reference requires an explicit asset_root")
    return AssetStore(asset_root or ".").read(image)


def observation_content(observation, prompt=None):
    """Reserved content is already projected for this seat, never whole config."""
    observation = validate_observation(observation)
    content = observation.get("content")
    if content is None:
        return prompt if prompt is not None else str(observation)
    validated = [part.model_dump(exclude_none=True) for part in parts(content)]
    if prompt:
        validated.insert(0, TextPart(text=prompt).model_dump())
    return validated


def media_metadata(value):
    """Strip inline bytes recursively for traces/telemetry, retaining provenance."""
    if isinstance(value, BaseModel):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        if value.get("type") == "image":
            return ImagePart.model_validate(value).model_dump(exclude={"data"})
        return {key: media_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [media_metadata(item) for item in value]
    return value


def inline_media(value, asset_root=None):
    """Resolve only images in an already seat-projected observation/message."""
    if isinstance(value, BaseModel):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        if value.get("type") == "image":
            image = ImagePart.model_validate(value)
            return {**image.model_dump(exclude_none=True),
                    "data": base64.b64encode(image_bytes(image, asset_root)).decode("ascii")}
        return {key: inline_media(item, asset_root) for key, item in value.items()}
    if isinstance(value, list):
        return [inline_media(item, asset_root) for item in value]
    return value


def content_modalities(messages):
    result = {"text"}
    for message in messages:
        content = message.get("content", "")
        if content is not None and not isinstance(content, str):
            result.update(part.type for part in parts(content))
    return result


def provider_messages(messages, api_format, asset_root=None):
    """Map canonical parts to provider payloads without mutating caller history."""
    result = []
    for message in messages:
        content = message.get("content", "")
        if content is None or isinstance(content, str):
            result.append(dict(message))
            continue
        blocks = []
        for part in parts(content):
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            else:
                if message.get("role") != "user":
                    raise ValueError("image input is supported only in user messages")
                data = base64.b64encode(image_bytes(part, asset_root)).decode("ascii")
                if api_format in {"anthropic", "vertexai_anthropic"}:
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.mime_type, "data": data}})
                elif api_format in {"openai", "vertexai_openai"}:
                    blocks.append({"type": "image_url", "image_url": {"url": f"data:{part.mime_type};base64,{data}"}})
                elif api_format == "vertexai":
                    blocks.append({"type": "image", "mime_type": part.mime_type, "data": data})
                else:
                    raise ValueError(f"image input unsupported by adapter {api_format!r}")
        result.append({**message, "content": blocks})
    return result
