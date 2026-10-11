"""Separate SDK clients for env and comm with stable per-invocation call IDs."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import asyncio
import time
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from a2a_engine.remote.dispatch import loopback_url, deliver_with_retry
from a2a_engine.remote.network import connect_mcp, mcp_call


@asynccontextmanager
async def connect(url, capability, *, timeout=30):
    loopback_url(url)
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {capability}"},
                                 timeout=timeout, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


async def execute(invocation, calls, *, network=None):
    outcomes = []
    remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("invocation expired")
    deadline = time.monotonic() + remaining
    async with asyncio.timeout(remaining):
        for index, (endpoint, name, arguments) in enumerate(calls):
            async def call(timeout):
                connection = (connect(invocation.mcp[endpoint], invocation.capability, timeout=timeout)
                              if invocation.protocol_version == "a2a-turns/1" else
                              connect_mcp(invocation.mcp[endpoint], invocation.capability, timeout=timeout, network=network))
                async with connection as session:
                    result = await session.call_tool(name, arguments,
                                                     meta={"a2a/call_id": f"{invocation.turn_id}:{index}"})
                    return result.structuredContent
            outcomes.append(await deliver_with_retry(call, deadline=deadline))
    return outcomes


async def report_completion(invocation, finished, *, network=None, grace_s=5):
    from a2a_engine.remote.contract import CompletionReceipt
    deadline = time.monotonic() + max(0, (invocation.deadline - datetime.now(timezone.utc)).total_seconds()) + grace_s

    async def report(remaining):
        result = await mcp_call(invocation.mcp["lifecycle"], invocation.lifecycle_credential,
                                "complete_turn", finished.model_dump(mode="json"),
                                timeout=min(2, remaining), network=network)
        receipt = CompletionReceipt.model_validate(result)
        if any(getattr(receipt, key) != value for key, value in invocation.identity().model_dump().items()):
            raise ValueError("invalid receipt")
        return receipt

    return await deliver_with_retry(report, deadline=deadline)
