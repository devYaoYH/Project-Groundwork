"""Private multimodal seat policies with persistent agent-side chat context."""

import json
from a2a_engine import LLMAgent
from a2a_engine.content import observation_content

SYSTEM = """You are {role} in a repeated tangram reference game with the same partner.
The director sees figures in target slots 1..N. The matcher sees a shuffled gallery
with trial-local opaque handles, and must put those handles into target slot order.
The images contain silhouettes, not meaningful labels. Negotiate descriptions and
short names through dialogue. Earlier trial handles are invalid in a new trial.
Only your private image is visible to you. Do not invent access to your partner's
image, placement or evaluator answer. Preserve conventions across rounds.
Reply with exactly one JSON object: {{"text":"message to partner",
"arrangement": ["handle for slot 1", "handle for slot 2", ...], "submit": false}}.
Director: send only text, omit arrangement and submit. Describe ALL target slots
clearly; ask for clarification when needed. Matcher: use arrangement to replace
your complete board; omit it to leave the board unchanged. You may submit true
when confident, including in your first reply. Only a submitted/final board is
scored; use remaining exchanges for clarification, never ask for oracle answers.
"""


class TangramReferenceAgent(LLMAgent):
    def __init__(self, client, name):
        super().__init__(client, system_prompt=SYSTEM.format(role=name), name=name)
        self.history = []
        self.request_summaries = []

    def build_messages(self, observation, tools):
        message = {"role": "user", "content": observation_content(observation)}
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            message,
        ]
        self.history.append(message)
        self.request_summaries.append(
            {
                "round": observation["round"],
                "messages": len(messages),
                "image_parts": sum(
                    p.get("type") == "image"
                    for m in messages
                    if isinstance(m.get("content"), list)
                    for p in m["content"]
                ),
            }
        )
        return messages

    def parse_response(self, text, observation, tools):
        self.history.append({"role": "assistant", "content": text or ""})
        value = (text or "").strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            result = json.loads(value)
            if not isinstance(result, dict):
                raise ValueError()
            result["text"] = str(result.get("text", ""))
            result["parse_failed"] = False
            return result
        except (ValueError, TypeError):
            return {"text": text or "", "parse_failed": True}


class ScriptedAgent:
    """Smoke policy identifies image pixels, without private evaluator metadata."""

    supported_input_modalities = {"text", "image"}
    capabilities = {
        "input": ["text", "image"],
        "evidence": "tangram_reference.agents.ScriptedAgent pixel policy v1",
    }
    # Labels describe visible silhouettes, rather than transmitting shared
    # internal figure indices as if they were a model's emerging conventions.
    descriptions = (
        "a solid square",
        "a square with a lower right notch and a bottom triangle extending right",
        "a square with an upper right diamond notch and a top triangle extending right",
        "a square with both right notches and triangles extending right at top and bottom",
    )

    def __init__(self, name, asset_root):
        self.name = name
        self.asset_root = asset_root
        self.observations = []

    async def act(self, observation, tools):
        from io import BytesIO
        from PIL import Image
        from a2a_engine.content import image_bytes, ImagePart
        from .stimuli import figure

        self.observations.append(observation)
        image = Image.open(
            BytesIO(
                image_bytes(
                    ImagePart.model_validate(observation["content"][-1]),
                    self.asset_root,
                )
            )
        )
        order = []
        for i in range(len(observation["labels"])):
            tile = image.crop((i * 240 + 10, 30, i * 240 + 230, 250)).tobytes()
            order.append(next(j for j in range(4) if tile == figure(j, 220).tobytes()))
        if self.name == "director":
            return {
                "text": "; ".join(
                    f"Slot {slot}: {self.descriptions[index]}"
                    for slot, index in enumerate(order, 1)
                )
            }
        last = next(
            m["text"]
            for m in reversed(observation["messages"])
            if m["speaker"] == "director"
        )
        target = [
            self.descriptions.index(description.partition(": ")[2])
            for description in last.split("; ")
        ]
        return {
            "text": "I submit my arrangement.",
            "arrangement": [observation["labels"][order.index(j)] for j in target],
            "submit": True,
        }
