"""Separate SDK clients for env and comm with stable per-invocation call IDs."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import asyncio
import time
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from a2a_engine.remote.dispatch import loopback_url, deliver_with_retry


@asynccontextmanager
async def connect(url, capability, *, timeout=30):
    loopback_url(url)
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {capability}"},
                                 timeout=timeout, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


async def execute(invocation, calls):
    outcomes = []
    remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("invocation expired")
    deadline = time.monotonic() + remaining
    async with asyncio.timeout(remaining):
        for index, (endpoint, name, arguments) in enumerate(calls):
            async def call(timeout):
                async with connect(invocation.mcp[endpoint], invocation.capability, timeout=timeout) as session:
                    result = await session.call_tool(name, arguments,
                                                     meta={"a2a/call_id": f"{invocation.turn_id}:{index}"})
                    return result.structuredContent
            outcomes.append(await deliver_with_retry(call, deadline=deadline))
    return outcomes
