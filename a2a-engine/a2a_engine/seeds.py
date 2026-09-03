"""Per-episode seed derivation.

The N episodes of a cell used to share one resolved config *including* the
seed, so replicates differed only by sampling noise and an environment that
draws its scenario from the seed drew the same one every time.  Each episode
now gets its own seed.

The derivation must be a **pure function** of ``(root_seed, cell_id,
episode_idx)`` rather than a draw from a generator advanced as episodes are
enumerated.  ``--shard-index``/``--shard-count`` and ``--resume`` both change
how many episodes a given process enumerates, so a sequential draw would hand
the same episode different seeds depending on how the run was invoked --
silently breaking the reproducibility the seed exists to provide.
"""

from __future__ import annotations

import hashlib

#: ``derived`` re-generates the same episodes from the same root and draws a
#: fresh replication set when the root is rotated.  ``static`` gives every
#: episode one fixed seed, which is for testing and debugging only.
SEED_MODES = ("derived", "static")

# Kept inside a signed 32-bit range: several environments hand the seed
# straight to ``random.Random`` or to numpy, and numpy rejects anything wider.
SEED_SPACE = 2 ** 31


def derive_seed(
    root_seed: int, cell_id: str, episode_idx: int, *, mode: str = "derived"
) -> int:
    """Return this episode's seed, deterministically, from the experiment root."""
    if mode not in SEED_MODES:
        raise ValueError(f"unknown seed mode {mode!r}; expected one of {list(SEED_MODES)}")
    if mode == "static":
        return int(root_seed)
    blob = f"{int(root_seed)}:{cell_id}:{int(episode_idx)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big") % SEED_SPACE
