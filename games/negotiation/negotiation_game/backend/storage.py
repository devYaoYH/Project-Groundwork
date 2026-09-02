"""
Persistent storage for completed game logs.

Each game is saved as a JSON file under DATA_DIR/<game_id>.json containing
the full game result and the event log.

On Cloud Run, local file I/O is skipped. Firestore is write-only when available.
"""

import base64
import gzip
import json
import logging
import os
from pathlib import Path

try:
    from google.cloud.firestore import Client as FirestoreClient
    from google.cloud.firestore import SERVER_TIMESTAMP
except ImportError:
    FirestoreClient = None
    SERVER_TIMESTAMP = None

from negotiation_game.backend.defaults import DATA_DIR, IS_CLOUD_RUN, FIRESTORE_COLLECTION, FIRESTORE_PROJECT, VISITOR_COLLECTION
from negotiation_game.backend.schemas import FirestoreDocumentSchema, CURRENT_SCHEMA_VERSION

log = logging.getLogger("negotiation")

# --- Compression utilities ---

def compress_events(events: list[dict]) -> str:
    """Compress events array to base64-encoded gzip string.

    Args:
        events: List of event dicts

    Returns:
        Base64-encoded gzip-compressed JSON string
    """
    json_str = json.dumps(events, separators=(',', ':'))
    compressed = gzip.compress(json_str.encode('utf-8'), compresslevel=9)
    return base64.b64encode(compressed).decode('ascii')


def decompress_events(compressed: str) -> list[dict]:
    """Decompress base64-encoded gzip string to events array.

    Args:
        compressed: Base64-encoded gzip-compressed JSON string

    Returns:
        List of event dicts
    """
    compressed_bytes = base64.b64decode(compressed.encode('ascii'))
    json_bytes = gzip.decompress(compressed_bytes)
    return json.loads(json_bytes.decode('utf-8'))


# --- Firestore ---

try:
    _firestore_client = FirestoreClient(project=FIRESTORE_PROJECT or None) if FirestoreClient else None
    _firestore_ok = _firestore_client is not None
    if _firestore_ok:
        project_info = f" project={FIRESTORE_PROJECT}" if FIRESTORE_PROJECT else ""
        log.info("Firestore client initialized%s", project_info)
except Exception:
    _firestore_client = None
    _firestore_ok = False


def firestore_available() -> bool:
    return _firestore_ok


def validate_firestore_document(game_config: dict, result: dict, events: list[dict]) -> FirestoreDocumentSchema:
    """Validate a game trace against the Firestore schema. Raises ValidationError on mismatch."""
    return FirestoreDocumentSchema(
        game_config=game_config,
        result=result,
        events=events,
    )


def save_to_firestore(game_id: str, game_config: dict, result: dict, events: list[dict]) -> None:
    """Write a completed game trace to Firestore with current schema version (V3).

    V3 schema compresses events array with gzip to reduce storage size while
    keeping game_config and result uncompressed for queryability.
    """
    if not _firestore_ok:
        return
    try:
        validated = validate_firestore_document(game_config, result, events)
        # Exclude None values to keep schema clean (no cheap_talk_transcript: null)
        doc = validated.model_dump(exclude_none=True)

        # Firestore doesn't support nested arrays (list[list[...]]).
        # Convert agent_projects from [[...], [...]] to {"0": [...], "1": [...]}
        for section in ("game_config", "result"):
            if section in doc and "agent_projects" in doc[section]:
                ap = doc[section]["agent_projects"]
                doc[section]["agent_projects"] = {str(i): v for i, v in enumerate(ap)}

        # Also convert agent_projects inside per_round_scenarios
        if "result" in doc and "per_round_scenarios" in doc["result"]:
            for scenario in doc["result"]["per_round_scenarios"]:
                if "agent_projects" in scenario:
                    ap = scenario["agent_projects"]
                    scenario["agent_projects"] = {str(i): v for i, v in enumerate(ap)}

        # V3: Compress events and replace with events_compressed
        if "events" in doc:
            doc["events_compressed"] = compress_events(doc["events"])
            del doc["events"]  # Remove uncompressed events

        # Ensure schema_version is set
        doc["schema_version"] = CURRENT_SCHEMA_VERSION
        doc["created_at"] = SERVER_TIMESTAMP

        _firestore_client.collection(FIRESTORE_COLLECTION).document(game_id).set(doc)

        # Log with size info
        orig_size = len(json.dumps(events, separators=(',', ':')))
        compressed_size = len(doc["events_compressed"])
        reduction_pct = (1 - compressed_size / orig_size) * 100 if orig_size > 0 else 0
        log.info(
            "[%s] saved to Firestore (schema v%d, events: %d→%d bytes, %.1f%% reduction)",
            game_id[:8], CURRENT_SCHEMA_VERSION, orig_size, compressed_size, reduction_pct
        )
    except Exception as e:
        log.error("[%s] Firestore write failed: %s", game_id[:8], e)


def save_visitor_signup(game_id: str, email: str, meta: dict) -> None:
    """Record a demo visitor signup in VISITOR_COLLECTION (doc id = game_id).

    Kept separate from the research trace collection so visitor PII never
    mixes with experiment data.
    """
    if not _firestore_ok:
        return
    try:
        doc = {
            "email": email,
            "game_id": game_id,
            **meta,
            "status": "started",
            "created_at": SERVER_TIMESTAMP,
        }
        _firestore_client.collection(VISITOR_COLLECTION).document(game_id).set(doc)
        log.info("[%s] visitor signup saved", game_id[:8])
    except Exception as e:
        log.error("[%s] visitor signup write failed: %s", game_id[:8], e)


def update_visitor_result(game_id: str, result_summary: dict) -> None:
    """Attach final game outcome to an existing visitor signup doc."""
    if not _firestore_ok:
        return
    try:
        _firestore_client.collection(VISITOR_COLLECTION).document(game_id).set(
            {**result_summary, "status": "completed", "completed_at": SERVER_TIMESTAMP},
            merge=True,
        )
        log.info("[%s] visitor result updated", game_id[:8])
    except Exception as e:
        log.error("[%s] visitor result write failed: %s", game_id[:8], e)


def list_traces(limit: int = 50, start_after: str | None = None, offset: int = 0, filters: dict[str, any] | None = None) -> tuple[list[dict], int]:
    """List game traces from Firestore, ordered by created_at desc.
    Returns (summary dicts, total count). Supports both cursor and offset pagination.

    Args:
        limit: Maximum number of traces to return
        start_after: Game ID to start pagination after (cursor-based, mutually exclusive with offset)
        offset: Number of documents to skip (offset-based, mutually exclusive with start_after)
        filters: Optional dict of field filters (e.g., {"schema_version": 2})
                Note: Server-side filtering requires a Firestore composite index.
                If the index is not available, falls back to client-side filtering.

    Returns:
        Tuple of (traces list, total count)
    """
    if not _firestore_ok:
        return [], 0
    try:
        col = _firestore_client.collection(FIRESTORE_COLLECTION)
        query = col.order_by("created_at", direction="DESCENDING")

        # Try server-side filtering first (requires composite index)
        server_side_filter_failed = False
        if filters:
            try:
                for field, value in filters.items():
                    query = query.where(field, "==", value)
            except Exception:
                # Composite index not available - will do client-side filtering
                server_side_filter_failed = True
                query = col.order_by("created_at", direction="DESCENDING")

        # Get total count for pagination (only when using offset)
        total_count = 0
        if offset >= 0 and not start_after:
            count_query = col.order_by("created_at", direction="DESCENDING")
            if filters and not server_side_filter_failed:
                for field, value in filters.items():
                    count_query = count_query.where(field, "==", value)
            total_count = len(list(count_query.select([]).stream()))

        # Apply cursor or offset
        if start_after:
            cursor_doc = col.document(start_after).get()
            if cursor_doc.exists:
                query = query.start_after(cursor_doc)
        elif offset > 0:
            query = query.offset(offset)

        # Fetch more results if doing client-side filtering
        fetch_limit = limit * 3 if (filters and server_side_filter_failed) else limit
        query = query.limit(fetch_limit)

        # V2+ optimization: use select() to skip events_compressed/events blobs
        # Only fetch top-level fields needed for summaries
        summary_query = query.select(["game_config", "result", "schema_version", "created_at"])

        results = []
        for doc in summary_query.stream():
            data = doc.to_dict()
            result = data.get("result", {})
            config = data.get("game_config", {})

            # Client-side filtering if needed
            if filters and server_side_filter_failed:
                matches = all(data.get(field) == value for field, value in filters.items())
                if not matches:
                    continue

            results.append({
                "game_id": doc.id,
                "mode": config.get("mode", "?"),
                "num_rounds": config.get("num_rounds", 0),
                "agent_a_reward": result.get("agent_a_cumulative_reward", 0),
                "agent_b_reward": result.get("agent_b_cumulative_reward", 0),
                "agents": config.get("agents", []),
                "created_at": str(data.get("created_at", "")),
                "seed": config.get("seed"),
                "swapped": config.get("swapped", False),
                "experiment_label": config.get("experiment_label", ""),
                "experiment_run_id": config.get("experiment_run_id", ""),
                "schema_version": data.get("schema_version", 1),
            })

            # Stop once we have enough results
            if len(results) >= limit:
                break

        return results, total_count
    except Exception as e:
        log.error("Firestore list_traces failed: %s", e)
        return [], 0


def _restore_agent_projects(data: dict) -> None:
    """Reverse the map encoding of agent_projects back to list[list[...]]."""
    for section in ("game_config", "result"):
        if section in data and isinstance(data[section], dict):
            ap = data[section].get("agent_projects")
            if isinstance(ap, dict):
                data[section]["agent_projects"] = [ap[k] for k in sorted(ap.keys())]

    # Also restore agent_projects inside per_round_scenarios
    result = data.get("result")
    if isinstance(result, dict):
        for scenario in result.get("per_round_scenarios", []):
            if isinstance(scenario, dict):
                ap = scenario.get("agent_projects")
                if isinstance(ap, dict):
                    scenario["agent_projects"] = [ap[k] for k in sorted(ap.keys())]


def get_trace(game_id: str) -> dict | None:
    """Fetch a single game trace from Firestore by game_id."""
    if not _firestore_ok:
        return None
    try:
        doc = _firestore_client.collection(FIRESTORE_COLLECTION).document(game_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data["game_id"] = doc.id
        # Convert Firestore timestamp to string for JSON serialization
        if "created_at" in data and data["created_at"] is not None:
            data["created_at"] = str(data["created_at"])
        # Reverse agent_projects map→list conversion (see save_to_firestore)
        _restore_agent_projects(data)
        return data
    except Exception as e:
        log.error("Firestore get_trace failed: %s", e)
        return None


# --- Local file storage (dev only, skipped on Cloud Run) ---

def save_game(game_id: str, result: dict, events: list[dict]) -> Path | None:
    """Write a completed game to disk. Skipped on Cloud Run."""
    if IS_CLOUD_RUN:
        return None
    os.makedirs(DATA_DIR, exist_ok=True)
    path = Path(DATA_DIR) / f"{game_id}.json"
    payload = {"result": result, "events": events}
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def load_game(game_id: str) -> dict | None:
    """Load a single game from disk, or None if not found."""
    if IS_CLOUD_RUN:
        return None
    path = Path(DATA_DIR) / f"{game_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def load_all_games() -> dict[str, dict]:
    """Load all saved games. Returns {game_id: result_dict}. Empty on Cloud Run."""
    if IS_CLOUD_RUN:
        return {}
    out: dict[str, dict] = {}
    if not os.path.isdir(DATA_DIR):
        return out
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            data = json.loads((Path(DATA_DIR) / fname).read_text())
            game_id = fname.removesuffix(".json")
            out[game_id] = data["result"]
        except (json.JSONDecodeError, KeyError):
            continue
    return out
