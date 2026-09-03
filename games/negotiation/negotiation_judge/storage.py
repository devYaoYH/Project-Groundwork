"""Firestore storage helpers for judge results.

Document ID scheme: {episode_uid}__{judge_model}__{prompt_version}

This compound key lets multiple judgment versions (different judge models or
prompt iterations) coexist in the same collection without overwriting each
other. episode_uid is also stored as a top-level field so it can be queried.

Resume logic: skip a environment only when (episode_uid, prompt_version) already exists
in Firestore — i.e. the exact same prompt produced a judgment for this environment.
"""

import base64
import gzip
import logging
from collections import defaultdict
from datetime import datetime, timezone

from negotiation_judge.schema import JudgmentRecord


def compress_transcript(text: str) -> str:
    """Gzip-compress a transcript string and return as base64."""
    return base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii")


def decompress_transcript(compressed: str) -> str:
    """Decompress a base64 + gzip transcript string."""
    return gzip.decompress(base64.b64decode(compressed)).decode("utf-8")

log = logging.getLogger("judge.storage")

JUDGE_COLLECTION = "judge_results"

try:
    from google.cloud.firestore import Client as FirestoreClient
    _client = FirestoreClient()
    _ok = True
    log.info("Judge Firestore client initialized")
except Exception:
    _client = None
    _ok = False


def _doc_id(episode_uid: str, judge_model: str, prompt_version: str) -> str:
    """Compound Firestore document ID."""
    # Sanitise model name: Firestore doc IDs cannot contain '/'
    safe_model = judge_model.replace("/", "_")
    return f"{episode_uid}__{safe_model}__{prompt_version}"


def firestore_available() -> bool:
    return _ok


def save_judgment(episode_uid: str, judgment_dict: dict, judge_model: str, prompt_version: str = "unknown", input_transcript: str | None = None) -> bool:
    """Write a GameJudgment to Firestore under the compound doc ID.

    Overwrites if the exact same (episode_uid, judge_model, prompt_version) exists.
    A different prompt_version produces a new document, preserving history.
    """
    if not _ok:
        log.warning("Firestore not available, skipping write for %s", episode_uid)
        return False
    try:
        merged = {**judgment_dict, "episode_uid": episode_uid, "judge_model": judge_model,
                  "prompt_version": prompt_version, "judged_at": datetime.now(timezone.utc).isoformat(),
                  "input_transcript_gz": compress_transcript(input_transcript) if input_transcript else None}
        record = JudgmentRecord(**merged)
        doc_id = _doc_id(episode_uid, judge_model, prompt_version)
        _client.collection(JUDGE_COLLECTION).document(doc_id).set(record.model_dump())
        log.debug("Saved judgment %s to Firestore", doc_id)
        return True
    except Exception as e:
        log.error("Firestore write failed for %s: %s", episode_uid, e)
        return False


def get_judgment(episode_uid: str) -> dict | None:
    """Fetch the most recently produced judgment for a environment.

    Queries by episode_uid field and returns the document with the latest
    judged_at timestamp, so the frontend always shows the newest version.
    """
    if not _ok:
        return None
    try:
        docs = (
            _client.collection(JUDGE_COLLECTION)
            .where("episode_uid", "==", episode_uid)
            .order_by("judged_at", direction="DESCENDING")
            .limit(1)
            .stream()
        )
        for doc in docs:
            data = doc.to_dict()
            data["_doc_id"] = doc.id
            return data
        return None
    except Exception as e:
        log.error("Firestore read failed for %s: %s", episode_uid, e)
        return None


def list_completed_episode_uids() -> dict[str, set[str]]:
    """Return {episode_uid: {prompt_version, ...}} for all judged games in Firestore.

    The runner uses this to skip games where the current prompt_version already
    has a judgment — a different prompt_version means re-judge.
    """
    if not _ok:
        return {}
    try:
        docs = _client.collection(JUDGE_COLLECTION).select(["episode_uid", "prompt_version"]).stream()
        result: dict[str, set[str]] = defaultdict(set)
        for doc in docs:
            data = doc.to_dict() or {}
            gid = data.get("episode_uid") or doc.id  # fallback for legacy docs
            pv = data.get("prompt_version", "unknown")
            result[gid].add(pv)
        return dict(result)
    except Exception as e:
        log.error("Firestore list failed: %s", e)
        return {}


def list_judgment_versions(episode_uid: str) -> list[dict]:
    """Return all judgment versions for a environment, sorted newest first.

    Each entry contains only the metadata fields needed for the version
    selector: _doc_id, judge_model, prompt_version, judged_at.
    """
    if not _ok:
        return []
    try:
        docs = (
            _client.collection(JUDGE_COLLECTION)
            .where("episode_uid", "==", episode_uid)
            .order_by("judged_at", direction="DESCENDING")
            .stream()
        )
        versions = []
        for doc in docs:
            data = doc.to_dict() or {}
            versions.append({
                "_doc_id": doc.id,
                "judge_model": data.get("judge_model", "unknown"),
                "prompt_version": data.get("prompt_version", "unknown"),
                "judged_at": data.get("judged_at", ""),
            })
        return versions
    except Exception as e:
        log.error("Firestore list_versions failed for %s: %s", episode_uid, e)
        return []


def get_judgment_by_doc_id(doc_id: str) -> dict | None:
    """Fetch a specific judgment version by its compound document ID."""
    if not _ok:
        return None
    try:
        doc = _client.collection(JUDGE_COLLECTION).document(doc_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data["_doc_id"] = doc.id
        return data
    except Exception as e:
        log.error("Firestore read failed for doc %s: %s", doc_id, e)
        return None


def list_all_judgments() -> list[dict]:
    """Fetch all judge results from Firestore.

    Used by run_consolidation.py to pull everything for Phase 2.
    For games with multiple versions, all versions are returned — the
    consolidation step should deduplicate or pick latest as needed.
    """
    if not _ok:
        return []
    try:
        results = []
        for doc in _client.collection(JUDGE_COLLECTION).stream():
            data = doc.to_dict()
            # Ensure episode_uid is always present (legacy docs used doc.id as episode_uid)
            if "episode_uid" not in data:
                data["episode_uid"] = doc.id
            results.append(data)
        log.info("Loaded %d judgment documents from Firestore", len(results))
        return results
    except Exception as e:
        log.error("Firestore list_all failed: %s", e)
        return []
