"""The provenance block, and the promotion of it into queryable columns.

Provenance is stamped **at expansion**, before any episode runs, and written
into the trace itself in full.  A trace handed to a collaborator, or read years
later without a control plane, says what produced it.  The dimension and fact
tables are a projection of that block rather than an independent source of
truth, which is what makes the star schema rebuildable: drop it and re-project.

Nothing here reconstructs identity from a filename, a directory layout, or a
log line.  Every field is either stamped by the compiler or read back off the
block it stamped.
"""

from __future__ import annotations

from typing import Any

from a2a_engine.manifest import config_hash, redact_config
from a2a_engine.schemas import EpisodeTrace

PROVENANCE_KEY = "provenance"
PROVENANCE_SCHEMA_VERSION = 1

#: Episode-row statuses.  ``PARTIAL`` is a trace recovered from an interrupted
#: episode's event log: evidence for inspection and retry, never a result.
STATUS_COMPLETED = "COMPLETED"
STATUS_STOPPED = "STOPPED"
STATUS_PARTIAL = "PARTIAL"


def build_provenance(
    *,
    config: dict[str, Any],
    experiment_name: str,
    cell_id: str,
    episode_idx: int,
    attempt: int = 1,
    release: dict[str, Any] | None = None,
    experiment_id: str | None = None,
    design_sha256: str | None = None,
    item_id: str | None = None,
    item_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the block one episode carries about how it came to exist.

    ``experiment_id`` and ``design_sha256`` stay ``None`` until a design object
    exists, which is exactly the shape a hand-written config keeps forever: it
    runs, it records what it can, and it cannot join a preregistered analysis.
    """
    release = release or {}
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "experiment_name": experiment_name,
        "design_sha256": design_sha256,
        "release_id": release.get("release_id"),
        "release_version": release.get("release_version"),
        "declaration_sha256": release.get("declaration_sha256"),
        "cell_id": cell_id,
        "episode_id": config.get("episode_id"),
        "episode_idx": int(episode_idx),
        "attempt": int(attempt),
        "seed": config.get("seed"),
        "item_id": item_id,
        "item_bank_sha256": release.get("item_bank_sha256"),
        "oracle_version": release.get("oracle_version"),
        "participants": pinned_participants(config),
        # A randomized item attribute varies *within* a cell, so the cell's
        # levels cannot describe it and the episode's own record must.  The
        # compiler fills this in; a hand-written config leaves it empty.
        "item_attributes": dict(item_attributes or {}),
    }


def pinned_participants(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Freeze the roster: who played, as what kind, under which binding.

    Seat position is the environment's to permute, so this records identity and
    binding only.  Credentials are redacted before hashing for the same reason
    ``config_hash`` redacts them: they are not experimental variables.
    """
    pinned: list[dict[str, Any]] = []
    for index, entry in enumerate(config.get("agents") or []):
        spec = entry if isinstance(entry, dict) else getattr(entry, "model_dump", dict)()
        if not isinstance(spec, dict):
            continue
        redacted = redact_config(spec)
        pinned.append({
            "participant_id": str(spec.get("name") or spec.get("id") or f"participant_{index}"),
            "kind": _participant_kind(spec),
            "binding": spec.get("binding") or spec.get("model") or spec.get("type") or None,
            "config_sha256": config_hash(redacted),
        })
    return pinned


def _participant_kind(spec: dict[str, Any]) -> str:
    """Map an agent entry onto a declared participant kind.

    Roles declare which kinds they accept (``llm``/``scripted``/``human``), so
    a heuristic stand-in and a scripted one are the same kind as far as the
    declaration is concerned; only a human arm is structurally different.
    """
    declared = str(spec.get("type") or "").lower()
    if declared == "human":
        return "human"
    if declared == "llm" or spec.get("model"):
        return "llm"
    return "scripted"


def provenance_of(trace: EpisodeTrace) -> dict[str, Any]:
    """Read the block back off a persisted trace, tolerating its absence.

    ``EpisodeConfigBase`` allows extras, which is how the block survives from
    the resolved config into whatever config subclass the environment builds
    and out through ``EpisodeTrace.config``.
    """
    config = trace.config
    block = getattr(config, PROVENANCE_KEY, None)
    if block is None:
        extra = getattr(config, "model_extra", None) or {}
        block = extra.get(PROVENANCE_KEY)
    return dict(block) if isinstance(block, dict) else {}


def episode_status(trace: EpisodeTrace) -> str:
    """The terminal state of this attempt, as one queryable token."""
    if trace.observability.get("partial"):
        return STATUS_PARTIAL
    return STATUS_STOPPED if trace.stopped else STATUS_COMPLETED


def promoted_columns(trace: EpisodeTrace) -> dict[str, Any]:
    """The subset of the block the fact table keeps as real columns.

    Promotion is a query-performance decision on a rebuildable projection, not
    a durability decision -- the durable copy is the block inside ``config``,
    which travels with the trace -- so this set can be revised at any time.
    """
    block = provenance_of(trace)
    return {
        "experiment_id": block.get("experiment_id"),
        "release_id": block.get("release_id"),
        "item_id": block.get("item_id"),
        "attempt": _as_int(block.get("attempt"), default=1),
        "seed": _as_int(trace.config.seed if trace.config.seed is not None else block.get("seed")),
        "status": episode_status(trace),
    }


def release_dimension(trace: EpisodeTrace) -> dict[str, Any] | None:
    """Rebuild the release dimension row this episode's provenance implies.

    The fact row references a release, and a runner writing into a fresh
    database has no control plane to have seeded one.  Projecting the dimension
    out of the trace is what lets the foreign key hold anyway -- and it is the
    same direction of travel as everything else here: the record is the source,
    the tables are the projection.
    """
    block = provenance_of(trace)
    release_id = block.get("release_id")
    if not release_id:
        return None
    return {
        "id": str(release_id),
        "environment_id": str(trace.config.environment_id or ""),
        "version": block.get("release_version"),
        "declaration_sha256": block.get("declaration_sha256"),
        "item_bank_sha256": block.get("item_bank_sha256"),
        "oracle_version": block.get("oracle_version"),
    }


def _as_int(value: Any, *, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
