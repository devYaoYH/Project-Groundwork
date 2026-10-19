"""Make the colocated pytest plugin importable for ordinary tooling tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
