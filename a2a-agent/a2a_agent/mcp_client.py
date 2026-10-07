"""Separate SDK clients for env and comm with stable per-invocation call IDs."""

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from a2a_engine.remote.dispatch import loopback_url


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
    async with AsyncExitStack() as stack:
        sessions = {}
        for endpoint in dict.fromkeys(target for target, _, _ in calls):
            session = await stack.enter_async_context(connect(invocation.mcp[endpoint], invocation.capability,
                                                              timeout=remaining))
            await session.list_tools()
            sessions[endpoint] = session
        for index, (endpoint, name, arguments) in enumerate(calls):
            result = await sessions[endpoint].call_tool(name, arguments,
                                                       meta={"a2a/call_id": f"{invocation.turn_id}:{index}"})
            outcomes.append(result.structuredContent)
    return outcomes
