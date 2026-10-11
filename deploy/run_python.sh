#!/usr/bin/env bash
# The stable Cloud SDK image bundles Python without a python3 executable on PATH.
# Use the interpreter gcloud actually selected instead of assuming its location.
set -euo pipefail
deployment_python="$(gcloud info --format='value(basic.python_location)')"
if [[ ! -x "$deployment_python" ]]; then
  echo "Cloud SDK did not report an executable Python interpreter" >&2
  exit 1
fi
# SDK-bundled Python can retain a build-time OpenSSL certificate path absent
# from the image. Prefer a system store, or use the SDK's maintained certifi
# bundle when the minimal image has no system CA package. Verification stays on.
if [[ -z "${SSL_CERT_FILE:-}" ]]; then
  if [[ -r /etc/ssl/certs/ca-certificates.crt ]]; then
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  else
    deployment_sdk_root="$(gcloud info --format='value(installation.sdk_root)')"
    deployment_ca_bundle="$deployment_sdk_root/lib/third_party/certifi/cacert.pem"
    if [[ ! -r "$deployment_ca_bundle" ]]; then
      echo "Cloud SDK image does not expose a trusted CA bundle" >&2
      exit 1
    fi
    export SSL_CERT_FILE="$deployment_ca_bundle"
  fi
fi
exec "$deployment_python" "$@"
