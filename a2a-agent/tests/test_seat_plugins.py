from types import SimpleNamespace

import pytest
from a2a_agent import seat_plugins as registry


def entry(name, plugin):
    return SimpleNamespace(name=name, load=lambda: plugin)


@pytest.fixture
def discover(monkeypatch):
    registry.seat_plugins.cache_clear()
    monkeypatch.setattr("a2a_engine.registry.installed_environments", lambda: ["extension"])

    def configure(entries):
        monkeypatch.setattr(registry, "entry_points", lambda **kwargs: entries)
        return registry.seat_plugins()

    yield configure
    registry.seat_plugins.cache_clear()


def test_new_package_dispatches_without_an_agent_harness_edit(discover):
    marker = object()
    plugin = registry.SeatPlugin("extension", lambda context: (marker, context.config))
    assert discover([entry("extension", plugin)]) == {"extension": plugin}
    assert registry.get_seat_plugin("extension").create_policy({"type": "scripted"}) == (
        marker,
        {"type": "scripted"},
    )
    with pytest.raises(ValueError, match="unsupported environment"):
        registry.get_seat_plugin("unknown")


@pytest.mark.parametrize(
    "entries",
    [
        [entry("extension", object())],
        [entry("extension", registry.SeatPlugin("other", lambda context: None))],
        [entry("other", registry.SeatPlugin("other", lambda context: None))],
        [
            entry(
                "extension",
                registry.SeatPlugin(
                    "extension", lambda context: None, runtime_adapter="unsupported"
                ),
            )
        ],
        [entry("extension", registry.SeatPlugin("extension", lambda context: None))] * 2,
    ],
)
def test_invalid_or_duplicate_entry_points_fail_closed(discover, entries):
    with pytest.raises(ValueError):
        discover(entries)


def test_broken_entry_point_is_not_silently_skipped(discover):
    def broken():
        raise ImportError("broken installed package")

    with pytest.raises(ImportError):
        discover([SimpleNamespace(name="extension", load=broken)])


@pytest.mark.parametrize(
    "config", [{"api_key": "private"}, {"type": "llm"}, {"type": "heuristic"}, {"type": "unknown"}]
)
def test_validation_precedes_factory_execution(config):
    def forbidden(context):
        pytest.fail("invalid configuration reached the plugin")

    with pytest.raises(ValueError):
        registry.SeatPlugin("extension", forbidden).create_policy(config)
