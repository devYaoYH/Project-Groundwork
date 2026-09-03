"""a2a-judge: LLM-as-judge analysis layer for a2a-engine episodes.

Two layers, deliberately separated:

- **here** — environment-agnostic machinery: transcript prompt construction and
  judgment storage/resume, usable by any environment whose episodes carry
  ``{speaker, text}`` message events.
- **games/<environment>/** — rubrics, taxonomies, golden sets and prompt versions,
  which are environment-specific by nature (see ``games/negotiation/negotiation_judge``).
"""

from a2a_judge.prompt import JudgeContext, build_transcript_prompt, render_transcript
from a2a_judge.store import (
    FirestoreJudgmentStore,
    JudgmentStore,
    LocalJudgmentStore,
    compress_transcript,
    decompress_transcript,
    judgment_doc_id,
    pending,
)

__all__ = [
    "JudgeContext",
    "build_transcript_prompt",
    "render_transcript",
    "JudgmentStore",
    "LocalJudgmentStore",
    "FirestoreJudgmentStore",
    "judgment_doc_id",
    "compress_transcript",
    "decompress_transcript",
    "pending",
]
