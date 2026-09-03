"""Seam tests: the extended environment registry.

register_environment() grew three optional declarations (resolve_config, storage,
dry_run_checks_keys). The compatibility requirement is that a environment registered
the old way — positionally, with no keywords — still behaves exactly as before.
"""

import pytest

from a2a_engine.registry import _REGISTRY, get_environment, get_environment_spec, list_environments, register_environment


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved)


class Dummy:
    def __init__(self, config, dry_run=False):
        self.config = config

    def run(self):
        return None


def test_minimal_registration_still_works():
    """word-guess and any external environment registered pre-merge look like this."""
    register_environment("dummy", Dummy)
    assert get_environment("dummy") is Dummy
    spec = get_environment_spec("dummy")
    assert spec.resolve_config is None
    assert spec.storage is None


def test_dry_run_key_checking_defaults_to_on():
    """Calendar relies on --dry-run asserting that API keys are present."""
    register_environment("dummy", Dummy)
    assert get_environment_spec("dummy").dry_run_checks_keys is True


def test_a_game_can_opt_out_of_key_checking():
    """Negotiation substitutes heuristic agents, so keys would be wrong to demand."""
    register_environment("dummy", Dummy, dry_run_checks_keys=False)
    assert get_environment_spec("dummy").dry_run_checks_keys is False


def test_declarations_are_retrievable():
    def resolver(cfg):
        return cfg

    adapter = object()
    register_environment(
        "dummy", Dummy, resolve_config=resolver,
        storage={"backend": "s3"}, package="dummy-environment", rating_adapter=adapter,
    )
    spec = get_environment_spec("dummy")
    assert spec.resolve_config is resolver
    assert spec.storage == {"backend": "s3"}
    assert spec.package == "dummy-environment"
    assert spec.rating_adapter is adapter


def test_get_environment_returns_the_class_not_the_spec():
    """Downstream code calls GameCls(config=..., dry_run=...) directly."""
    register_environment("dummy", Dummy, storage={"backend": "local"})
    assert get_environment("dummy") is Dummy


def test_reregistration_replaces():
    class Other(Dummy):
        pass

    register_environment("dummy", Dummy)
    register_environment("dummy", Other)
    assert get_environment("dummy") is Other


def test_unknown_game_error_names_the_alternatives():
    register_environment("calendar", Dummy)
    with pytest.raises(KeyError, match="calendar"):
        get_environment_spec("nope")


def test_list_environments_is_sorted():
    register_environment("zeta", Dummy)
    register_environment("alpha", Dummy)
    assert list_environments() == ["alpha", "zeta"]


def test_discovery_repopulates_a_registry_that_was_emptied_after_discovery():
    """Re-importing cannot re-register: module bodies only ever run once.

    Anything that clears the registry after discovery (a test fixture, a
    reload) would otherwise leave every later lookup failing against a
    silently empty registry, because ``_discovered`` short-circuits and the
    environment modules are already in ``sys.modules``.
    """
    from a2a_engine import registry

    discovered = registry.discover_environments(force=True)
    assert discovered, "expected at least one installed environment entry point"

    registry._REGISTRY.clear()
    assert registry.list_environments() == []

    assert registry.discover_environments() == discovered
    assert registry.get_environment_spec(discovered[0]) is not None

    # A environment whose package some other import already pulled in before discovery
    # ran registers during that import, not during discovery. Remembering only
    # what a discovery call itself added would drop exactly those games, and
    # they are the ones that can never be recovered by importing again.
    registry._REGISTRY.clear()
    assert sorted(registry.discover_environments(force=True)) == discovered

    # A environment registered directly, outside any entry point, is a test's own
    # business and must not be resurrected behind its back.
    registry.register_environment("scratch", Dummy)
    registry._REGISTRY.clear()
    assert "scratch" not in registry.discover_environments()


def test_every_declared_game_entry_point_actually_registers():
    """A environment whose import fails silently just disappears from the registry.

    The control plane surfaces that only as "no release for environment X", so assert
    the declared entry points and the registered games agree.
    """
    from importlib.metadata import entry_points

    from a2a_engine import registry
    from a2a_engine.registry import ENTRY_POINT_GROUP

    declared = sorted(point.name for point in entry_points(group=ENTRY_POINT_GROUP))
    assert declared, "expected the workspace games to declare entry points"
    assert sorted(registry.discover_environments(force=True)) == declared


def test_installed_environments_excludes_runtime_only_registrations():
    """A directly registered environment has no package behind it, so anything that
    must correspond to a distribution must not pick it up."""
    from a2a_engine import registry

    entry_point_games = registry.installed_environments()
    assert entry_point_games

    registry.register_environment("runtime-only", Dummy)
    assert "runtime-only" in registry.list_environments()
    assert registry.installed_environments() == entry_point_games
