"""Game registry. Downstream benchmarks register their game class by name.

A "game" is any callable/class with the contract:

    class MyGame:
        def __init__(self, config: dict, dry_run: bool = False) -> None: ...
        def run(self) -> GameTraceBase: ...

Registration may additionally declare two things the runner honors:

``resolve_config``
    ``(config: dict) -> dict``, called once per batch at *expand* time, before
    fan-out. This is where a game derives config from config — sampling a task,
    or running a solver to synthesize a scenario. Its output is merged into the
    run config and therefore lands in the trace, which is what makes
    stochastically generated scenarios reproducible.

``storage``
    Optional default ``storage:`` block for the game. An experiment YAML may
    override it; shipped games should prefer an explicit local-first default.

``dry_run_checks_keys``
    Whether ``--dry-run`` should assert that every LLM agent has a usable API
    key. True for games whose dry run still calls models with a shortened
    config; False for games that substitute non-LLM stand-ins (negotiation
    swaps in heuristic agents), where demanding keys would be wrong.

``rating_adapter``
    Optional game-owned adapter that turns completed traces into generic rating
    events. The engine invokes it only from a replay/materialization workflow;
    a running game never writes leaderboard state directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, NamedTuple

log = logging.getLogger("a2a_engine.registry")

#: Entry-point group a game package declares to be auto-discovered:
#:
#:     [project.entry-points."a2a_engine.games"]
#:     word_guess = "word_guess"
#:
#: Importing the named module is what triggers its ``register_game`` call. This
#: is why ``a2a-run`` works on an experiment for a game the caller never
#: imported — without it, every run needs a per-game ``run.py`` wrapper.
ENTRY_POINT_GROUP = "a2a_engine.games"


class GameSpec(NamedTuple):
    """What the runner needs to know about a registered game."""

    cls: Callable[..., Any]
    resolve_config: Callable[[dict], dict] | None = None
    storage: dict[str, Any] | None = None
    package: str | None = None
    dry_run_checks_keys: bool = True
    rating_adapter: Any | None = None


_REGISTRY: dict[str, GameSpec] = {}
# Every registration ever seen, kept even when ``_REGISTRY`` is emptied.
# Registration happens as a module-import side effect and Python runs a module
# body only once, so a registry that is cleared after import can never be
# rebuilt by importing again. This ledger is what makes discovery recoverable;
# only entry-point games are ever replayed from it, so games registered
# directly by a test are not resurrected behind that test's back.
_registered_ever: dict[str, GameSpec] = {}


def register_game(
    name: str,
    cls: Callable[..., Any],
    *,
    resolve_config: Callable[[dict], dict] | None = None,
    storage: dict[str, Any] | None = None,
    package: str | None = None,
    dry_run_checks_keys: bool = True,
    rating_adapter: Any | None = None,
) -> None:
    """Register a game class under a name. Replaces any existing entry."""
    spec = GameSpec(
        cls=cls,
        resolve_config=resolve_config,
        storage=storage,
        package=package,
        dry_run_checks_keys=dry_run_checks_keys,
        rating_adapter=rating_adapter,
    )
    _REGISTRY[name] = spec
    _registered_ever[name] = spec


def get_game(name: str) -> Callable[..., Any]:
    """Return the game class. Still returns the bare class, for compatibility."""
    return get_game_spec(name).cls


def get_game_spec(name: str) -> GameSpec:
    if name not in _REGISTRY:
        discover_games()
    if name not in _REGISTRY:
        raise KeyError(
            f"Game {name!r} not registered. Known: {sorted(_REGISTRY)}. "
            f"Install the game package (it should declare a {ENTRY_POINT_GROUP!r} "
            "entry point), or import it before running."
        )
    return _REGISTRY[name]


def list_games() -> list[str]:
    return sorted(_REGISTRY)


_discovered = False


_entry_point_cache: list | None = None


def _entry_points() -> list:
    # Scanning installed distributions is not free, and every registry miss
    # reaches here through ``get_game_spec``.
    global _entry_point_cache
    if _entry_point_cache is None:
        try:
            _entry_point_cache = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception as exc:  # pragma: no cover - importlib.metadata edge cases
            log.debug("Entry-point discovery unavailable: %s", exc)
            _entry_point_cache = []
    return _entry_point_cache


def _restore_known_games(points: list) -> None:
    """Put back any entry-point game the ledger has but the registry lost."""
    for point in points:
        spec = _registered_ever.get(point.name)
        if spec is not None:
            _REGISTRY.setdefault(point.name, spec)


def installed_games() -> list[str]:
    """Registered games that an installed package actually advertises.

    ``list_games`` reports whatever is in the registry, which includes games a
    caller registered directly at runtime. Only entry-point games correspond to
    something installed, so anything that must line up with a distribution -
    a release, a packaged runtime image - should ask this instead.
    """
    discover_games()
    return sorted(point.name for point in _entry_points() if point.name in _REGISTRY)


def discover_games(force: bool = False) -> list[str]:
    """Import every installed package advertising an ``a2a_engine.games`` entry point.

    Import failures are logged and skipped rather than raised: one game with a
    missing optional dependency must not stop the others from running.

    Discovery is idempotent: importing an already-imported game package re-runs
    no registration, so anything the registry has lost since is restored from
    the registration ledger rather than silently staying absent.
    """
    global _discovered
    points = _entry_points()
    if _discovered and not force:
        _restore_known_games(points)
        return list_games()
    _discovered = True

    for point in points:
        try:
            point.load() if point.attr else import_module(point.value)
        except Exception as exc:
            log.warning("Could not load game entry point %r: %s: %s",
                        point.name, type(exc).__name__, exc)
    _restore_known_games(points)
    return list_games()
