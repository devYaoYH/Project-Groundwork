"""Verify workflow discovery from installed, non-editable distributions."""

import importlib
import inspect
import json
from pathlib import Path
from unittest.mock import patch

from a2a_engine.registry import get_environment_spec, installed_environments
from a2a_engine.workflow import environment_workflow


def fail(*args, **kwargs):
    raise AssertionError("installed workflow export attempted episode execution")


def main():
    expected = {"calendar", "word_guess", "buyer_seller", "negotiation", "tangram_reference"}
    assert set(installed_environments()) == expected
    for name in sorted(expected):
        cls = get_environment_spec(name).cls
        assert "site-packages" in str(Path(inspect.getfile(cls)))
        with patch.object(cls, "__init__", fail), patch.object(cls, "runtime_capability", fail), \
             patch("a2a_engine.llm.factory.make_llm_client", fail), \
             patch("a2a_engine.remote.server.RuntimeManager.__init__", fail):
            first = environment_workflow(name)
            assert first == environment_workflow(name)
            assert first["status"] == "available", first
            assert first["workflow"]["diagnostics"] == []
            assert len(json.dumps(first)) < 100000
    for module in ("a2a_agent.service", "a2a_agent.strategies", "word_guess.runtime_strategy",
                   "buyer_seller.runtime_strategy", "negotiation_game.runtime_strategy", "tangram_reference.runtime_strategy",
                   "a2a_engine.remote.compose", "calendar_game.workflow_steps"):
        assert "site-packages" in str(Path(inspect.getfile(importlib.import_module(module))))
    print("All five installed wheels export deterministic available workflows without execution")


if __name__ == "__main__":
    main()
