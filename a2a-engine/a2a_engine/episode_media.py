"""Retain and retrieve the exact image inputs recorded in an episode.

Release banks describe initial stimuli; an episode can also contain generated
boards and route overlays. These bytes travel with persisted evidence, never in
the JSON transcript. Retrieval is restricted to references in the selected trace.
"""
from __future__ import annotations

from pathlib import Path

from .content import AssetStore, ImagePart


def episode_images(trace) -> list[ImagePart]:
    value = trace.model_dump(mode="json") if hasattr(trace, "model_dump") else trace
    images = {}

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "image":
                image = ImagePart.model_validate(node)
                images[image.sha256] = image
            else:
                for child in node.values():
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return [images[key] for key in sorted(images)]


def persist_episode_images(trace, destination: str | Path) -> list[ImagePart]:
    images = episode_images(trace)
    if not images:
        return []
    config = trace.config.model_dump() if hasattr(trace.config, "model_dump") else trace.config
    root = config.get("asset_root")
    target = AssetStore(destination)
    source = AssetStore(root) if root else None
    for image in images:
        if (target.root / image.sha256).exists():
            target.read(image)
        elif image.data is not None or source is not None:
            payload = (source or target).read(image)
            target.put(payload, image.mime_type)
        else:
            raise ValueError("recorded image requires an explicit asset_root for persistence")
    return images


def episode_image(trace, digest: str) -> ImagePart:
    image = next((image for image in episode_images(trace) if image.sha256 == digest), None)
    if image is None:
        raise KeyError("image is not referenced by this episode")
    return image


def read_episode_image(trace, digest: str, asset_root: str | Path):
    image = episode_image(trace, digest)
    return AssetStore(asset_root).read(image), image.mime_type
