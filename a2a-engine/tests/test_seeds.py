"""Seed derivation: pure, shard-stable, and distinct per episode."""

from __future__ import annotations

import pytest

from a2a_engine.seeds import SEED_SPACE, derive_seed


def test_derive_seed_is_a_pure_function_of_its_three_inputs():
    assert derive_seed(7, "baseline", 3) == derive_seed(7, "baseline", 3)
    assert derive_seed(7, "baseline", 3) != derive_seed(8, "baseline", 3)
    assert derive_seed(7, "baseline", 3) != derive_seed(7, "treatment", 3)
    assert derive_seed(7, "baseline", 3) != derive_seed(7, "baseline", 4)


def test_every_episode_of_a_cell_gets_its_own_seed():
    seeds = [derive_seed(1, "b1", index) for index in range(32)]
    assert len(set(seeds)) == 32


def test_seeds_stay_inside_the_range_environments_can_consume():
    for index in range(64):
        seed = derive_seed(99, "cell", index)
        assert 0 <= seed < SEED_SPACE


def test_sharding_does_not_change_any_episodes_seed():
    """The reason this is a hash and not a generator.

    ``--shard-index``/``--shard-count`` and ``--resume`` change how many
    episodes a given process enumerates. A sequential draw would hand the same
    episode a different seed depending on how the run was invoked.
    """
    whole = {index: derive_seed(5, "b1", index) for index in range(9)}

    sharded: dict[int, int] = {}
    for shard in range(3):
        # Each shard enumerates only its own slice, in its own process.
        for index in range(shard, 9, 3):
            sharded[index] = derive_seed(5, "b1", index)

    assert sharded == whole


def test_resuming_reproduces_the_seed_of_the_episode_it_re_runs():
    before = [derive_seed(5, "b1", index) for index in range(4)]
    # A resumed process enumerates only what is missing; index 2 must still be
    # the same episode it was the first time round.
    assert derive_seed(5, "b1", 2) == before[2]


def test_static_mode_holds_one_seed_across_the_experiment():
    assert derive_seed(42, "b1", 0, mode="static") == 42
    assert derive_seed(42, "b2", 7, mode="static") == 42


def test_an_unknown_seed_mode_is_rejected_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown seed mode"):
        derive_seed(1, "b1", 0, mode="random")
