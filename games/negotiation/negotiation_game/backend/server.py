"""
HTTP server for the Negotiation Game.

Uses http.server for REST endpoints. Event delivery via polling.
No external dependencies required.
"""

import asyncio
import copy
import json
import http.server
import logging
import random
import re
import subprocess
import threading
import os
import traceback
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import (
    DEFAULT_PORT,
    DEFAULT_PROJECTS_A,
    DEFAULT_PROJECTS_B,
    API_MAX_RETRIES,
    API_BACKOFF_BASE,
    API_BACKOFF_MAX,
    API_REQUEST_COOLDOWN,
    FIRESTORE_COLLECTION,
)
from negotiation_game.backend.engine import GameConfig, GameEngine, GameMode
from negotiation_game.backend.agents import make_agent
from negotiation_game.backend.agents.human import HumanAgent
from negotiation_game.backend.storage import (
    save_game, load_all_games, save_to_firestore, firestore_available,
    list_traces, get_trace, save_visitor_signup, update_visitor_result,
)

from pathlib import Path
from negotiation_analysis.data_loader import reconstruct_transcript_from_events

log = logging.getLogger("negotiation")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATIC_DIR = _PROJECT_ROOT / "webapp" / "static"
_ALLOWED_POOL_DIR = _PROJECT_ROOT / "data" / "scenario_pools"

games = load_all_games()
game_events = {}
game_configs: dict[str, dict] = {}  # game_id -> raw config payload (for Firestore)
game_consent: dict[str, bool] = {}  # game_id -> consent flag
human_agents: dict[str, dict[str, HumanAgent]] = {}  # game_id -> agent_id -> HumanAgent
game_engines: dict[str, GameEngine] = {}  # game_id -> engine (for stop)
batch_swaps: dict[str, bool] = {}  # game_id -> whether turn order was swapped
game_visitors: dict[str, str] = {}  # game_id -> visitor email (demo signups)

# M/C ratios sampled when a request asks for target_mc_ratio: "random".
# Matches the competitive / mixed / collaborative cells from the paper.
RANDOM_MC_CHOICES = (0.5, 0.8, 1.0)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- Async game runner ---
_loop = None


def get_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop


def run_game_async(config, agent_a, agent_b, agents_cfg, consent, experiment_label="", visitor_email=""):
    engine = GameEngine(config, agent_a, agent_b)
    gid = config.game_id

    # Build firestore config AFTER engine init (pool may update config fields)
    firestore_cfg = _build_firestore_cfg(config, agents_cfg, experiment_label)
    game_configs[gid] = firestore_cfg
    game_consent[gid] = consent

    if visitor_email:
        game_visitors[gid] = visitor_email
        human_side = "agent_a" if isinstance(agent_a, HumanAgent) else (
            "agent_b" if isinstance(agent_b, HumanAgent) else None)
        save_visitor_signup(gid, visitor_email, {
            "opponent_model": next((c.get("model") for c in agents_cfg if c.get("type") == "llm"), None),
            "human_side": human_side,
            "num_rounds": config.num_rounds,
            "target_mc_ratio": config.target_mc_ratio,
            "enable_cheap_talk": config.enable_cheap_talk,
            "full_transparency": config.full_transparency,
            "maximize_joint": config.maximize_joint,
            "experiment_label": experiment_label,
            "consent": consent,
        })

    game_events[gid] = []
    game_engines[gid] = engine

    async def on_event(etype, data):
        ev = {"type": etype, "data": data}
        game_events[gid].append(ev)
        log.info("[%s] event: %s", gid[:8], etype)

    engine.on_event(on_event)

    async def run():
        log.info("[%s] game starting (%s)", gid[:8], config.mode.value)
        try:
            result = await engine.run_game()
            games[gid] = result
            save_game(gid, result, game_events[gid])
            consent_val = game_consent.get(gid)
            log.info("[%s] consent=%s, firestore_available=%s", gid[:8], consent_val, firestore_available())
            if consent_val:
                save_to_firestore(gid, game_configs.get(gid, {}), result, game_events[gid])
            if gid in game_visitors:
                update_visitor_result(gid, {
                    "agent_a_reward": result.get("agent_a_cumulative_reward"),
                    "agent_b_reward": result.get("agent_b_cumulative_reward"),
                    "oracle_stats": config.oracle_stats,
                })
                game_visitors.pop(gid, None)
            human_agents.pop(gid, None)
            game_engines.pop(gid, None)
            log.info("[%s] game complete — A=%.1f B=%.1f",
                     gid[:8], result["agent_a_cumulative_reward"],
                     result["agent_b_cumulative_reward"])
        except Exception as e:
            traceback.print_exc()
            log.error("[%s] game error: %s", gid[:8], e)
            ev = {"type": "error", "data": {"message": str(e)}}
            game_events[gid].append(ev)
            game_engines.pop(gid, None)

    asyncio.run_coroutine_threadsafe(run(), get_loop())


def _normalize_config(body: dict) -> dict:
    """Normalize old-style (agent_a/agent_b) or new-style (agents list) config into
    a consistent dict with agent_values, agent_shifting, agent configs list, etc."""
    normalized = dict(body)

    # Build agent_shifting
    if "agent_shifting" not in normalized:
        mode = normalized.get("mode", "stable")
        if mode == "shifting":
            normalized["agent_shifting"] = [False, True]
        else:
            normalized["agent_shifting"] = [False, False]

    # Build agents list from old format
    if "agents" not in normalized:
        agent_a_cfg = normalized.pop("agent_a", {"type": "random"})
        agent_b_cfg = normalized.pop("agent_b", {"type": "random"})
        normalized["agents"] = [agent_a_cfg, agent_b_cfg]

    return normalized


def _validate_pool_path(pool_path: str) -> str:
    """Resolve and validate a scenario_pool_path from an untrusted request body.
    Only JSON files under data/scenario_pools/ are allowed; raises ValueError otherwise."""
    resolved = os.path.realpath(os.path.join(_PROJECT_ROOT, pool_path))
    if not (
        resolved.startswith(_ALLOWED_POOL_DIR + os.sep)
        and resolved.endswith(".json")
        and os.path.isfile(resolved)
    ):
        raise ValueError(
            f"scenario_pool_path not allowed: {pool_path!r} — must be a .json file under data/scenario_pools/"
        )
    return resolved


def _build_firestore_cfg(config: GameConfig, agents_cfg: list[dict], experiment_label: str = "") -> dict:
    """Build the Firestore config dict from a GameConfig (after engine init)."""
    cfg = {
        "mode": config.mode.value,
        "num_rounds": config.num_rounds,
        "cheap_talk_turns": config.cheap_talk_turns,
        "resource_types": config.resource_types,
        "resource_supply": config.resource_supply,
        "resource_costs": config.resource_costs,
        "agent_budget": config.agent_budget,
        "max_resource_types_per_turn": config.max_resource_types_per_turn,
        "agent_shifting": config.agent_shifting,
        "first_speaker": config.first_speaker,
        "agents": [{"type": c.get("type", "random"), **({"model": c["model"]} if "model" in c else {})} for c in agents_cfg],
        "goal": config.goal,
        "seed": config.seed,
        "swapped": config.swapped,
        "thinking": config.thinking,
        "visible_utilities": config.visible_utilities,
        "visible_opponent_reward": config.visible_opponent_reward,
        "enable_cheap_talk": config.enable_cheap_talk,
        "share_projects": config.share_projects,
        "think_about_opponent": config.think_about_opponent,
        "maximize_joint": config.maximize_joint,
        "full_transparency": config.full_transparency,
        "named_projects": config.named_projects,
        "experiment_label": experiment_label,
        "experiment_run_id": config.experiment_run_id,
        "experiment_name": config.experiment_name,
        "git_hash": config.git_hash,
    }
    cfg["agent_projects"] = config.agent_projects
    if config.rotate_projects:
        cfg["rotate_projects"] = True
        cfg["scenario_pool_path"] = config.scenario_pool_path
        cfg["target_mc_ratio"] = config.target_mc_ratio
    if config.scenario_synergy:
        cfg["scenario_synergy"] = config.scenario_synergy
    if config.oracle_stats:
        cfg["oracle_stats"] = config.oracle_stats
    return cfg


def _resolve_target_mc(value):
    """Resolve a target_mc_ratio request value. The literal "random" samples a
    hidden ratio server-side so demo players cannot know the scenario condition."""
    if value == "random":
        return random.choice(RANDOM_MC_CHOICES)
    return value


def _validate_visitor_email(normalized: dict) -> str:
    """Pop and validate visitor_email from a request body. Demo games
    (experiment_label=poster_demo) require one; other flows may omit it."""
    email = (normalized.pop("visitor_email", "") or "").strip()
    is_demo = normalized.get("experiment_label") == "poster_demo"
    if not email:
        if is_demo:
            raise ValueError("visitor_email is required to play the demo — enter your email to try the game")
        return ""
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError(f"visitor_email does not look like a valid email address: {email!r}")
    return email


def _start_single_game(body: dict) -> dict:
    """Create and launch a single game from a normalized config body.
    Returns {"game_id": ..., "status": "started"}.
    """
    normalized = _normalize_config(body)
    if normalized.get("scenario_pool_path"):
        normalized["scenario_pool_path"] = _validate_pool_path(normalized["scenario_pool_path"])
    visitor_email = _validate_visitor_email(normalized)
    consent = normalized.get("consent", False)
    agents_cfg = normalized.get("agents", [{"type": "random"}, {"type": "random"}])
    fs = normalized.get("first_speaker", 0)

    config = GameConfig(
        mode=GameMode(normalized.get("mode", "stable")),
        num_rounds=normalized.get("num_rounds", 5),
        cheap_talk_turns=normalized.get("cheap_talk_turns", 3),
        resource_types=normalized.get("resource_types", ["wood", "stone", "gold"]),
        resource_supply=normalized.get("resource_supply", {"wood": 10, "stone": 10, "gold": 6}),
        resource_costs=normalized.get("resource_costs", {"wood": 1.0, "stone": 1.5, "gold": 3.0}),
        agent_budget=normalized.get("agent_budget", 15.0),
        max_resource_types_per_turn=normalized.get("max_resource_types_per_turn", 2),
        agent_projects=normalized.get("agent_projects") or [list(DEFAULT_PROJECTS_A), list(DEFAULT_PROJECTS_B)],
        oracle_stats=normalized.get("oracle_stats"),
        scenario_synergy=normalized.get("scenario_synergy"),
        agent_shifting=normalized.get("agent_shifting"),
        first_speaker=fs,
        goal=normalized.get("goal") or "",
        thinking=normalized.get("thinking", True),
        visible_utilities=normalized.get("visible_utilities", False),
        visible_opponent_reward=normalized.get("visible_opponent_reward", True),
        enable_cheap_talk=normalized.get("enable_cheap_talk", True),
        share_projects=normalized.get("share_projects", False),
        think_about_opponent=normalized.get("think_about_opponent", False),
        maximize_joint=normalized.get("maximize_joint", False),
        full_transparency=normalized.get("full_transparency", False),
        named_projects=normalized.get("named_projects", False),
        seed=normalized.get("seed"),
        swapped=normalized.get("swapped", False),
        scenario_pool_path=normalized.get("scenario_pool_path"),
        target_mc_ratio=_resolve_target_mc(normalized.get("target_mc_ratio") or normalized.get("mc_ratio")),
        rotate_projects=normalized.get("rotate_projects", False),
        experiment_run_id=normalized.get("experiment_run_id"),
        experiment_name=normalized.get("experiment_name"),
        git_hash=normalized.get("git_hash"),
    )


    # Create agent implementations, ordered by first_speaker
    agent_impls = [make_agent(c) for c in agents_cfg]
    agent_a_impl = agent_impls[fs]
    agent_b_impl = agent_impls[1 - fs]

    human_agents_for_game = {}
    if isinstance(agent_a_impl, HumanAgent):
        human_agents_for_game["agent_a"] = agent_a_impl
    if isinstance(agent_b_impl, HumanAgent):
        human_agents_for_game["agent_b"] = agent_b_impl
    if human_agents_for_game:
        human_agents[config.game_id] = human_agents_for_game

    log.info("[%s] game start — agents: %s vs %s (first_speaker=%d, seed=%s, swapped=%s)",
             config.game_id[:8],
             agents_cfg[0].get("type", "random"),
             agents_cfg[1].get("type", "random"),
             fs, config.seed, config.swapped)

    run_game_async(config, agent_a_impl, agent_b_impl, agents_cfg, consent,
                   experiment_label=normalized.get("experiment_label", ""),
                   visitor_email=visitor_email)
    return {"game_id": config.game_id, "status": "started"}


# --- Static file serving ---
_LAUNCH_SECRET = os.environ.get("LAUNCH_SECRET", "")


class GameHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _check_launch_secret(self) -> bool:
        """Return True if the request is authorized to launch games.
        If LAUNCH_SECRET is not set, all requests are allowed (dev mode)."""
        if not _LAUNCH_SECRET:
            return True
        return self.headers.get("X-Launch-Secret") == _LAUNCH_SECRET

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Launch-Secret")
        self.end_headers()

    def _parse_query(self, key: str) -> str:
        qs = urlparse(self.path).query
        for param in qs.split("&"):
            if param.startswith(f"{key}="):
                return param.split("=", 1)[1]
        return ""

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        if path == "/health":
            return self._json({"status": "ok"})

        if path == "/api/consent/status":
            return self._json({"firestore_available": firestore_available()})

        if path == "/api/config":
            return self._json({"firestore_collection": FIRESTORE_COLLECTION})

        if path == "/api/games":
            results = []
            for gid, data in games.items():
                results.append({
                    "game_id": gid,
                    "mode": data.get("mode", "?"),
                    "num_rounds": data.get("num_rounds", 0),
                    "agent_a_reward": data.get("agent_a_cumulative_reward", 0),
                    "agent_b_reward": data.get("agent_b_cumulative_reward", 0),
                })
            for gid in game_events:
                if gid not in games:
                    results.append({"game_id": gid, "mode": "in_progress",
                                    "num_rounds": 0, "agent_a_reward": 0, "agent_b_reward": 0})
            return self._json(results)

        # Batch status: /api/batch/status?ids=id1,id2,id3
        if path == "/api/batch/status":
            ids_str = self._parse_query("ids")
            if not ids_str:
                return self._json({"error": "Missing 'ids' query parameter"}, 400)
            game_ids = [gid.strip() for gid in ids_str.split(",") if gid.strip()]
            result_games = []
            all_done = True
            for gid in game_ids:
                done = gid in games
                if not done:
                    all_done = False
                swapped = batch_swaps.get(gid, False)
                entry = {"game_id": gid, "done": done, "swapped": swapped}
                if done:
                    g = games[gid]
                    a_reward = g.get("agent_a_cumulative_reward", 0)
                    b_reward = g.get("agent_b_cumulative_reward", 0)
                    # Un-swap rewards so they refer to original agent identities
                    if swapped:
                        a_reward, b_reward = b_reward, a_reward
                    entry["agent_a_reward"] = a_reward
                    entry["agent_b_reward"] = b_reward
                result_games.append(entry)
            return self._json({"games": result_games, "all_done": all_done})

        # Event polling: /api/game/{id}/events?after=N
        if path.startswith("/api/game/") and path.endswith("/events"):
            gid = path.split("/")[3]
            after = int(self._parse_query("after") or "0")
            evts = game_events.get(gid, [])
            return self._json({
                "events": evts[after:],
                "total": len(evts),
                "done": gid in games,
            })

        if path.startswith("/api/game/"):
            gid = path.split("/")[3]
            if gid in games:
                return self._json(games[gid])
            if gid in game_events:
                return self._json({"game_id": gid, "status": "in_progress",
                                    "events_so_far": len(game_events[gid])})
            return self._json({"error": "Not found"}, 404)

        # Dataset traces from Firestore
        if path == "/api/traces":
            limit = int(self._parse_query("limit") or "50")
            start_after = self._parse_query("start_after") or None
            offset = int(self._parse_query("offset") or "0")
            traces, total = list_traces(limit=min(limit, 200), start_after=start_after, offset=offset)
            return self._json({"traces": traces, "count": len(traces), "total": total})

        if path.startswith("/api/judge/"):
            from judge.storage import (
                get_judgment, get_judgment_by_doc_id, list_judgment_versions,
                firestore_available as judge_fs_available,
            )
            if not judge_fs_available():
                return self._json({"error": "Judge Firestore not available"}, 503)

            parts = path.split("/")
            # /api/judge/<game_id>/versions
            if len(parts) >= 5 and parts[4] == "versions":
                game_id = parts[3]
                versions = list_judgment_versions(game_id)
                return self._json({"versions": versions})

            # /api/judge/<game_id>/version/<doc_id>  (doc_id may contain __)
            if len(parts) >= 5 and parts[4] == "version":
                doc_id = "/".join(parts[5:])
                judgment = get_judgment_by_doc_id(doc_id)
                if judgment:
                    return self._json(_decompress_judgment_transcript(judgment))
                return self._json({"error": "No judgment found"}, 404)

            # /api/judge/<game_id>  — latest version
            game_id = parts[3]
            judgment = get_judgment(game_id)
            if judgment:
                return self._json(_decompress_judgment_transcript(judgment))
            return self._json({"error": "No judgment found"}, 404)

        if path.startswith("/api/traces/"):
            trace_id = path.split("/")[3]
            trace = get_trace(trace_id)
            if trace:
                # Reconstruct transcript from events for V2+ schemas
                schema_version = trace.get("schema_version", 1)
                if schema_version >= 2:
                    # V3: Decompress events if compressed
                    if "events_compressed" in trace:
                        from negotiation_game.backend.storage import decompress_events
                        events = decompress_events(trace["events_compressed"])
                    else:
                        events = trace.get("events", [])

                    result = trace.get("result", {})
                    rounds = result.get("rounds", [])
                    for rnd in rounds:
                        # If transcript not present or None, reconstruct from events
                        if not rnd.get("cheap_talk_transcript"):
                            round_num = rnd["round_number"]
                            rnd["cheap_talk_transcript"] = reconstruct_transcript_from_events(
                                events, round_num
                            )
                    # Extract prompt events for the dataset viewer
                    prompt_types = {"system_prompt", "project_instructions"}
                    trace["prompt_events"] = [
                        e for e in events if e.get("type") in prompt_types
                    ]
                return self._json(trace)
            return self._json({"error": "Trace not found"}, 404)

        # Static files
        fp = path.lstrip("/") or "index.html"
        full = os.path.join(_STATIC_DIR, fp)
        if os.path.isfile(full):
            ct = {"html": "text/html", "js": "application/javascript",
                  "css": "text/css", "json": "application/json",
                  "png": "image/png", "jpg": "image/jpeg",
                  "svg": "image/svg+xml"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        # Guard all game-launching endpoints with LAUNCH_SECRET
        _launch_paths = {"/api/game/start", "/api/batch/start",
                         "/api/scenario/generate", "/api/scenario/oracle-solve",
                         "/api/scenario/name-projects"}
        if path in _launch_paths and not self._check_launch_secret():
            return self._json({"error": "Unauthorized: missing or invalid X-Launch-Secret header"}, 401)

        # Single game start (backward compatible)
        if path == "/api/game/start":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                result = _start_single_game(body)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(result)

        # Batch start
        if path == "/api/batch/start":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            count = body.get("count", 0)
            if not isinstance(count, int) or count < 1 or count > 100:
                return self._json({"error": "count must be an integer between 1 and 100"}, 400)

            config = body.get("config", {})
            balance = body.get("balance_turn_order", False)
            experiment_label = body.get("experiment_label", "")

            # Reject human agents in batch
            agents_cfg = config.get("agents", [
                config.get("agent_a", {"type": "random"}),
                config.get("agent_b", {"type": "random"}),
            ])
            for ac in agents_cfg:
                if ac.get("type") == "human":
                    return self._json({"error": "Human agents are not supported in batch mode"}, 400)

            # Batch-level seed for deterministic per-game seed generation
            batch_seed = body.get("seed")
            rng = random.Random(batch_seed)

            game_ids = []
            swapped_flags = []
            last_pair_seed = None

            for i in range(count):
                game_config = copy.deepcopy(config)

                # For balanced pairs, use the same seed for both games to ensure
                # they sample the same scenario from the pool (swapping only affects assignment)
                if balance:
                    if i % 2 == 0:
                        # Even index: generate new seed for this pair
                        last_pair_seed = rng.randint(0, 2**31 - 1)
                        game_config["seed"] = last_pair_seed
                    else:
                        # Odd index: reuse seed from previous (non-swapped) game
                        game_config["seed"] = last_pair_seed
                else:
                    game_config["seed"] = rng.randint(0, 2**31 - 1)

                game_config["experiment_label"] = experiment_label

                swapped = balance and (i % 2 == 1)
                if swapped:
                    game_config["first_speaker"] = 1
                    game_config["swapped"] = True
                else:
                    game_config["first_speaker"] = 0
                    game_config["swapped"] = False

                try:
                    result = _start_single_game(game_config)
                except ValueError as e:
                    return self._json({"error": str(e), "started_game_ids": game_ids}, 400)
                gid = result["game_id"]
                game_ids.append(gid)
                swapped_flags.append(swapped)
                batch_swaps[gid] = swapped

            log.info("Batch started: %d games (balance=%s, seed=%s)", count, balance, batch_seed)
            return self._json({
                "batch_size": count,
                "game_ids": game_ids,
                "swapped": swapped_flags,
                "status": "started",
            })

        # Generate scenario via SA solver
        if path == "/api/scenario/generate":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                from negotiation_game.backend.optimizer import generate_scenario
                result = generate_scenario(
                    target_mc=body.get("target_mc", 0.75),
                    resource_types=body.get("resource_types", ["wood", "stone", "gold"]),
                    resource_costs=body.get("resource_costs", {"wood": 1.0, "stone": 1.5, "gold": 3.0}),
                    resource_supply=body.get("resource_supply", {"wood": 10, "stone": 10, "gold": 6}),
                    cash_per_player=body.get("cash_per_player", 20.0),
                    num_projects=body.get("num_projects", 4),
                    max_types=body.get("max_resource_types_per_turn", 2),
                )
                # name_projects already called inside generate_scenario
                return self._json(result)
            except Exception as e:
                log.error("Scenario generation failed: %s", e)
                return self._json({"error": str(e)}, 500)

        # Oracle solve via LLM
        if path == "/api/scenario/oracle-solve":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                from negotiation_game.backend.optimizer import oracle_solve
                result = oracle_solve(
                    agent_projects=body.get("agent_projects", []),
                    resource_types=body.get("resource_types", []),
                    resource_supply=body.get("resource_supply", {}),
                    resource_costs=body.get("resource_costs", {}),
                    agent_budget=body.get("agent_budget", 20),
                    scenario_synergy=body.get("scenario_synergy"),
                    agent_config=body.get("agent_config", {}),
                    max_resource_types=body.get("max_resource_types_per_turn", 2),
                )
                return self._json(result)
            except Exception as e:
                log.error("Oracle solve failed: %s %s", type(e).__name__, e)
                return self._json({"error": str(e)}, 500)

        # Name projects via LLM
        if path == "/api/scenario/name-projects":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                from negotiation_game.backend.optimizer import name_projects
                agent_projects = body.get("agent_projects", [])
                named = name_projects(agent_projects)
                return self._json({"agent_projects": named})
            except Exception as e:
                log.error("Project naming failed: %s %s", type(e).__name__, e)
                return self._json({"agent_projects": body.get("agent_projects", [])})

        # Human player input
        if path.startswith("/api/game/") and path.endswith("/input"):
            gid = path.split("/")[3]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            human_map = human_agents.get(gid, {})
            requested_agent = body.get("agent")
            ha = human_map.get(requested_agent) if requested_agent else None
            if ha is None and len(human_map) == 1:
                ha = next(iter(human_map.values()))
            if ha:
                log.info(
                    "[%s] human input for %s: %s",
                    gid[:8],
                    requested_agent or "sole_human",
                    body.get("text", "")[:80],
                )
                ha.submit_input(body.get("text", ""))
                return self._json({"ok": True})
            return self._json({"error": "No human agent for this game"}, 404)

        # Stop a running game: POST /api/game/{id}/stop
        if path.startswith("/api/game/") and path.endswith("/stop"):
            gid = path.split("/")[3]
            engine = game_engines.get(gid)
            if engine:
                log.info("[%s] stop requested", gid[:8])
                engine.stop()
                return self._json({"ok": True, "game_id": gid})
            return self._json({"error": "No running game with that ID"}, 404)

        self.send_error(404)


def _decompress_judgment_transcript(judgment: dict) -> dict:
    """Replace input_transcript_gz with decompressed input_transcript before sending to client."""
    gz = judgment.pop("input_transcript_gz", None)
    if gz:
        try:
            from judge.storage import decompress_transcript
            judgment["input_transcript"] = decompress_transcript(gz)
        except Exception:
            pass  # non-fatal: frontend just won't show the transcript
    return judgment


def _git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _print_startup_banner(port: int):
    from negotiation_game.backend.schemas import FirestoreDocumentSchema
    git_hash = _git_hash()
    schema_version = FirestoreDocumentSchema.model_fields['schema_version'].default
    log.info("=" * 56)
    log.info("Negotiation Game Server")
    log.info("  url:       http://localhost:%d", port)
    log.info("  git:       %s", git_hash)
    log.info("  schema:    v%d", schema_version)
    log.info("  retries:   %d attempts, backoff %g–%gs, cooldown %gs",
             API_MAX_RETRIES, API_BACKOFF_BASE, API_BACKOFF_MAX, API_REQUEST_COOLDOWN)
    log.info("  cache:     anthropic prompt caching enabled (breakpoint: last msg)")
    log.info("=" * 56)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="  %(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), GameHandler)
    _print_startup_banner(port)
    server.serve_forever()


if __name__ == "__main__":
    main()
