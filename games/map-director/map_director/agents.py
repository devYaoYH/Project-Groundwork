"""Seat-owned multimodal dialogue history; never reset between rounds."""

import json
from a2a_engine import LLMAgent
from a2a_engine.content import observation_content

GIVER_SYSTEM = """You are the giver in a repeated Map Director task. Your private image has a red target route; your partner's map has no route and may have missing, renamed, duplicate, or displaced landmarks. Coordinate frames differ: numbers on your map are not your partner's local numbers. Describe the physical route using shared landmarks, alignment, directions, and relative distances. Landmarks are reference points, not obstacles. Ask clarifying questions when necessary. You never see the follower's drawing. Preserve useful shared conventions between rounds, but follow the NEW red route each round. Output JSON {"text":"your instructions or answer"}."""
FOLLOWER_SYSTEM = """You are the follower in a repeated Map Director task. Your private map has no target route; follow the giver's directions. The purple line is your current drawing. Maps may differ in landmarks and coordinate units. Read YOUR OWN axis labels: x increases east, y increases north. Do not copy giver numeric coordinates without establishing a mapping. Ask questions about missing/duplicate/displaced landmarks. Use shared anchors and alignment to recover the physical route. No correctness feedback arrives before submission. Preserve conventions across rounds, but draw the NEW requested route. Output JSON {"text":"reply or question","route":[[x,y],...],"submit":false}. route is optional and replaces the entire drawing, using YOUR OWN local axis units. When confident use submit:true and include the complete route from START to FINISH. You may submit on your first turn. A short dialogue is sufficient when instructions are clear."""


class MapDirectorAgent(LLMAgent):
    supported_input_modalities = {"text", "image"}

    def __init__(self, client, *, role, system_prompt=None):
        super().__init__(
            client,
            system_prompt=system_prompt or (GIVER_SYSTEM if role == "giver" else FOLLOWER_SYSTEM),
            name=role,
        )
        self.role = role
        self.history = []
        self.last_messages = []

    def build_messages(self, observation, tools):
        self.history.append(
            {"role": "user", "content": observation_content(observation)}
        )
        self.last_messages = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
        ]
        return self.last_messages

    def parse_response(self, text, observation, tools):
        self.history.append({"role": "assistant", "content": text})
        raw = text.strip()
        if raw.startswith("```"):
            fenced = raw.split("\n", 1)
            raw = fenced[1].rsplit("```", 1)[0] if len(fenced) == 2 else raw
        try:
            action = json.loads(raw)
            if not isinstance(action, dict):
                raise ValueError("action must be object")
            action.setdefault("text", "")
            if not isinstance(action["text"], str):
                raise ValueError("text must be string")
            return action
        except (ValueError, TypeError):
            return {"text": text, "parse_failed": True}


class ScriptedMapAgent:
    supported_input_modalities = {"text", "image"}
    capabilities = {
        "input": ["text", "image"],
        "evidence": "deterministic-map-fixture-v1",
    }

    def __init__(self, role, *, task_version="v1"):
        self.task_version = task_version
        self.name = role
        self.observations = []

    async def act(self, observation, tools):
        self.observations.append(observation)
        if self.task_version == "v2":
            from .scripted_v2 import act
            return act(self.name, observation)
        if self.name == "giver":
            # Read the visible red first leg, rather than consulting a bank key.
            import base64
            from io import BytesIO
            from PIL import Image

            pixels = Image.open(
                BytesIO(base64.b64decode(observation["content"][1]["data"]))
            )
            r, g, b = pixels.getpixel((134, 538))
            path = "hill" if r > 150 and g < 100 and b < 100 else "oak"
            return {
                "text": f"From START cabin go to {path}, then the windmill position (halfway between hill and lake), then the bridge aligned north of windmill, then east to FINISH chapel."
            }
        # Uses only public instructions + follower's local frame, never oracle.
        text = observation["incoming"]
        a = [25, 95] if "hill," in text else [85, 35]
        return {
            "text": "Drawing that route and submitting.",
            "route": [[25, 35], a, [85, 95], [85, 155], [145, 155]],
            "submit": True,
        }


V2_RULES = """ Your maps have independently shifted landmarks: no single affine alignment recovers all corresponding positions. Reconstruct the route relative to YOUR map's landmarks, not a globally transformed grid. The route passes through ordered gaps BETWEEN landmark pairs, rather than visiting each landmark center. Modest variation within the intended route corridor is allowed. Infer and discuss the passages and their order from the giver's red route. START and FINISH remain named landmarks; the start and end positions are relative to those markers on each map."""


def system_prompts(version):
    if version == "v2":
        giver = GIVER_SYSTEM.replace("Describe the physical route", "Describe the landmark-relative route")
        follower = FOLLOWER_SYSTEM.replace("Use shared anchors and alignment to recover the physical route.",
                                           "Use ordered passages between named landmarks to recover a route on your own map.")
        return {"giver": giver + V2_RULES, "follower": follower + V2_RULES}
    return {"giver": GIVER_SYSTEM, "follower": FOLLOWER_SYSTEM}
