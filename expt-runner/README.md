# expt-runner

Tiny CLI that loads an experiment YAML, expands cells, runs each environment
in-process via `a2a-engine`, and writes one JSON trace per run.

## Install

```bash
uv pip install -e expt-runner
```

## Usage

```bash
a2a-run path/to/experiment.yaml \
    --max-parallelism 4 \
    --results-dir ./results
```

Or directly:

```bash
uv run python -m expt_runner.run_experiment path/to/experiment.yaml
```

## Flags

| Flag | Default | Effect |
|---|---|---|
| `yaml_path` (positional) | required | Experiment YAML to load |
| `--max-parallelism N` | 4 | Threadpool size; 1 = sequential |
| `--results-dir DIR` | `./results` | Traces are written to `<DIR>/<experiment_name>/<episode_uid>.json` |
| `--dry-run` | off | Check configured model credentials and persist nothing |
| `--smoke-test` | off | Run deterministic stand-ins through sink preflight and read-back |
| `--log-level` | `INFO` | Standard logging level |
| `--resume` | off | Skip runs whose `episode_id` already exists in local episodes/metadata/manifest |
| `--storage-backend BACKEND` | `A2A_STORAGE_BACKEND` | Override the YAML sink (`local`, `sqlite`, `s3`, or `firestore`) |
| `--storage-path PATH` | `A2A_TRACE_DB` | Shorthand for a SQLite trace database |
| `--s3-bucket BUCKET` | `A2A_TRACE_BUCKET` | Shorthand for an S3-backed sink; never required for local runs |

## YAML format

See `a2a-engine/examples/example_experiment.yaml`. Per-cell `config:` is
deep-merged on top of experiment `defaults:`.

## Registering a environment

Environment classes are looked up by `environment_id`. Your benchmark package should
register itself at import time:

```python
# in your_benchmark/__init__.py
from a2a_engine import register_environment
from your_benchmark.environment import CalendarGame
register_environment("calendar", CalendarGame)
```

Then either import that package before invoking `a2a-run`, or wire it through
a Python entry-point in your benchmark's `pyproject.toml`.

A registered environment must satisfy:

```python
class GameCls:
    def __init__(self, config: dict, dry_run: bool = False) -> None: ...
    def run(self) -> EpisodeTrace: ...
```

## Tracing & Langfuse

Tracing is opt-in. With no `OTEL_*` env vars set, the runner installs an
in-memory `TracerProvider` but no exporter, so no spans are shipped (and the
existing JSON trace files are unaffected).

Each run emits:

- a root `environment <environment_id>` span with `gen_ai.conversation.id`, `langfuse.session.id`,
  and `langfuse.trace.tags = [experiment_name, cell_id]`
- a persisted `trace.observability` reference to that root's OTel trace/span IDs
- one `invoke_agent <agent_name>` span per `agent.act()` call
- Calendar's legacy `BaseClient` lifecycle spans (`calendar.client.turn`,
  `calendar.client.decide`, and related protocol transitions)
- one `chat <model>` child span per LLM API call, with full
  `gen_ai.*` attributes (provider, model, usage tokens, finish reasons, errors)

### Local debug

```bash
OTEL_TRACES_EXPORTER=console uv run python run.py experiments/example.yaml --dry-run
```

### Local JSONL episodes

```bash
A2A_OTEL_TRACES_FILE=./results/otel-spans.jsonl \
  uv run python run.py experiments/example.yaml --dry-run
```

Each completed span is appended as one JSON object per line. You can also set
`OTEL_TRACES_EXPORTER=file`, which writes to `./results/otel-episodes.jsonl`.

### Langfuse Cloud

```bash
AUTH=$(printf '%s' "pk-lf-...:sk-lf-..." | base64)
export OTEL_EXPORTER_OTLP_ENDPOINT="https://us.cloud.langfuse.com/api/public/otel"   # or https://cloud.langfuse.com/... for EU
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic ${AUTH},x-langfuse-ingestion-version=4"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
```

### Capture prompts/completions

Local research runs capture message bodies by default as structured
`gen_ai.input.messages` / `gen_ai.output.messages` attributes. This keeps the
OTel projection useful for episode replay alongside the canonical environment trace.
For a reduced logging surface, opt out explicitly:

```bash
export A2A_CAPTURE_CONTENT=false
```

## Resume and S3 trace sync

Every non-dry-run trace is first written locally to:

```text
<results-dir>/<experiment_name>/<episode_uid>.json
```

The runner also writes a small sidecar metadata file:

```text
<results-dir>/<experiment_name>/<episode_uid>.metadata.json
```

and appends the same record to:

```text
<results-dir>/<experiment_name>/_run_manifest.jsonl
```

Use `--resume` to safely re-run an interrupted experiment collection. Completed
`episode_id` values found in local trace JSON, metadata sidecars, or the
manifest are skipped.

S3 upload is optional and best-effort. If the S3 upload fails, the local trace
and metadata remain written and the experiment run continues.

```bash
A2A_TRACE_BUCKET=calbench-a2a-episodes \
AWS_PROFILE=a2a-calendar \
A2A_TRACE_USER=alice \
uv run python run.py experiments/example.yaml --resume
```

Remote episodes are written as:

```text
s3://<bucket>/<prefix>/<uploader>/<experiment_name>/<episode_id>/<episode_uid>.json
s3://<bucket>/<prefix>/<uploader>/<experiment_name>/<episode_id>/<episode_uid>.metadata.json
```
