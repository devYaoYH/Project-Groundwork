#!/usr/bin/env bash
# The stable Cloud SDK image bundles Python without a python3 executable on PATH.
# Use the interpreter gcloud actually selected instead of assuming its location.
set -euo pipefail
deployment_python="$(gcloud info --format='value(basic.python_location)')"
if [[ ! -x "$deployment_python" ]]; then
  echo "Cloud SDK did not report an executable Python interpreter" >&2
  exit 1
fi
exec "$deployment_python" "$@"
