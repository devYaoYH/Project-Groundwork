"""Open frozen compiler/evidence files without current scientific backfills."""
from pathlib import Path

from a2a_engine.storage.schema import apply_schema
from local_stack.control_plane import ControlPlane


def open_frozen_control(path, *, workspace, results_dir):
    control = ControlPlane.__new__(ControlPlane)
    control.path = Path(path)
    control.trace_database = control.path
    control.workspace = Path(workspace)
    control.results_dir = Path(results_dir)
    # Add compatible storage columns/indexes only. Normal ControlPlane startup
    # can normalize old authored designs and recover abandoned local launches;
    # neither belongs to a frozen hosted experiment or its admitted execution.
    with control._session() as database:
        apply_schema(database)
    return control
