"""Throwaway: rerun Phase 2 consolidation on dcaa097 judgments with the
generalized CONSOLIDATION_SYSTEM_PROMPT.

Reads the 730 existing judgments from Firestore (prompt_version=dcaa097),
runs Step 1 (category definition) + Step 2 (alias mapping), and writes
the result to judge/output/taxonomy_v2.json without touching the existing
taxonomy.json.

Usage:
    uv run python -m judge.consolidate_v2
"""

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import REPO_ROOT, LLM_PROVIDERS
from negotiation_game.backend.agents.factory import detect_provider, get_api_key_for_provider
from negotiation_judge.schema import GameJudgment
from negotiation_judge.storage import firestore_available, list_all_judgments
from negotiation_judge.consolidate import consolidate_taxonomy, save_taxonomy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.consolidate_v2")

PROMPT_VERSION = "dcaa097"
MODEL = "minimax/minimax-m2.5"
OUTPUT_PATH = REPO_ROOT / "judge" / "output" / "taxonomy_v2.json"


def main() -> None:
    if not firestore_available():
        log.error("Firestore not available.")
        sys.exit(1)

    provider_name = detect_provider(MODEL)
    if not provider_name or provider_name not in LLM_PROVIDERS:
        log.error("Cannot detect provider for '%s'.", MODEL)
        sys.exit(1)
    log.info("Auto-detected provider: %s", provider_name)

    provider = LLM_PROVIDERS[provider_name]
    api_key = get_api_key_for_provider(provider_name)
    if not api_key:
        log.error("No API key for provider '%s'.", provider_name)
        sys.exit(1)

    raw_docs = list_all_judgments()
    raw_docs = [d for d in raw_docs if d.get("prompt_version") == PROMPT_VERSION]
    log.info("Filtered to %d documents with prompt_version=%s", len(raw_docs), PROMPT_VERSION)

    judgments: list[GameJudgment] = []
    for doc in raw_docs:
        try:
            clean = {k: v for k, v in doc.items() if k not in ("judge_model", "judged_at")}
            judgments.append(GameJudgment.model_validate(clean))
        except Exception as e:
            log.warning("Parse error for %s: %s", doc.get("episode_uid", "?"), e)

    log.info("Parsed %d judgments", len(judgments))

    taxonomy = consolidate_taxonomy(
        judgments=judgments,
        api_format=provider["api_format"],
        api_base=provider["api_base"],
        api_key=api_key,
        model=MODEL,
        temperature=0.0,
        output_path=OUTPUT_PATH,
    )
    save_taxonomy(taxonomy, OUTPUT_PATH)
    log.info("Done. Taxonomy written to %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
