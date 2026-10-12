"""Strict credential-bearing join links; never include these in public handles."""

import base64
import json
import math
import re
from urllib.parse import urlsplit

PREFIX = "a2a-join:"
MAX_LINK_BYTES = 8192
FIELDS = frozenset({"join_url", "seat", "environment_id", "expires_at", "join_ticket"})


def _validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("invalid join link")
    if type(value["seat"]) is not int or value["seat"] < 0:
        raise ValueError("invalid join link")
    expiry = value["expires_at"]
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError("invalid join link")
    if not isinstance(value["environment_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value["environment_id"]):
        raise ValueError("invalid join link")
    ticket = value["join_ticket"]
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise ValueError("invalid join link")
    if base64.urlsafe_b64encode(base64.urlsafe_b64decode(ticket + "=")).decode().rstrip("=") != ticket:
        raise ValueError("invalid join link")
    url = value["join_url"]
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError("invalid join link")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r"/episodes/[A-Za-z0-9_-]+/join", parsed.path)):
        raise ValueError("invalid join link")
    parsed.port
    return value


def encode_join_link(descriptor):
    try:
        value = _validate({key: descriptor[key] for key in FIELDS})
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        link = PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")
        if len(link) > MAX_LINK_BYTES:
            raise ValueError()
        return link
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid join link") from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def decode_join_link(link):
    try:
        if not isinstance(link, str) or len(link) > MAX_LINK_BYTES or not link.startswith(PREFIX):
            raise ValueError()
        encoded = link[len(PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ValueError()
        payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(payload).decode().rstrip("=") != encoded:
            raise ValueError()
        return _validate(json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("invalid join link") from None
