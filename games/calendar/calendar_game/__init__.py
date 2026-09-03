"""Calendar scheduling benchmark environment."""

import logging

log = logging.getLogger(__name__)

try:
    from calendar_game.game import CalendarGame, CalendarGameConfig
    __all__ = ["CalendarGame", "CalendarGameConfig"]
except ImportError as exc:
    # The analysis modules stay importable without the environment's heavier optional
    # dependencies, but a swallowed failure here silently removes Calendar from
    # the environment registry — and the control plane then reports only that no
    # release matches the experiment. Say why instead.
    log.warning("Calendar environment unavailable, so it will not register: %s: %s",
                type(exc).__name__, exc)
    __all__ = []
