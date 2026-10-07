"""Calendar's deterministic schedule-only baseline expressed as MCP calls."""


def free_slots(render):
    slots = []
    for line in render.splitlines():
        if "[FREE]" in line:
            try:
                slots.append(int(line.split("Slot")[1].split(":")[0].strip()))
            except (IndexError, ValueError):
                pass
    return slots


class ScriptedPolicy:
    def __init__(self):
        self.config = {}
        self.turned = False

    def calls(self, invocation):
        observation = invocation.observation
        if invocation.kind == "register":
            self.config = observation["game_config"]
            return []
        if invocation.kind == "round_start":
            self.turned = False
            return []
        if invocation.kind != "turn":
            return []
        meeting = observation["meeting"]
        free = free_slots(observation["calendar_render"])
        if invocation.phase == "DECISION":
            return [("env", "schedule", {"meeting_id": meeting["id"], "slot": free[0]})] if free else []
        if invocation.phase != "CHEAP_TALK":
            raise ValueError("Phase 2 scripted runtime supports CHEAP_TALK and DECISION only")
        if self.turned:
            return []
        self.turned = True
        content = f"I'm free at slots: {free}"
        protocol = self.config.get("communication_protocol", "dm")
        if "participant_groupchat" in protocol:
            return [("comm", "send", {"channel": "participant_groupchat", "content": content})]
        if "all_groupchat" in protocol or protocol == "groupchat":
            return [("comm", "send", {"channel": "all_groupchat", "content": content})]
        return [("comm", "send", {"channel": "dm", "to": other, "content": content})
                for other in meeting.get("participants", []) if other != invocation.seat]
