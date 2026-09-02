# a2a-judge

LLM-as-judge analysis layer for `a2a-engine` traces. Given a `GameRecord`
loaded from a results directory, this package builds a chat-format prompt
suitable for sending to an LLM judge.

## Install

```bash
uv add a2a-judge
# or, in this monorepo, use the path source already wired in pyproject.toml
```

## Usage

```python
from a2a_engine import GameDataset
from a2a_judge import build_transcript_prompt, JudgeContext

ds = GameDataset.from_dir("results/word_guess_example")
messages = build_transcript_prompt(ds[0])
# `messages` is OpenAI-format chat messages: [{"role": "system", ...}, {"role": "user", ...}]

# Or grab the structured context if you want to render your own template:
ctx = JudgeContext.from_record(ds[0])
```

## Coming soon

- A judge runner (parallel fan-out across a dataset, model selection, retry).
- A typed result schema for judge outputs.
- Aggregation utilities (per-experiment / per-batch rollups).
