"""Verify workflow and agent discovery from independently inventoried installed wheels."""

import argparse
import importlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch

from a2a_agent.seat_plugins import seat_plugins
from a2a_engine.registry import get_environment_spec, installed_environments
from a2a_engine.workflow import environment_workflow


def fail(*args, **kwargs):
    raise AssertionError("installed workflow export attempted episode execution")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.expected_manifest.read_text())
    expected = {name for package in manifest["packages"] for name in package["environments"]}
    expected_seats = {name for package in manifest["packages"] for name in package["seats"]}
    assert set(installed_environments()) == expected
    assert set(seat_plugins()) == expected_seats
    for name in sorted(expected):
        cls = get_environment_spec(name).cls
        assert "site-packages" in str(Path(inspect.getfile(cls)))
        with (
            patch.object(cls, "__init__", fail),
            patch.object(cls, "runtime_capability", fail),
            patch("a2a_engine.llm.factory.make_llm_client", fail),
            patch("a2a_engine.remote.server.RuntimeManager.__init__", fail),
        ):
            first = environment_workflow(name)
            assert first == environment_workflow(name)
            assert first["status"] == "available", first
            assert first["workflow"]["diagnostics"] == []
            assert len(json.dumps(first)) < 100000
    modules = {"a2a_agent.service", "a2a_agent.strategies", "a2a_engine.remote.compose"}
    for package in manifest["packages"]:
        modules.update(target.split(":")[0] for target in package["seats"].values())
    for module in modules:
        assert "site-packages" in str(Path(inspect.getfile(importlib.import_module(module))))
    print(
        f"All {len(expected)} installed environments and {len(expected_seats)} seat plugins verified"
    )


if __name__ == "__main__":
    main()
