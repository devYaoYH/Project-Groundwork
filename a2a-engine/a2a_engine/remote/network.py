"""Explicit local-deployment origins; private addresses are not implicit trust."""

from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contract import MAX_BODY_BYTES


class NetworkPolicy:
    def __init__(self, *, advertised_origin=None, allowed_origins=()):
        self.advertised_origin = advertised_origin
        self.allowed_origins = frozenset(allowed_origins)
        if advertised_origin is not None:
            for origin in (*self.allowed_origins, advertised_origin):
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or not parsed.hostname or not parsed.port
                        or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
                    raise ValueError("provisioned origin must be exact local HTTP origin")

    def validate(self, url):
        from .dispatch import loopback_url
        if self.advertised_origin is None:
            return loopback_url(url)
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" not in self.allowed_origins
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or ".." in parsed.path or "%" in parsed.path):
            raise ValueError("endpoint is not provisioned")
        return url.rstrip("/")


@asynccontextmanager
async def connect_mcp(url, credential, *, timeout=30, network=None):
    (network or NetworkPolicy()).validate(url)
    async def bound_response(response):
        if response.headers.get("content-length") and int(response.headers["content-length"]) > MAX_BODY_BYTES:
            raise ValueError("response exceeds body limit")
        if response.headers.get("content-type", "").startswith("application/json"):
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("response exceeds body limit")
            response._content = bytes(body)

    async with httpx.AsyncClient(headers={"authorization": f"Bearer {credential}"},
                                 timeout=timeout, follow_redirects=False, trust_env=False,
                                 event_hooks={"response": [bound_response]}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


async def mcp_call(url, credential, name, arguments, *, timeout=30, network=None, call_id=None):
    import asyncio
    async with asyncio.timeout(timeout):
        async with connect_mcp(url, credential, timeout=timeout, network=network) as session:
            result = await session.call_tool(name, arguments,
                                             meta={"a2a/call_id": call_id} if call_id else None)
    if result.isError:
        raise ValueError("MCP request rejected")
    return result.structuredContent
