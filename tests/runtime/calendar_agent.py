"""Controlled /2 Calendar policy for the disposable container scheduling gate."""

import asyncio

from a2a_agent import service
from a2a_agent.mcp_client import execute


class CalendarPolicy(service.ProbePolicy):
    async def calls(self, invocation):
        if invocation.kind != "turn":
            return await super().calls(invocation)
        if invocation.phase == "DECISION":
            return [("env", "schedule", {"meeting_id": invocation.observation["meeting"]["id"], "slot": 999})]
        if invocation.phase == "DECISION_RETRY":
            calls = [("env", "get_observation", {}), ("env", "schedule", {"meeting_id": 0, "slot": 0}),
                     ("comm", "read_inbox", {}), ("comm", "list_peers", {})]
            if invocation.seat == 0:
                outcomes = await execute(invocation, calls, network=self.network)
                assert [outcome["status"] for outcome in outcomes] == ["ok", "accepted", "ok", "ok"]
                while not (self.directory / "release").exists():
                    await asyncio.sleep(0.02)
                return []
            return calls
        return await super().calls(invocation)

    async def reflect(self, invocation):
        await asyncio.sleep(0.15 if invocation.observation["target_agent_id"] == 1 else 0.02)
        return {"estimates": [{"slot": slot, "estimate_state": 0, "probability_free": 1,
            "probability_busy": 0, "confidence": 1, "logprobs": {}} for slot in range(invocation.observation["num_slots"])]}


if __name__ == "__main__":
    service.ProbePolicy = CalendarPolicy
    service.main()
