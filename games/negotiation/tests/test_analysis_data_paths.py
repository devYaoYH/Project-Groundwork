"""Seam tests: the analysis layer's committed data files must resolve.

A missing denylist is indistinguishable from an empty one at the call site —
``load_denylist`` returns an empty set and ``apply_denylist`` then filters
nothing. That failure mode is silent and changes analysis results, so the
paths themselves are pinned here.
"""

from negotiation_analysis import data_loader


def test_denylist_resolves_to_the_committed_file():
    assert data_loader.DENYLIST_PATH.is_file(), (
        f"denylist not found at {data_loader.DENYLIST_PATH}; "
        "a missing denylist silently excludes nothing"
    )


def test_committed_denylist_entries_are_actually_loaded():
    prefixes = data_loader.load_denylist()
    assert prefixes, "the committed denylist has entries but none were loaded"


def test_denylist_actually_removes_the_games_it_names():
    prefix = sorted(data_loader.load_denylist())[0]
    games = [{"episode_uid": f"{prefix}-run-1"}, {"episode_uid": "keep-this-one"}]

    kept = data_loader.apply_denylist(games)

    assert [environment["episode_uid"] for environment in kept] == ["keep-this-one"]


def test_analysis_data_dir_sits_beside_the_packages_that_ship_it():
    """REPO_ROOT points at whatever repository vendored the environment, which is not
    where the environment's own data lives."""
    assert data_loader.DATA_DIR.name == "data"
    assert data_loader.DATA_DIR.parent.name == "negotiation"
