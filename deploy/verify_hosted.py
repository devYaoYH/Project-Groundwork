"""Verify the hosted public/authentication boundary without collecting credentials.

This verifies deployment health and denial of unauthenticated private reads and
mutations. Real Google login, owner isolation, execution and exports are separate
authenticated end-to-end checks, not claims made by this script.
"""
import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def response(base, path, method="GET", body=None):
    request = Request(base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as result:
            return result.status, {key.lower(): value for key, value in result.headers.items()}, json.load(result)
    except HTTPError as error:
        return error.code, {key.lower(): value for key, value in error.headers.items()}, json.load(error)


def verify(url, source_sha, attempts=12):
    url = url.rstrip("/")
    if urlsplit(url).scheme != "https":
        raise ValueError("Hosted verification requires an HTTPS URL")
    last = None
    for attempt in range(attempts):
        try:
            status, headers, health = response(url, "/api/health")
            assert status == 200 and health.get("ok") is True, "health endpoint is unhealthy"
            assert health.get("source_sha") == source_sha, "health source_sha does not match deployed source"
            break
        except (AssertionError, URLError) as error:
            last = error
            if attempt == attempts - 1:
                raise RuntimeError(f"Hosted health verification failed: {last}") from error
            time.sleep(5)
    status, headers, config = response(url, "/auth/config")
    assert status == 200, "Google login configuration is unavailable"
    assert config.get("google_client_id", "").endswith(".apps.googleusercontent.com"), "missing Google web client"
    assert config.get("dev_auth") is False, "development login enabled on hosted platform"
    assert "Secure" in headers.get("set-cookie", ""), "hosted login cookie lacks Secure flag"
    paths = [("GET", "/auth/me", None), ("GET", "/api/releases", None),
             ("GET", "/api/experiments", None), ("GET", "/api/experiments/unauthorized", None),
             ("GET", "/api/launches/unauthorized", None), ("GET", "/api/episodes/unauthorized", None),
             ("GET", "/api/exports/unauthorized/download", None),
             ("POST", "/api/experiments", {}),
             ("POST", "/api/experiments/unauthorized/launch", {}),
             ("POST", "/api/experiments/unauthorized/export", {}),
             ("POST", "/api/launches/unauthorized/cancel", {}),
             ("POST", "/internal/dispatch", {"launch_id": "unauthorized"})]
    for method, path, body in paths:
        status, headers, data = response(url, path, method, body)
        assert status == 401, f"{method} {path}: expected 401, received {status}"
        assert set(data) == {"error"}, f"{method} {path}: error response exposes data"
        assert headers.get("cache-control") == "no-store", f"{path}: response may be cached"
    status, _, _ = response(url, "/auth/dev", "POST", {"user": "alice"})
    assert status == 404, "hosted development login must be disabled"
    result = {"service_url": url, "source_sha": source_sha, "health": "passed",
              "google_login_configuration": "passed", "unauthenticated_access_checks": len(paths),
              "development_login": "disabled", "authenticated_end_to_end": "requires real Google login"}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--project")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    url = args.url
    if args.project:
        from common import command
        url = command(["run", "services", "describe", "groundwork-control", "--project", args.project,
                       "--region", args.region, "--format=value(status.url)"], capture=True)
    verify(url, args.source_sha)
