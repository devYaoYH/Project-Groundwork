"""Cloud-free public-contract probe for a separately hosted scripted endpoint."""

import argparse
import asyncio
import json

from a2a_engine.remote.contract import TurnCompletion
from a2a_engine.remote.dispatch import TurnDispatcher, loopback_url, post_signed
from a2a_engine.remote.server import RuntimeManager
from .mcp_client import connect


class ConformanceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ConformanceError(message)


class ProbeDispatcher(TurnDispatcher):
    def __init__(self, io):
        super().__init__(io)
        self.duplicates = 0
        self.endpoints = set()
        self.rejections = 0

    async def _deliver(self, seat, invocation, deadline, recorder=None):
        try:
            if recorder:
                for endpoint, names in (("env", {"get_observation", "schedule", "reschedule"}),
                                        ("comm", {"list_peers", "send", "read_inbox"})):
                    async with connect(invocation.mcp[endpoint], invocation.capability) as session:
                        tools = await session.list_tools()
                        require({tool.name for tool in tools.tools} == names, "fixed tool discovery differs")
                        name = "get_observation" if endpoint == "env" else "read_inbox"
                        result = await session.call_tool(name, {}, meta={"a2a/call_id": "probe:" + name})
                        expected = invocation.observation if endpoint == "env" else invocation.inbox
                        require(result.structuredContent["data"] == expected, "read snapshot differs from push")
                        if invocation.phase == "DECISION" and endpoint == "env":
                            invalid = await session.call_tool("schedule", {"slot": "bad"},
                                                              meta={"a2a/call_id": "probe:invalid"})
                            require(invalid.isError and invalid.structuredContent["code"] == "invalid_arguments",
                                    "malformed MCP input was not rejected")
                            self.rejections += 1
                    self.endpoints.add(endpoint)
            bodies = await asyncio.gather(*[post_signed(seat.callback_url + "/turns", invocation, seat.secret)
                                             for _ in range(2)])
            completions = [TurnCompletion.model_validate_json(body) for body in bodies]
            require(completions[0] == completions[1] and completions[0].turn_id == invocation.turn_id,
                    "duplicate pushes did not share a completion")
            self.duplicates += 1
            if recorder:
                recorder.attempts = 1
                recorder.complete(completions[0])
            return completions[0]
        except BaseException:
            if recorder:
                recorder.close("unreachable")
            raise


def run_conformance(callback_url, provisioning_dir, *, join_timeout_s=30):
    """Publish private join provisioning; the supplied endpoint initiates admission.

    Calendar is needed only by this optional episode driver, not by the harness.
    The endpoint must implement the documented deterministic scripted policy.
    """
    from calendar_game.game import CalendarGame
    from calendar_game.remote import CALENDAR_TOOLS

    callback_url = loopback_url(callback_url)
    config = {"environment_id": "calendar", "seed": 42, "num_agents": 2, "num_slots": 3,
              "num_meetings": 1, "density": 0, "decision_retries": 0, "enable_fallback": False,
              "enable_reflection": False, "max_turns_per_round": 2, "join_timeout_s": join_timeout_s,
              "agents": [{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]}
    scenario = {"seed": 42, "calendars": [[None] * 3, [None] * 3],
                "meetings": [{"id": 0, "participants": [0, 1], "speaker_order": [0, 1], "duration": 1, "cost": 1}]}
    with RuntimeManager(CALENDAR_TOOLS, provisioning_dir=provisioning_dir) as manager:
        runtime = manager.provision(config)
        seat = runtime.episode.wait_ready(0)
        require(seat.callback_url == callback_url, "admitted callback differs from supplied endpoint")
        probe = ProbeDispatcher(manager.io)

        class ConformanceGame(CalendarGame):
            def _build_agents(self, scenario):
                agents = super()._build_agents(scenario)
                self._remote_clients[0].dispatcher = probe
                return agents

        trace = ConformanceGame(config, runtime_context=runtime).run_with_scenario(scenario)
        require(not trace.stopped and trace.metrics["meetings_scheduled"] == 1, "tiny episode did not complete")
        require(probe.endpoints == {"env", "comm"} and probe.duplicates >= 5, "transport coverage incomplete")
        messages = [event for event in trace.events if event.type == "dm_sent"]
        require(len(messages) == 2 and {event.data["agent_id"] for event in messages} == {0, 1},
                "duplicate push repeated or lost a scripted send")
        invalid = [event for event in trace.events if event.type == "invalid_tool_call"]
        require(len(invalid) == probe.rejections == 1 and invalid[0].data["tool_call"]["code"] == "invalid_arguments",
                "rejection conformance differs")
        require(all(calendar[0]["meeting_id"] == 0 for calendar in trace.final_state["calendars"]),
                "calendar commit differs")
        return {"protocol_version": "a2a-turns/1", "passed": True, "duplicate_pushes": probe.duplicates,
                "endpoints": sorted(probe.endpoints), "schema_rejections": probe.rejections}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callback-url", required=True, help="Literal loopback endpoint base, without /turns")
    parser.add_argument("--provisioning-dir", required=True, help="Dedicated owner-only directory watched by the endpoint owner")
    parser.add_argument("--join-timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        result = run_conformance(args.callback_url, args.provisioning_dir, join_timeout_s=args.join_timeout)
    except Exception:
        print(json.dumps({"passed": False, "error": "conformance failed; verify endpoint and private provisioning"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
