"""Wrapper: imports tangram_reference (registers the environment), then defers to the expt-runner CLI.

Usage:

    uv run python run.py experiments/example.yaml --smoke-test

`a2a-run experiments/example.yaml` works too once the package is installed —
the entry point in pyproject.toml handles registration. This wrapper also loads
.env, which the bare CLI does not.
"""

import sys

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

import tangram_reference  # noqa: F401,E402  (registers "tangram_reference" via import side-effect)
from expt_runner.run_experiment import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
