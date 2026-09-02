"""CLI: load an experiment YAML, expand batches, run games in-process, persist traces.

Usage:

    a2a-run path/to/experiment.yaml --max-parallelism 4 --results-dir ./results

Games are discovered automatically from installed packages that declare an
``a2a_engine.games`` entry point, so an experiment can name any installed game
without the caller importing it first.

Persistence goes through a ``TraceStore`` chosen by the experiment's ``storage:``
block, falling back to the game's registered default and then to local JSON.

Three execution modes, which answer different questions:

``--dry-run``
    *Are my models reachable?* Runs games with their scripted stand-in agents
    and asserts every LLM agent has a usable API key. Writes nothing.

``--smoke-test``
    *Is my data pipeline reachable?* Probes the configured sink, then runs one
    run per batch with scripted agents and **persists** each trace through that
    sink, reading it back to prove the round-trip. Needs no API keys.

(neither)
    The real thing: live models, every run, persisted.
"""

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from a2a_engine import (
    EnvironmentReference,
    EpisodeReference,
    GameTraceBase,
    expand_batches,
    get_game_spec,
    list_games,
    load_experiment,
    run_with_parallelism,
)
from a2a_engine._context import current_conversation_id
from a2a_engine.experiment import resolve_storage
from a2a_engine.manifest import RunManifest, git_hash
from a2a_engine.registry import discover_games
from a2a_engine.storage import check_store, make_store
from a2a_engine.storage.local import LocalJSONStore
from a2a_engine.tracing_otel import get_tracer, init_tracing, shutdown_tracing

log = logging.getLogger("expt_runner")


def _make_run_context(experiment_name: str, batch_label: str, resolved_cfg: dict,
                      run_idx: int, dry_run: bool, persist: bool = True) -> dict:
    cfg = dict(resolved_cfg)
    cfg["experiment_name"] = experiment_name
    cfg["experiment_run_id"] = f"{experiment_name}.{batch_label}.{run_idx}"
    cfg.setdefault("game_name", cfg.get("game_name"))
    cfg.setdefault("git_hash", git_hash(Path.cwd()))
    # Redis credentials remain process environment only.  The resolved config
    # records the stream name so a completed trace can be joined to its
    # operational recovery/replay log without leaking a connection string.
    redis_url = os.environ.get("A2A_REDIS_URL")
    rollout_id = os.environ.get("A2A_ROLLOUT_ID")
    if redis_url and rollout_id:
        episode_id = str(cfg["experiment_run_id"])
        prefix = os.environ.get("A2A_REDIS_STREAM_PREFIX", f"a2a:rollout:{rollout_id}")
        cfg["event_stream"] = {
            "stream": f"{prefix}:episode:{episode_id}",
            "episode_id": episode_id,
            "rollout_id": rollout_id,
        }
    return {"config": cfg, "dry_run": dry_run, "persist": persist,
            "experiment_name": experiment_name,
            "batch_label": batch_label, "run_idx": run_idx}


def _check_api_keys(cfg: dict, *, mode: str = "dry-run") -> None:
    """Validate that each agent config has a non-empty API key. Raises on first missing key.

    Agents without a ``model`` are scripted or heuristic and need no credential,
    so a config that calls no provider passes unconditionally.
    """
    from a2a_engine.llm.factory import detect_provider, get_api_key_for_provider

    agents = cfg.get("agents", [])
    for i, agent_spec in enumerate(agents):
        spec = agent_spec if isinstance(agent_spec, dict) else agent_spec.model_dump()
        model = spec.get("model", "")
        if not model:
            continue
        if spec.get("api_key"):
            continue
        api_format = str(spec.get("api_format") or "")
        if api_format in {"vertexai", "vertexai_anthropic", "vertexai_openai"}:
            log.info("%s: api_format %r uses Google ADC; skipping API key check (agent %d)", mode, api_format, i)
            continue
        provider = detect_provider(model)
        if provider is None:
            # A pool name in `model:` is the likely mistake here: the two are
            # different namespaces, and left alone it fails much later with an
            # empty base URL rather than a name the author can act on.
            if _is_agent_pool_name(model):
                raise EnvironmentError(
                    f"{mode}: agent {i} sets model={model!r}, which is an agent-pool "
                    f"name, not a provider's model string. Remove the inline 'agents' "
                    f"block and declare 'participants: [{model}, ...]' instead."
                )
            log.warning("%s: unknown provider for model %r (agent %d), skipping key check", mode, model, i)
            continue
        if provider in {"gemini_vertexai", "claude_vertexai", "vertexai_openai", "llama_vertexai"}:
            log.info("%s: provider %r uses Google ADC; skipping API key check (agent %d)", mode, provider, i)
            continue
        key = get_api_key_for_provider(provider)
        if not key:
            raise EnvironmentError(
                f"{mode}: missing API key for provider {provider!r} "
                f"(agent {i}, model {model!r}). Set it in the repo-root .env "
                f"before starting the stack."
            )
        log.info("%s: API key present for provider %r (agent %d)", mode, provider, i)


def _is_agent_pool_name(model: str) -> bool:
    """Whether this looks like a pool entry name rather than a model string."""
    from a2a_engine.agent_pool import load_agent_pool

    try:
        return model in load_agent_pool(Path.cwd()).agents
    except Exception:  # pragma: no cover - a broken pool must not mask the run
        return False


def _preflight_credentials(contexts: list[dict]) -> None:
    """Assert every distinct agent line-up in this run has usable credentials.

    Unlike the dry-run check this is not gated on ``dry_run_checks_keys``: that
    declaration is about a game substituting scripted agents for its dry run,
    and says nothing about whether a live run will call a provider.
    """
    seen: set[str] = set()
    for ctx in contexts:
        cfg = ctx["config"]
        agents = cfg.get("agents", [])
        fingerprint = json.dumps(
            [a if isinstance(a, dict) else a.model_dump() for a in agents],
            sort_keys=True, default=str,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        _check_api_keys(cfg, mode="preflight")


def _run_one(ctx: dict, store, results_dir: Path) -> str:
    cfg = ctx["config"]
    dry_run = ctx["dry_run"]
    persist = ctx.get("persist", True)
    game_name = cfg.get("game_name")
    if not game_name:
        raise ValueError("config is missing 'game_name'")
    spec = get_game_spec(game_name)

    # The key assertion belongs to --dry-run, whose question is model
    # reachability. A smoke test runs scripted agents to exercise storage, so
    # demanding keys there would fail runs for a reason it is not testing.
    if dry_run and persist is False and spec.dry_run_checks_keys:
        _check_api_keys(cfg)

    game = spec.cls(config=cfg, dry_run=dry_run)

    game_id = str(uuid.uuid4())
    tracer = get_tracer()
    with tracer.start_as_current_span(f"game {game_name}") as span:
        span.set_attribute("gen_ai.conversation.id", game_id)
        span.set_attribute("langfuse.session.id", game_id)
        span.set_attribute("a2a.episode.id", str(cfg.get("experiment_run_id") or game_id))
        span.set_attribute("a2a.game.name", str(game_name))
        environment = cfg.get("environment") or {}
        if isinstance(environment, dict):
            if environment.get("id"):
                span.set_attribute("a2a.environment.id", str(environment["id"]))
            if environment.get("revision"):
                span.set_attribute("a2a.environment.revision", str(environment["revision"]))
            if environment.get("content_sha256"):
                span.set_attribute("a2a.environment.content_sha256", str(environment["content_sha256"]))
        span.set_attribute(
            "langfuse.trace.tags",
            [ctx["experiment_name"], ctx["batch_label"]],
        )
        token = current_conversation_id.set(game_id)
        try:
            trace = game.run()
            context = span.get_span_context()
            if isinstance(trace, GameTraceBase):
                if isinstance(environment, dict) and environment.get("id"):
                    trace.environment = EnvironmentReference.model_validate(environment)
                trace.episode = EpisodeReference(
                    id=str(cfg.get("experiment_run_id") or game_id),
                    experiment_name=ctx["experiment_name"],
                    batch_label=ctx["batch_label"],
                    run_idx=ctx["run_idx"],
                )
                if context.is_valid:
                    trace.observability.update({
                        "otel_trace_id": f"{context.trace_id:032x}",
                        "otel_root_span_id": f"{context.span_id:016x}",
                        "episode_id": str(cfg.get("experiment_run_id") or game_id),
                    })
        finally:
            current_conversation_id.reset(token)

    if not isinstance(trace, GameTraceBase):
        raise TypeError(
            f"Game {game_name} returned {type(trace).__name__}, expected GameTraceBase"
        )
    trace.game_id = game_id

    if not persist:
        log.info("dry-run: skipping persistence for %s", cfg.get("experiment_run_id"))
        return f"dry-run-{game_id}"

    manifest = RunManifest.from_run(
        config=cfg,
        experiment_name=ctx["experiment_name"],
        batch_label=ctx["batch_label"],
        run_idx=ctx["run_idx"],
        game_id=game_id,
        game_package=spec.package,
        repo_root=Path.cwd(),
    )
    uri = store.put_trace(trace, manifest)
    _expire_event_stream(cfg)

    if ctx.get("verify_readback"):
        # The point of a smoke test is proving the trace survives the sink, not
        # merely that put_trace returned without raising.
        restored = store.get_trace(game_id)
        if restored is None:
            raise RuntimeError(
                f"{store.name}: trace {game_id} was written to {uri} but did not "
                "read back — the sink accepted the write without persisting it"
            )
        if len(restored.events) != len(trace.events):
            raise RuntimeError(
                f"{store.name}: trace {game_id} read back with {len(restored.events)} "
                f"events, expected {len(trace.events)}"
            )
    return uri


def _expire_event_stream(config: dict) -> None:
    """Bound the optional Redis replay-log lifetime after durable persistence."""
    stream = config.get("event_stream")
    ttl = os.environ.get("A2A_REDIS_STREAM_TTL_SECONDS")
    url = os.environ.get("A2A_REDIS_URL")
    if not isinstance(stream, dict) or not stream.get("stream") or not ttl or not url:
        return
    try:
        seconds = int(ttl)
        if seconds > 0:
            from a2a_engine.redis_stream import RedisStreams
            RedisStreams(url).expire(str(stream["stream"]), seconds)
    except Exception:
        log.warning("could not apply Redis stream retention", exc_info=True)


def _storage_for(spec, args, game_names: list[str]) -> dict:
    """Resolve the experiment's sink.

    A game's registered default only applies when the experiment does not choose
    for itself *and* every batch belongs to that one game — with several games in
    play there is no principled way to pick between game-specific defaults, so
    the neutral local store wins and the experiment is expected to be explicit.
    """
    game_default: dict = {}
    if not spec.storage and len(game_names) > 1:
        log.info(
            "Experiment spans %d games and declares no storage: block; "
            "using the default local store rather than any one game's default.",
            len(game_names),
        )
    elif len(game_names) == 1:
        try:
            game_default = dict(get_game_spec(game_names[0]).storage or {})
        except KeyError:
            pass

    overrides: dict = {}
    if args.storage_backend:
        overrides["backend"] = args.storage_backend
    if args.s3_bucket:
        overrides.setdefault("backend", "s3")
        overrides["bucket"] = args.s3_bucket
    if getattr(args, "storage_path", None):
        overrides.setdefault("backend", "sqlite")
        overrides["path"] = args.storage_path

    return resolve_storage(spec, game_default=game_default, overrides=overrides)


def _resolve_hooks(game_names: list[str]) -> dict:
    """Map each game in the experiment to its ``resolve_config`` hook, if any."""
    hooks: dict = {}
    for name in game_names:
        try:
            hook = get_game_spec(name).resolve_config
        except KeyError:
            continue
        if hook is not None:
            hooks[name] = hook
            log.info("Using %s's resolve_config hook for batch expansion", name)
    return hooks


def _smoke_test(spec, store, storage_cfg: dict, resolve_hooks: dict,
                args, results_dir: Path) -> int:
    """Preflight the sink, then push one real trace per batch through it.

    Structured as three gates so a failure says which layer broke:

    1. **Sink reachable** — ``store.check()``.
    2. **Batches expand** — presets, sinks and ``resolve_config`` all resolve,
       and every game named by a batch is actually installed.
    3. **Round-trip** — each batch runs with scripted agents, persists, and is
       read back from the sink.
    """
    banner = "=" * 68
    print(f"\n{banner}\nSMOKE TEST  {spec.name}\n{banner}")

    def fail(message: str) -> int:
        print(f"\nFAILED: {message}")
        shutdown_tracing()  # flush whatever spans the aborted preflight produced
        return 1

    # --- 1. sink reachability ---
    print(f"\n[1/3] Sink reachability  ({storage_cfg or 'defaults'})")
    result = check_store(store)
    print(f"      {result.render()}")
    if not result.ok:
        return fail("the configured sink is not reachable. Nothing was run.")

    # --- 2. expansion ---
    print("\n[2/3] Experiment expansion")
    try:
        expanded = expand_batches(spec, resolve_config=resolve_hooks)
    except Exception as exc:
        return fail(f"could not expand batches: {type(exc).__name__}: {exc}")

    contexts: list[dict] = []
    missing: list[str] = []
    for batch, resolved in expanded:
        game_name = str(resolved.get("game_name") or "")
        try:
            get_game_spec(game_name)
        except KeyError as exc:
            missing.append(f"{batch.label} -> {exc}")
            continue
        for i in range(max(1, args.smoke_runs_per_batch)):
            ctx = _make_run_context(spec.name, batch.label, resolved, i,
                                    dry_run=True, persist=True)
            ctx["verify_readback"] = True
            contexts.append(ctx)
        print(f"      OK   {batch.label} -> game={game_name}")
    for problem in missing:
        print(f"      FAIL {problem}")
    if missing:
        return fail("some batches name games that are not installed.")

    # --- 3. end-to-end round-trip ---
    plural = "run" if len(contexts) == 1 else "runs"
    print(f"\n[3/3] End-to-end write/read via {store.name} "
          f"({len(contexts)} {plural}, scripted agents)")
    results, errors = run_with_parallelism(
        fn=lambda ctx: _run_one(ctx, store, results_dir),
        items=contexts,
        max_workers=args.max_parallelism,
        on_result=lambda ctx, uri: print(
            f"      OK   {ctx['config']['experiment_run_id']}"
            f"  [{ctx['config'].get('game_name')}] -> {uri}"),
        on_error=lambda ctx, e: print(
            f"      FAIL {ctx['config']['experiment_run_id']}"
            f"  [{ctx['config'].get('game_name')}]: {e}"),
    )

    games = sorted({c["config"].get("game_name") for c in contexts})
    print(f"\n{banner}")
    print(f"{'PASS' if not errors else 'FAIL'}  "
          f"{len(results)}/{len(contexts)} runs persisted and read back  "
          f"| games: {', '.join(str(g) for g in games)}  | sink: {store.name}")
    print(banner + "\n")
    shutdown_tracing()
    return 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an a2a-engine experiment YAML.")
    parser.add_argument("yaml_path", help="Path to experiment YAML file")
    parser.add_argument("--max-parallelism", type=int, default=4)
    parser.add_argument("--results-dir", default="./results")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check LLM API keys and run scripted agents; persist nothing.")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Probe the configured sink, then run scripted agents and "
                             "persist through it, verifying each trace reads back. "
                             "Needs no API keys.")
    parser.add_argument("--smoke-runs-per-batch", type=int, default=1,
                        help="Runs per batch during --smoke-test (default: 1).")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--resume", action="store_true",
                        help="Skip experiment_run_id values already present in local results manifests.")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="Zero-based shard index to run after expanding the experiment.")
    parser.add_argument("--shard-count", type=int, default=1,
                        help="Total number of shards used to partition expanded runs.")
    parser.add_argument("--storage-backend", default=os.environ.get("A2A_STORAGE_BACKEND"),
                        help="Override the trace store backend (local | sqlite | s3 | firestore).")
    parser.add_argument("--s3-bucket", default=os.environ.get("A2A_TRACE_BUCKET"),
                        help="Shorthand for --storage-backend s3 with this bucket.")
    parser.add_argument("--storage-path", default=os.environ.get("A2A_TRACE_DB"),
                        help="Shorthand for --storage-backend sqlite with this database file.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.shard_count < 1:
        parser.error("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        parser.error("--shard-index must satisfy 0 <= index < shard-count")

    discover_games()

    spec = load_experiment(args.yaml_path)
    _configure_observability(spec.observability, results_dir=Path(args.results_dir))
    init_tracing("a2a-engine")
    log.info("Loaded experiment %r with %d batches", spec.name, len(spec.batches))
    log.info("Registered games: %s", list_games() or "<none>")

    game_names = spec.game_names()
    log.info("Experiment games: %s", ", ".join(game_names) or "<none>")
    resolve_hooks = _resolve_hooks(game_names)

    results_dir = Path(args.results_dir)
    storage_cfg = _storage_for(spec, args, game_names)
    store = make_store(storage_cfg, results_dir=results_dir)
    log.info("Trace store: %s (%s)", store.name, storage_cfg or "defaults")

    if args.smoke_test:
        return _smoke_test(spec, store, storage_cfg, resolve_hooks, args, results_dir)

    contexts: list[dict] = []
    for batch, resolved in expand_batches(spec, resolve_config=resolve_hooks):
        for i in range(batch.count):
            contexts.append(
                _make_run_context(spec.name, batch.label, resolved, i,
                                  args.dry_run, persist=not args.dry_run)
            )

    if args.shard_count > 1:
        before = len(contexts)
        contexts = [
            ctx for idx, ctx in enumerate(contexts)
            if idx % args.shard_count == args.shard_index
        ]
        log.info(
            "Shard enabled: running shard %d/%d with %d of %d expanded runs",
            args.shard_index, args.shard_count, len(contexts), before,
        )

    if args.resume:
        # Ask the configured sink what it already holds; only fall back to the
        # JSON tree for backends that cannot answer (resuming against S3 still
        # reads the local mirror, which is where its manifests live).
        resumable = store if hasattr(store, "completed_run_ids") else LocalJSONStore(
            results_dir=results_dir
        )
        completed = resumable.completed_run_ids(spec.name)
        before = len(contexts)
        contexts = [
            ctx for ctx in contexts
            if ctx["config"]["experiment_run_id"] not in completed
        ]
        log.info("Resume enabled: skipping %d completed runs, %d remaining",
                 before - len(contexts), len(contexts))

    if not contexts:
        log.warning("No runs to execute.")
        return 0

    # A live run reaches real providers. Checking credentials up front turns a
    # per-episode "HTTP Error 401: Unauthorized" from deep inside the client
    # into one message naming the provider, the agent and the model.
    if not args.dry_run:
        try:
            _preflight_credentials(contexts)
        except EnvironmentError as exc:
            log.error("%s", exc)
            return 1

    log.info("Launching %d runs (max_parallelism=%d)", len(contexts), args.max_parallelism)
    results, errors = run_with_parallelism(
        fn=lambda ctx: _run_one(ctx, store, results_dir),
        items=contexts,
        max_workers=args.max_parallelism,
        on_result=lambda ctx, p: log.info("ok  %s -> %s", ctx["config"]["experiment_run_id"], p),
        on_error=lambda ctx, e: log.error("fail %s: %s", ctx["config"]["experiment_run_id"], e),
    )
    log.info("Done: %d ok, %d failed", len(results), len(errors))
    shutdown_tracing()
    return 0 if not errors else 1


def _configure_observability(config: dict | None, *, results_dir: Path) -> None:
    """Apply typed ExperimentConfig observability before creating a tracer.

    Legacy YAML has no observability block and continues to use environment
    variables untouched. The typed config is explicit experiment input, so it
    wins when present and is then retained in the normalized run config.
    """
    if config is None:
        return
    if "capture_content" in config:
        os.environ["A2A_CAPTURE_CONTENT"] = "true" if config["capture_content"] else "false"
    local_spans_path = config.get("local_spans_path")
    if local_spans_path:
        os.environ["A2A_OTEL_TRACES_FILE"] = str(local_spans_path)
    elif not os.environ.get("A2A_OTEL_TRACES_FILE"):
        # Typed experiments are local-first by contract. Compose supplies a
        # shared /data path; direct CLI runs get a portable sibling projection.
        os.environ["A2A_OTEL_TRACES_FILE"] = str(results_dir / "otel-spans.jsonl")
    if os.environ.get("A2A_OTEL_TRACES_FILE"):
        os.environ["OTEL_TRACES_EXPORTER"] = "file"


if __name__ == "__main__":
    sys.exit(main())
