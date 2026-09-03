"""Firestore trace store — lifted from ``a2a-negotiation/backend/storage.py``.

Two behaviors from the original are preserved deliberately, because the existing
negotiation corpus depends on both:

- **gzip+base64 event compression** (``events_compressed``), keeping
  ``config``/``metrics`` uncompressed so they stay queryable.
- **nested-array flattening**: Firestore rejects ``list[list[...]]``, so keys
  like ``agent_projects`` are stored as ``{"0": [...], "1": [...]}`` and
  restored on read.

Unlike the original this stores a ``EpisodeTrace``, not a bespoke document
shape, so the judge and ``EpisodeDataset`` read negotiation and calendar identically.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import time
from pathlib import Path
from typing import Any

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.base import StoreCheck, register_store
from a2a_engine.storage.local import LocalJSONStore

log = logging.getLogger("a2a_engine.storage.firestore")

TRACE_SCHEMA_VERSION = 4


def compress_events(events: list[dict]) -> str:
    """Compress an events array to a base64-encoded gzip string."""
    blob = json.dumps(events, separators=(",", ":"), default=str)
    return base64.b64encode(gzip.compress(blob.encode("utf-8"), compresslevel=9)).decode("ascii")


def decompress_events(compressed: str) -> list[dict]:
    """Inverse of :func:`compress_events`."""
    return json.loads(gzip.decompress(base64.b64decode(compressed.encode("ascii"))).decode("utf-8"))


def flatten_nested_arrays(node: Any) -> Any:
    """Rewrite list-of-lists as {"0": [...], "1": [...]} for Firestore."""
    if isinstance(node, dict):
        return {k: flatten_nested_arrays(v) for k, v in node.items()}
    if isinstance(node, list):
        if any(isinstance(v, list) for v in node):
            return {str(i): flatten_nested_arrays(v) for i, v in enumerate(node)}
        return [flatten_nested_arrays(v) for v in node]
    return node


def restore_nested_arrays(node: Any) -> Any:
    """Inverse of :func:`flatten_nested_arrays`.

    A dict whose keys are exactly "0".."n-1" is treated as a flattened list.
    """
    if isinstance(node, dict):
        keys = list(node.keys())
        if keys and all(k.isdigit() for k in keys) and sorted(int(k) for k in keys) == list(range(len(keys))):
            return [restore_nested_arrays(node[str(i)]) for i in range(len(keys))]
        return {k: restore_nested_arrays(v) for k, v in node.items()}
    if isinstance(node, list):
        return [restore_nested_arrays(v) for v in node]
    return node


class FirestoreEpisodeStore:
    """Writes locally first, then mirrors to a Firestore collection."""

    name = "firestore"

    def __init__(
        self,
        *,
        collection: str = "game_traces",
        project: str | None = None,
        results_dir: str | Path = "./results",
        client: Any | None = None,
        **_ignored: Any,
    ) -> None:
        self.collection = collection
        self.project = project
        self.local = LocalJSONStore(results_dir=results_dir)
        self._client = client

    @property
    def client(self) -> Any | None:
        """Lazily construct the Firestore client.

        Deferred so that importing this module — which ``a2a_engine.storage``
        does unconditionally — never requires google-cloud-firestore or
        credentials. A store that cannot connect degrades to local-only.
        """
        if self._client is None:
            try:
                from google.cloud.firestore import Client
            except ImportError:
                log.warning("google-cloud-firestore not installed; Firestore mirroring disabled")
                return None
            try:
                self._client = Client(project=self.project or None)
            except Exception as exc:
                log.warning("Firestore client init failed: %s", exc)
                return None
        return self._client

    def to_document(self, trace: EpisodeTrace, manifest: EpisodeManifest) -> dict[str, Any]:
        """Serialize a trace into the Firestore document shape."""
        payload = json.loads(trace.model_dump_json())
        events = payload.pop("events", [])
        doc = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "episode_uid": trace.episode_uid,
            "environment_id": manifest.environment_id,
            "experiment_name": manifest.experiment_name,
            "episode_id": manifest.episode_id,
            "cell_id": manifest.cell_id,
            "config": flatten_nested_arrays(payload.get("config", {})),
            "final_state": flatten_nested_arrays(payload.get("final_state", {})),
            "metrics": flatten_nested_arrays(payload.get("metrics", {})),
            "release": flatten_nested_arrays(payload.get("release") or {}),
            "episode": flatten_nested_arrays(payload.get("episode") or {}),
            "observability": flatten_nested_arrays(payload.get("observability", {})),
            "started_at": payload.get("started_at"),
            "ended_at": payload.get("ended_at"),
            "stopped": payload.get("stopped", False),
            "events_compressed": compress_events(events),
            "manifest": json.loads(manifest.model_dump_json()),
        }
        return doc

    def from_document(self, doc: dict[str, Any]) -> EpisodeTrace:
        """Deserialize current and pre-migration negotiation documents.

        The old negotiation collection used ``{game_config, result}`` while
        this store uses ``{config, final_state, metrics}``. Reading both makes
        a historical corpus usable immediately and gives the backfill script a
        single canonical conversion path.
        """
        events = decompress_events(doc["events_compressed"]) if doc.get("events_compressed") else doc.get("events", [])
        if "game_config" in doc:
            config = restore_nested_arrays(doc.get("game_config") or {})
            result = restore_nested_arrays(doc.get("result") or {})
            agents = config.get("agents") or []
            config.setdefault("environment_id", "negotiation")
            config.setdefault("num_agents", len(agents) or 2)
            config.setdefault("agents", agents)
            metrics = dict(result.get("metrics") or {})
            rounds = result.get("rounds") or []
            metrics.setdefault("num_rounds", len(rounds))
            for old_key, new_key in (
                ("agent_a_cumulative_reward", "agent_a_total_reward"),
                ("agent_b_cumulative_reward", "agent_b_total_reward"),
            ):
                if old_key in result:
                    metrics.setdefault(new_key, result[old_key])
            metrics.setdefault("overdrawn_rounds", sum(bool(r.get("overdrawn")) for r in rounds if isinstance(r, dict)))
            a_reward, b_reward = result.get("agent_a_cumulative_reward"), result.get("agent_b_cumulative_reward")
            if a_reward is not None and b_reward is not None:
                metrics.setdefault("joint_reward", a_reward + b_reward)
            payload = {
                "episode_uid": doc.get("episode_uid") or result.get("episode_uid", ""),
                "config": config,
                "events": events,
                "final_state": result,
                "metrics": metrics,
                "release": restore_nested_arrays(doc.get("release", {})) or None,
                "episode": restore_nested_arrays(doc.get("episode", {})) or None,
                "observability": restore_nested_arrays(doc.get("observability", {})),
                "stopped": result.get("stopped", False),
            }
            if doc.get("started_at") or doc.get("created_at"):
                payload["started_at"] = doc.get("started_at") or doc.get("created_at")
            if doc.get("ended_at"):
                payload["ended_at"] = doc["ended_at"]
            return EpisodeTrace.model_validate(payload)
        return EpisodeTrace.model_validate({
            "episode_uid": doc.get("episode_uid", ""),
            "config": restore_nested_arrays(doc.get("config", {})),
            "events": events,
            "final_state": restore_nested_arrays(doc.get("final_state", {})),
            "metrics": restore_nested_arrays(doc.get("metrics", {})),
            "release": restore_nested_arrays(doc.get("release", {})) or None,
            "episode": restore_nested_arrays(doc.get("episode", {})) or None,
            "observability": restore_nested_arrays(doc.get("observability", {})),
            "started_at": doc.get("started_at"),
            "ended_at": doc.get("ended_at"),
            "stopped": doc.get("stopped", False),
        })

    def put_episode(self, trace: EpisodeTrace, manifest: EpisodeManifest) -> str:
        local_uri = self.local.put_episode(trace, manifest)
        manifest.storage.backend = self.name

        client = self.client
        if client is None:
            manifest.storage.status = "not_configured"
            self.local.write_manifest(manifest, Path(local_uri))
            return local_uri

        uri = f"firestore://{self.collection}/{trace.episode_uid}"
        manifest.storage.uri = uri
        try:
            client.collection(self.collection).document(trace.episode_uid).set(
                self.to_document(trace, manifest)
            )
            manifest.storage.status = "written"
        except Exception as exc:
            manifest.storage.status = "failed"
            manifest.storage.error = f"{type(exc).__name__}: {exc}"
            log.warning("Firestore write failed for %s: %s", trace.episode_uid, exc)

        self.local.write_manifest(manifest, Path(local_uri))
        return uri if manifest.storage.status == "written" else local_uri

    def get_episode(self, episode_uid: str) -> EpisodeTrace | None:
        client = self.client
        if client is None:
            return self.local.get_episode(episode_uid)
        snapshot = client.collection(self.collection).document(episode_uid).get()
        if not getattr(snapshot, "exists", False):
            return self.local.get_episode(episode_uid)
        return self.from_document(snapshot.to_dict())

    def list_episodes(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        client = self.client
        if client is None:
            return self.local.list_episodes(filters, limit, cursor)
        query = client.collection(self.collection)
        for key, value in (filters or {}).items():
            query = query.where(key, "==", value)
        query = query.order_by("episode_uid").limit(limit)
        if cursor:
            query = query.start_after({"episode_uid": cursor})
        rows = [doc.to_dict().get("manifest", {}) for doc in query.stream()]
        next_cursor = rows[-1].get("episode_uid") if len(rows) == limit and rows else None
        return rows, next_cursor

    # --- preflight ---

    def check(self) -> StoreCheck:
        """Probe the collection with a bounded read — no canary document.

        A write probe against a shared collection would leave debris that the
        analysis layer then has to filter out, so this confirms the SDK is
        installed, credentials resolve, and the collection is queryable.
        """
        started = time.monotonic()
        target = f"firestore://{self.project or 'default'}/{self.collection}"
        local = self.local.check()
        if not local.ok:
            return StoreCheck(
                backend=self.name, ok=False, target=str(self.local.results_dir),
                detail=f"local mirror unusable: {local.detail}",
            )
        client = self.client
        if client is None:
            return StoreCheck(
                backend=self.name, ok=False, target=target,
                detail="no Firestore client; install google-cloud-firestore and "
                       "check credentials (see docs/STORAGE.md)",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        try:
            list(client.collection(self.collection).limit(1).stream())
        except Exception as exc:
            return StoreCheck(
                backend=self.name, ok=False, target=target,
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        return StoreCheck(
            backend=self.name, ok=True, target=target,
            detail="collection is queryable",
            latency_ms=(time.monotonic() - started) * 1000,
        )


register_store("firestore", FirestoreEpisodeStore)
