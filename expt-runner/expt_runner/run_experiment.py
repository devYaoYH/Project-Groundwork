"""CLI: load an experiment YAML, expand cells, run games in-process, persist episodes.

Usage:

    a2a-run path/to/experiment.yaml --max-parallelism 4 --results-dir ./results

Games are discovered automatically from installed packages that declare an
``a2a_engine.environments`` entry point, so an experiment can name any installed environment
without the caller importing it first.

Persistence goes through a ``EpisodeStore`` chosen by the experiment's ``storage:``
block, falling back to the environment's registered default and then to local JSON.

Three execution modes, which answer different questions:

``--dry-run``
    *Are my models reachable?* Runs games with their scripted stand-in agents
    and asserts every LLM agent has a usable API key. Writes nothing.

``--smoke-test``
    *Is my data pipeline reachable?* Probes the configured sink, then runs one
    run per cell with scripted agents and **persists** each trace through that
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
    ReleaseReference,
    EpisodeReference,
    EpisodeTrace,
    expand_cells,
    get_environment_spec,
    list_environments,
    load_experiment,
    run_with_parallelism,
)
from a2a_engine._context import current_conversation_id
from a2a_engine.event_sink import current_event_sink, open_event_sink
from a2a_engine.experiment import resolve_storage
from a2a_engine.manifest import EpisodeManifest, git_hash
from a2a_engine.provenance import build_provenance
from a2a_engine.registry import discover_environments
from a2a_engine.seeds import derive_seed
from a2a_engine.storage import check_store, make_store
from a2a_engine.storage.local import LocalJSONStore
from a2a_engine.tracing_otel import get_tracer, init_tracing, shutdown_tracing

log = logging.getLogger("expt_runner")

def _release_facts(environment_id: str | None) -> dict:
    """What the registered release declaration contributes to provenance.

    An environment registered at runtime without a declaration - a test double,
    a scaffold in progress - contributes nothing, and the block records that
    absence rather than inventing an identity for it.
    """
    if not environment_id:
        return {}
    try:
        declaration = get_environment_spec(environment_id).declaration
    except KeyError:
        return {}
    if declaration is None:
        return {}
    return {
        "release_id": declaration.id,
        "release_version": declaration.version,
        "declaration_sha256": declaration.content_sha256(),
        "item_bank_sha256": (
            declaration.item_policy.item_bank_sha256
            if declaration.item_policy is not None else None
        ),
        "oracle_version": declaration.oracle_version,
    }


def _make_run_context(experiment_name: str, cell_id: str, resolved_cfg: dict,
                      episode_idx: int, dry_run: bool, persist: bool = True,
                      attempt: int = 1) -> dict:
    # ``dict(resolved_cfg)`` is a *shallow* copy shared across the cell's
    # episodes, so anything written per episode has to stay top level; a nested
    # mutation would leak across siblings.
    cfg = dict(resolved_cfg)
    # A locked research design is expanded once by the control plane. The
    # execution YAML contains one already-materialised episode per runner cell
    # plus this small marker, so the runner consumes the exact config that was
    # reviewed at lock time instead of deriving a new identity or seed.
    planned = cfg.pop("_design_episode", None)
    if planned is not None and not isinstance(planned, dict):
        raise ValueError("_design_episode must be a mapping when present")
    logical_cell_id = str(planned.get("cell_id") if planned else cell_id)
    logical_episode_idx = int(planned.get("episode_idx") if planned else episode_idx)
    planned_attempt = int(planned.get("attempt") if planned else attempt)
    cfg["experiment_name"] = experiment_name
    cfg["episode_id"] = str(planned.get("episode_id")) if planned else (
        f"{experiment_name}.{cell_id}.{episode_idx}"
    )
    cfg.setdefault("environment_id", cfg.get("environment_id"))
    cfg.setdefault("git_hash", git_hash(Path.cwd()))

    if planned:
        cfg["seed"] = int(planned["seed"])
        cfg["provenance"] = dict(planned["provenance"])
    else:
        # The cell's declared seed is the *root*, not the episode's seed. Without
        # this the N episodes of a cell share one seed, so replicates differ only
        # by sampling noise and an environment that draws a scenario from its seed
        # draws the same one every time.
        root_seed = resolved_cfg.get("seed")
        if root_seed is not None:
            mode = str(resolved_cfg.get("seed_mode") or "derived")
            cfg["seed"] = derive_seed(int(root_seed), cell_id, episode_idx, mode=mode)
            cfg["root_seed"] = int(root_seed)

    # Redis credentials remain process release only.  The resolved config
    # records the stream name so a completed trace can be joined to its
    # operational recovery/replay log without leaking a connection string.
    redis_url = os.environ.get("A2A_REDIS_URL")
    launch_id = os.environ.get("A2A_LAUNCH_ID")
    if redis_url and launch_id:
        episode_id = str(cfg["episode_id"])
        prefix = os.environ.get("A2A_REDIS_STREAM_PREFIX", f"a2a:launch:{launch_id}")
        cfg["event_stream"] = {
            "stream": f"{prefix}:episode:{episode_id}",
            "episode_id": episode_id,
            "launch_id": launch_id,
        }

    # Stamped here, where identity is already stamped, and before any episode
    # runs. An experiment, cell or release identifier not written at expansion
    # is gone forever. ``experiment_id`` and ``design_sha256`` stay null until
    # a design object exists, which is the shape a hand-written config keeps.
    if not planned:
        cfg["provenance"] = build_provenance(
            config=cfg,
            experiment_name=experiment_name,
            cell_id=cell_id,
            episode_idx=episode_idx,
            attempt=attempt,
            release=_release_facts(cfg.get("environment_id")),
        )
    return {"config": cfg, "dry_run": dry_run, "persist": persist,
            "experiment_name": experiment_name,
            "cell_id": logical_cell_id, "episode_idx": logical_episode_idx,
            "attempt": planned_attempt}


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
    declaration is about a environment substituting scripted agents for its dry run,
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
    environment_id = cfg.get("environment_id")
    if not environment_id:
        raise ValueError("config is missing 'environment_id'")
    spec = get_environment_spec(environment_id)

    # The key assertion belongs to --dry-run, whose question is model
    # reachability. A smoke test runs scripted agents to exercise storage, so
    # demanding keys there would fail runs for a reason it is not testing.
    if dry_run and persist is False and spec.dry_run_checks_keys:
        _check_api_keys(cfg)

    # The attempt's identity is fixed before the environment is constructed, so
    # its event log is open from the first event rather than from whenever the
    # run happens to end.
    episode_uid = str(uuid.uuid4())
    sink = open_event_sink(
        results_dir,
        experiment_name=ctx["experiment_name"],
        episode_uid=episode_uid,
        episode_id=str(cfg.get("episode_id") or ""),
        environment_id=environment_id,
    ) if persist else None
    sink_token = current_event_sink.set(sink)
    try:
        return _play(ctx, cfg, spec, store, environment_id, episode_uid, persist, dry_run)
    finally:
        if sink is not None:
            sink.close()
        current_event_sink.reset(sink_token)


def _play(ctx: dict, cfg: dict, spec, store, environment_id: str,
          episode_uid: str, persist: bool, dry_run: bool) -> str:
    environment = spec.cls(config=cfg, dry_run=dry_run)

    tracer = get_tracer()
    with tracer.start_as_current_span(f"environment {environment_id}") as span:
        span.set_attribute("gen_ai.conversation.id", episode_uid)
        span.set_attribute("langfuse.session.id", episode_uid)
        span.set_attribute("a2a.episode.id", str(cfg.get("episode_id") or episode_uid))
        span.set_attribute("a2a.environment.name", str(environment_id))
        release = cfg.get("release") or {}
        if isinstance(release, dict):
            if release.get("id"):
                span.set_attribute("a2a.release.id", str(release["id"]))
            if release.get("release"):
                span.set_attribute("a2a.release.version", str(release["release"]))
            if release.get("content_sha256"):
                span.set_attribute("a2a.release.content_sha256", str(release["content_sha256"]))
        span.set_attribute(
            "langfuse.trace.tags",
            [ctx["experiment_name"], ctx["cell_id"]],
        )
        token = current_conversation_id.set(episode_uid)
        try:
            trace = environment.run()
            context = span.get_span_context()
            if isinstance(trace, EpisodeTrace):
                if isinstance(release, dict) and release.get("id"):
                    trace.release = ReleaseReference.model_validate(release)
                trace.episode = EpisodeReference(
                    id=str(cfg.get("episode_id") or episode_uid),
                    experiment_name=ctx["experiment_name"],
                    cell_id=ctx["cell_id"],
                    episode_idx=ctx["episode_idx"],
                )
                if context.is_valid:
                    trace.observability.update({
                        "otel_trace_id": f"{context.trace_id:032x}",
                        "otel_root_span_id": f"{context.span_id:016x}",
                        "episode_id": str(cfg.get("episode_id") or episode_uid),
                    })
        finally:
            current_conversation_id.reset(token)

    if not isinstance(trace, EpisodeTrace):
        raise TypeError(
            f"Environment {environment_id} returned {type(trace).__name__}, expected EpisodeTrace"
        )
    trace.episode_uid = episode_uid

    if not persist:
        log.info("dry-run: skipping persistence for %s", cfg.get("episode_id"))
        return f"dry-run-{episode_uid}"

    manifest = EpisodeManifest.from_run(
        config=cfg,
        experiment_name=ctx["experiment_name"],
        cell_id=ctx["cell_id"],
        episode_idx=ctx["episode_idx"],
        episode_uid=episode_uid,
        game_package=spec.package,
        repo_root=Path.cwd(),
    )
    uri = store.put_episode(trace, manifest)
    _expire_event_stream(cfg)

    if ctx.get("verify_readback"):
        # The point of a smoke test is proving the trace survives the sink, not
        # merely that put_episode returned without raising.
        restored = store.get_episode(episode_uid)
        if restored is None:
            raise RuntimeError(
                f"{store.name}: trace {episode_uid} was written to {uri} but did not "
                "read back — the sink accepted the write without persisting it"
            )
        if len(restored.events) != len(trace.events):
            raise RuntimeError(
                f"{store.name}: trace {episode_uid} read back with {len(restored.events)} "
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


def _storage_for(spec, args, environment_ids: list[str]) -> dict:
    """Resolve the experiment's sink.

    A environment's registered default only applies when the experiment does not choose
    for itself *and* every cell belongs to that one environment — with several games in
    play there is no principled way to pick between environment-specific defaults, so
    the neutral local store wins and the experiment is expected to be explicit.
    """
    game_default: dict = {}
    if not spec.storage and len(environment_ids) > 1:
        log.info(
            "Experiment spans %d games and declares no storage: block; "
            "using the default local store rather than any one environment's default.",
            len(environment_ids),
        )
    elif len(environment_ids) == 1:
        try:
            game_default = dict(get_environment_spec(environment_ids[0]).storage or {})
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


def _resolve_hooks(environment_ids: list[str]) -> dict:
    """Map each environment in the experiment to its ``resolve_config`` hook, if any."""
    hooks: dict = {}
    for name in environment_ids:
        try:
            hook = get_environment_spec(name).resolve_config
        except KeyError:
            continue
        if hook is not None:
            hooks[name] = hook
            log.info("Using %s's resolve_config hook for cell expansion", name)
    return hooks


def _smoke_test(spec, store, storage_cfg: dict, resolve_hooks: dict,
                args, results_dir: Path) -> int:
    """Preflight the sink, then push one real trace per cell through it.

    Structured as three gates so a failure says which layer broke:

    1. **Sink reachable** — ``store.check()``.
    2. **Cells expand** — presets, sinks and ``resolve_config`` all resolve,
       and every environment named by a cell is actually installed.
    3. **Round-trip** — each cell runs with scripted agents, persists, and is
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
        expanded = expand_cells(spec, resolve_config=resolve_hooks)
    except Exception as exc:
        return fail(f"could not expand cells: {type(exc).__name__}: {exc}")

    contexts: list[dict] = []
    missing: list[str] = []
    for cell, resolved in expanded:
        environment_id = str(resolved.get("environment_id") or "")
        try:
            get_environment_spec(environment_id)
        except KeyError as exc:
            missing.append(f"{cell.label} -> {exc}")
            continue
        for i in range(max(1, args.smoke_episodes_per_cell)):
            ctx = _make_run_context(spec.name, cell.label, resolved, i,
                                    dry_run=True, persist=True)
            ctx["verify_readback"] = True
            contexts.append(ctx)
        print(f"      OK   {cell.label} -> environment={environment_id}")
    for problem in missing:
        print(f"      FAIL {problem}")
    if missing:
        return fail("some cells name games that are not installed.")

    # --- 3. end-to-end round-trip ---
    plural = "run" if len(contexts) == 1 else "runs"
    print(f"\n[3/3] End-to-end write/read via {store.name} "
          f"({len(contexts)} {plural}, scripted agents)")
    results, errors = run_with_parallelism(
        fn=lambda ctx: _run_one(ctx, store, results_dir),
        items=contexts,
        max_workers=args.max_parallelism,
        on_result=lambda ctx, uri: print(
            f"      OK   {ctx['config']['episode_id']}"
            f"  [{ctx['config'].get('environment_id')}] -> {uri}"),
        on_error=lambda ctx, e: print(
            f"      FAIL {ctx['config']['episode_id']}"
            f"  [{ctx['config'].get('environment_id')}]: {e}"),
    )

    games = sorted({c["config"].get("environment_id") for c in contexts})
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
    parser.add_argument("--smoke-episodes-per-cell", type=int, default=1,
                        help="Runs per cell during --smoke-test (default: 1).")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--resume", action="store_true",
                        help="Skip episode_id values already present in local results manifests.")
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

    discover_environments()

    spec = load_experiment(args.yaml_path)
    _configure_observability(spec.observability, results_dir=Path(args.results_dir))
    init_tracing("a2a-engine")
    log.info("Loaded experiment %r with %d cells", spec.name, len(spec.cells))
    log.info("Registered games: %s", list_environments() or "<none>")

    environment_ids = spec.environment_ids()
    log.info("Experiment games: %s", ", ".join(environment_ids) or "<none>")
    resolve_hooks = _resolve_hooks(environment_ids)

    results_dir = Path(args.results_dir)
    storage_cfg = _storage_for(spec, args, environment_ids)
    store = make_store(storage_cfg, results_dir=results_dir)
    log.info("Trace store: %s (%s)", store.name, storage_cfg or "defaults")

    if args.smoke_test:
        return _smoke_test(spec, store, storage_cfg, resolve_hooks, args, results_dir)

    contexts: list[dict] = []
    for cell, resolved in expand_cells(spec, resolve_config=resolve_hooks):
        for i in range(cell.count):
            contexts.append(
                _make_run_context(spec.name, cell.label, resolved, i,
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
        resumable = store if hasattr(store, "completed_episode_ids") else LocalJSONStore(
            results_dir=results_dir
        )
        completed = resumable.completed_episode_ids(spec.name)
        before = len(contexts)
        contexts = [
            ctx for ctx in contexts
            if ctx["config"]["episode_id"] not in completed
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
        on_result=lambda ctx, p: log.info("ok  %s -> %s", ctx["config"]["episode_id"], p),
        on_error=lambda ctx, e: log.error("fail %s: %s", ctx["config"]["episode_id"], e),
    )
    log.info("Done: %d ok, %d failed", len(results), len(errors))
    shutdown_tracing()
    return 0 if not errors else 1


def _configure_observability(config: dict | None, *, results_dir: Path) -> None:
    """Apply typed ExperimentConfig observability before creating a tracer.

    Legacy YAML has no observability block and continues to use release
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
