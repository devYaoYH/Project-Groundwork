"""Pluggable trace storage. See ``base.EpisodeStore``."""

from a2a_engine.storage.base import (
    StoreCheck,
    ControlPlaneReader,
    EpisodeStore,
    check_store,
    make_control_plane_reader,
    iter_episodes,
    list_stores,
    make_store,
    register_store,
    _import_backends,
)
from a2a_engine.storage.results import RESULT_PREDICATE, counts_as_result

_import_backends()

__all__ = [
    "StoreCheck",
    "ControlPlaneReader",
    "EpisodeStore",
    "check_store",
    "make_control_plane_reader",
    "iter_episodes",
    "list_stores",
    "make_store",
    "register_store",
    "RESULT_PREDICATE",
    "counts_as_result",
]
