"""Local, content-addressed environment item banks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .environment import ParameterConfig


def _item_id(raw: dict[str, Any]) -> str:
    for key in ("item_id", "task_id", "candidate_id", "id"):
        value = raw.get(key)
        if value is not None:
            if key == "candidate_id" and raw.get("mc_bucket") is not None:
                return f"{value}@mc-{raw['mc_bucket']}"
            return str(value)
    raise ValueError("every item-bank row needs item_id, task_id, candidate_id, or id")


def _item_params(raw: dict[str, Any]) -> dict[str, Any]:
    for key in ("params", "game_config", "config"):
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _oracle_result(raw: dict[str, Any]) -> Any:
    for key in ("oracle_result", "optimal", "oracle_stats"):
        if key in raw:
            return raw[key]
    return None


@dataclass(frozen=True)
class Item:
    item_id: str
    params: dict[str, Any]
    oracle_result: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "params": self.params,
            "oracle_result": self.oracle_result,
        }

    def attribute(self, key: str, default: Any = None) -> Any:
        """Read one frozen column, preferring the params block over the raw row.

        Banks disagree about where an attribute lives: calendar keeps ``density``
        inside ``params``, while negotiation carries ``mc_bucket`` beside its
        ``game_config``.  Both are attributes of the item.
        """

        if key in self.params:
            return self.params[key]
        return self.raw.get(key, default)

    def has_attribute(self, key: str) -> bool:
        return key in self.params or key in self.raw


@dataclass(frozen=True)
class ItemBank:
    path: Path
    item_bank_sha256: str
    items: tuple[Item, ...]

    @classmethod
    def load(cls, path: str | Path, *, expected_sha256: str | None = None) -> "ItemBank":
        path = Path(path)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise ValueError(
                f"item bank digest mismatch: expected {expected_sha256}, got {digest}"
            )
        raw_items = _decode_items(path, payload)
        items = tuple(
            Item(
                item_id=_item_id(raw),
                params=_item_params(raw),
                oracle_result=_oracle_result(raw),
                raw=raw,
            )
            for raw in raw_items
        )
        if not items:
            raise ValueError("item bank must contain at least one item")
        ids = [item.item_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("item bank item identifiers must be unique")
        return cls(path=path, item_bank_sha256=digest, items=items)

    def get(self, item_id: str) -> Item:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(f"unknown item {item_id!r}")

    def page(self, *, limit: int = 50, cursor: str | None = None) -> tuple[list[Item], str | None]:
        try:
            start = int(cursor or "0")
        except ValueError as exc:
            raise ValueError("item cursor must be an integer offset") from exc
        if start < 0:
            raise ValueError("item cursor must not be negative")
        page = list(self.items[start:start + limit])
        next_cursor = str(start + len(page)) if start + len(page) < len(self.items) else None
        return page, next_cursor

    def attribute_levels(self, key: str) -> list[tuple[Any, int]]:
        """Distinct values of one frozen column, with the row count behind each.

        Ordered by first appearance so a bank's own ordering survives into the
        strata a researcher sees.  Raises when no row carries the column, which
        is the drift a hand-written domain would have hidden.
        """

        if not any(item.has_attribute(key) for item in self.items):
            raise KeyError(
                f"no item in {self.path.name} carries the attribute {key!r}; "
                "the declaration and the item bank have drifted apart"
            )
        counts: dict[str, int] = {}
        values: dict[str, Any] = {}
        for item in self.items:
            if not item.has_attribute(key):
                continue
            value = item.attribute(key)
            token = json.dumps(value, sort_keys=True, default=str)
            counts[token] = counts.get(token, 0) + 1
            values.setdefault(token, value)
        return [(values[token], count) for token, count in counts.items()]


def derive_item_domain(
    parameter: "ParameterConfig", bank: ItemBank
) -> tuple[list[Any] | tuple[float, float] | None, list[tuple[Any, int]]]:
    """Project one item-sourced parameter's domain out of the pinned bank.

    Returns the domain in the shape the parameter's declared type implies — a
    ``(minimum, maximum)`` pair for numerics, the distinct values for
    categorical and boolean — alongside the full level/count list, which is what
    the design layer needs to know which strata are actually available.
    """

    levels = bank.attribute_levels(parameter.bank_key)
    if parameter.type in {"continuous", "integer"}:
        numeric = [value for value, _ in levels if isinstance(value, (int, float))
                   and not isinstance(value, bool)]
        domain = (float(min(numeric)), float(max(numeric))) if numeric else None
    else:
        domain = [value for value, _ in levels]
    return domain, levels


def _decode_items(path: Path, payload: bytes) -> list[dict[str, Any]]:
    text = payload.decode("utf-8")
    if path.suffix.lower() == ".json":
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            decoded = decoded.get("items", decoded.get("scenarios"))
        if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
            raise ValueError("JSON item banks must contain an items or scenarios list")
        return decoded
    items: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL item at line {number}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"item-bank line {number} must be a JSON object")
        items.append(raw)
    return items
