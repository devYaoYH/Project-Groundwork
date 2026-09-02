"""Game-agnostic judgment storage.

Lifted from ``a2a-negotiation/judge/storage.py``, with the negotiation-specific
document schema removed. What was worth keeping — and is now shared by every
game — is the addressing and resume scheme:

    document id = {game_id}__{judge_model}__{prompt_version}

That compound key is what lets several judge models and several prompt
iterations coexist over the same corpus without overwriting each other, and it
is what makes ``pending()`` correct: a game is re-judged when the prompt version
changes, and skipped when it has not.

Backends mirror ``a2a_engine.storage``: local JSONL always works, Firestore is
optional and degrades rather than failing.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger("a2a_judge.store")

UNKNOWN_VERSION = "unknown"


def judgment_doc_id(game_id: str, judge_model: str, prompt_version: str) -> str:
    """Compound key. Slashes in model ids (``meta/llama-4``) would nest paths."""
    safe_model = judge_model.replace("/", "_")
    return f"{game_id}__{safe_model}__{prompt_version}"


def compress_transcript(text: str) -> str:
    """Gzip+base64 a transcript so judged inputs stay auditable without bloat."""
    return base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii")


def decompress_transcript(compressed: str) -> str:
    return gzip.decompress(base64.b64decode(compressed)).decode("utf-8")


@runtime_checkable
class JudgmentStore(Protocol):
    """Persistence for judge outputs."""

    name: str

    def save(self, game_id: str, judgment: dict, *, judge_model: str,
             prompt_version: str, input_transcript: str | None = None) -> bool: ...

    def completed(self) -> dict[str, set[str]]:
        """``{game_id: {prompt_version, ...}}`` for everything already judged."""
        ...

    def load_all(self) -> list[dict]: ...


class LocalJudgmentStore:
    """Append-only JSONL, one line per judgment. The default everywhere."""

    name = "local"

    def __init__(self, path: str | Path = "./results/judgments.jsonl") -> None:
        self.path = Path(path)

    def save(self, game_id: str, judgment: dict, *, judge_model: str,
             prompt_version: str = UNKNOWN_VERSION,
             input_transcript: str | None = None) -> bool:
        record = {
            **judgment,
            "game_id": game_id,
            "judge_model": judge_model,
            "prompt_version": prompt_version,
            "judged_at": datetime.now(timezone.utc).isoformat(),
            "doc_id": judgment_doc_id(game_id, judge_model, prompt_version),
        }
        if input_transcript is not None:
            record["input_transcript_gz"] = compress_transcript(input_transcript)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str))
            f.write("\n")
        return True

    def load_all(self) -> list[dict]:
        """Later writes win, so re-judging the same key supersedes cleanly."""
        if not self.path.exists():
            return []
        by_key: dict[str, dict] = {}
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("doc_id") or judgment_doc_id(
                record.get("game_id", ""),
                record.get("judge_model", ""),
                record.get("prompt_version", UNKNOWN_VERSION),
            )
            by_key[key] = record
        return list(by_key.values())

    def completed(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for record in self.load_all():
            gid = record.get("game_id")
            if gid:
                out[gid].add(record.get("prompt_version", UNKNOWN_VERSION))
        return dict(out)


class FirestoreJudgmentStore:
    """Firestore-backed judgments, addressed by the compound key."""

    name = "firestore"

    def __init__(self, collection: str = "judge_results", project: str | None = None,
                 client: Any | None = None) -> None:
        self.collection = collection
        self.project = project
        self._client = client

    @property
    def client(self) -> Any | None:
        if self._client is None:
            try:
                from google.cloud.firestore import Client
            except ImportError:
                log.warning("google-cloud-firestore not installed; judgments not persisted")
                return None
            try:
                self._client = Client(project=self.project or None)
            except Exception as exc:
                log.warning("Firestore client init failed: %s", exc)
                return None
        return self._client

    def save(self, game_id: str, judgment: dict, *, judge_model: str,
             prompt_version: str = UNKNOWN_VERSION,
             input_transcript: str | None = None) -> bool:
        client = self.client
        if client is None:
            return False
        record = {
            **judgment,
            "game_id": game_id,
            "judge_model": judge_model,
            "prompt_version": prompt_version,
            "judged_at": datetime.now(timezone.utc).isoformat(),
        }
        if input_transcript is not None:
            record["input_transcript_gz"] = compress_transcript(input_transcript)
        try:
            doc_id = judgment_doc_id(game_id, judge_model, prompt_version)
            client.collection(self.collection).document(doc_id).set(record)
            return True
        except Exception as exc:
            log.error("Firestore judgment write failed for %s: %s", game_id, exc)
            return False

    def load_all(self) -> list[dict]:
        client = self.client
        if client is None:
            return []
        return [doc.to_dict() for doc in client.collection(self.collection).stream()]

    def completed(self) -> dict[str, set[str]]:
        client = self.client
        if client is None:
            return {}
        try:
            docs = client.collection(self.collection).select(
                ["game_id", "prompt_version"]
            ).stream()
        except Exception as exc:
            log.error("Firestore judgment list failed: %s", exc)
            return {}
        out: dict[str, set[str]] = defaultdict(set)
        for doc in docs:
            data = doc.to_dict() or {}
            # Legacy documents predate the game_id field; fall back to the id.
            gid = data.get("game_id") or doc.id
            out[gid].add(data.get("prompt_version", UNKNOWN_VERSION))
        return dict(out)


def pending(
    game_ids: Iterable[str], store: JudgmentStore, prompt_version: str
) -> list[str]:
    """Games still needing a judgment at this prompt version.

    Resume is keyed on (game_id, prompt_version), not game_id alone: bumping the
    prompt is how you force a re-judge, and forgetting that distinction would
    silently mix judgments from two different rubrics in one aggregate.
    """
    done = store.completed()
    return [gid for gid in game_ids if prompt_version not in done.get(gid, set())]
