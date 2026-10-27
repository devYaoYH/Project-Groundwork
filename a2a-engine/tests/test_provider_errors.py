"""Provider diagnostics reach owner logs without returning raw request echoes."""
import base64
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from a2a_engine.llm.errors import provider_error_detail


def http_error(body, code=401):
    payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return HTTPError("https://fixture.invalid", code, "Unauthorized", {}, BytesIO(payload))


@pytest.mark.parametrize("body", [
    {"error": {"code": 401, "message": "API key expired."}},
    {"error": {"type": "authentication_error", "message": "API key expired."}},
    {"message": "API key expired."},
    {"error": "API key expired."},
])
def test_extracts_provider_message(body):
    assert provider_error_detail(http_error(body)) == "API key expired."


def test_drops_metadata_and_redacts_credentials_images_and_private_urls():
    key = "custom-provider-secret"
    image = base64.b64encode(b"private-image-bytes" * 30).decode()
    body = {"error": {"message": f"Bad key {key} sk-or-v1-abc Bearer abc123\n"
                     f"data:image/png;base64,{image} {image} https://private.invalid/join?ticket=private",
                      "metadata": {"request": "private prompt", "raw_error": image}}}
    detail = provider_error_detail(http_error(body), api_key=key)
    for private in (key, "sk-or-v1-abc", "abc123", image, "private.invalid", "ticket=private", "private prompt"):
        assert private not in detail
    assert "\n" not in detail
    assert "Bad key" in detail


@pytest.mark.parametrize("body", ["<html>private diagnostic</html>", "invalid", "[]", "null",
    {"error": {"message": {"private": "data"}}}, {"error": {"metadata": "private"}},
    {"error": {"message": "x" * (17 * 1024)}}])
def test_unsupported_or_oversized_bodies_are_omitted(body):
    assert provider_error_detail(http_error(body)) is None


def test_diagnostics_are_bounded_single_line_and_cannot_mask_errors():
    detail = provider_error_detail(http_error({"error": {"message": "API\nkey\r\nexpired\x00. " * 200}}))
    assert detail.startswith("API key expired .")
    assert len(detail) <= 1000 and detail.endswith("[truncated]")
    assert "\x00" not in detail and "\n" not in detail

    class BrokenBody(BytesIO):
        def read(self, *_):
            raise OSError("cannot read")
    assert provider_error_detail(HTTPError("", 401, "Unauthorized", {}, BrokenBody())) is None
    assert provider_error_detail(RuntimeError("private body")) is None
