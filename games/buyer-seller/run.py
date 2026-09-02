"""Wrapper: imports buyer_seller (registers the game), then defers to expt-runner CLI.

Usage:

    uv run python games/word-guess/run.py games/word-guess/experiments/example.yaml --dry-run
"""

import base64
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _wire_langfuse_otlp() -> None:
    """If LANGFUSE_* keys are set, translate them into the OTLP env vars the SDK reads."""
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    base = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    if not (pk and sk):
        return
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return  # user already configured an exporter; don't override
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{base}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = (
        f"Authorization=Basic {auth},x-langfuse-ingestion-version=4"
    )
    os.environ.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")


_wire_langfuse_otlp()

import buyer_seller  # noqa: F401,E402  (registers "buyer_seller" via import side-effect)
from expt_runner.run_experiment import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
