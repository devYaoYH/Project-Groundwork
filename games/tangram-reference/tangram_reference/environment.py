"""Environment-owned projection, validation, scoring and repeated session."""

import asyncio
import json
import random
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from pydantic import Field
from a2a_engine import EventLog, EpisodeConfigBase, EpisodeTrace, register_environment
from a2a_engine.content import AssetStore, TextPart
from a2a_engine.items import ItemBank
from a2a_engine.llm.factory import make_llm_client
from .agents import TangramReferenceAgent, ScriptedAgent
from .declaration import DECLARATION, BANK_PATH


class TangramReferenceConfig(EpisodeConfigBase):
    environment_id: str = "tangram_reference"
    num_agents: Literal[2] = 2
    num_rounds: int = Field(default=6, ge=2, le=6)
    max_exchanges: int = Field(default=6, ge=1, le=20)
    inventory_size: int = Field(default=4, ge=4, le=4)
    item_id: str = "seven_piece_four_figures_v1"
    asset_root: str | None = None
    image_assets: list[dict] = Field(default_factory=list)
    gallery_layout_indices: list[int] | None = None


class TangramReferenceEnvironment:
    def __init__(
        self,
        config,
        dry_run=False,
        *,
        episode=None,
        runtime_manager=None,
        runtime_context=None,
    ):
        self.config = (
            config
            if isinstance(config, TangramReferenceConfig)
            else TangramReferenceConfig(**config)
        )
        self.dry_run = dry_run
        self.events = EventLog.from_config(self.config)
        self.bank = ItemBank.load(
            BANK_PATH, expected_sha256=DECLARATION.item_policy.item_bank_sha256
        )
        self.item = self.bank.get(self.config.item_id)
        rng = random.Random(self.config.seed)
        indices = self.config.gallery_layout_indices
        if indices is None:
            indices = [
                rng.randrange(len(row["gallery_variants"]))
                for row in self.item.raw["rounds"]
            ]
        if len(indices) != 6 or any(
            not isinstance(i, int) or not 0 <= i < 24 for i in indices
        ):
            raise ValueError(
                "gallery_layout_indices must contain six integers in 0..23"
            )
        self.config.gallery_layout_indices = list(indices)
        self.rounds = [
            {**row, **row["gallery_variants"][i]}
            for row, i in zip(self.item.raw["rounds"], indices, strict=True)
        ]
        # Generated board evidence belongs to a writable runtime store, never
        # the installed, immutable item bank. Relocated bank panels are verified.
        if (
            self.config.asset_root is None
            or Path(self.config.asset_root).resolve() == self.bank.asset_store.root
        ):
            self.config.asset_root = tempfile.mkdtemp(
                prefix="groundwork-tangram-assets-"
            )
        self.store = AssetStore(self.config.asset_root)
        self.config.asset_root = str(self.store.root)
        for image in self.bank.assets:
            if self.store.root != self.bank.asset_store.root:
                self.store.put(self.bank.asset_store.read(image), image.mime_type)
        self.config.image_assets = [
            im.model_dump(exclude_none=True) for im in self.bank.assets
        ]
        from a2a_engine.remote.lobby import open_episode

        self._owns_episode = episode is None
        self.episode = episode or open_episode(
            runtime_manager,
            self.config.model_dump(),
            scripted=dry_run,
            runtime_context=runtime_context,
        )
        self.runtime = self.episode.managed
        try:
            self._build_agents()
        except BaseException:
            if self._owns_episode:
                self.episode.close()
            raise

    def _build_agents(self):
        for seat, role in enumerate(("director", "matcher")):
            spec = (
                self.config.agents[seat].model_dump()
                if seat < len(self.config.agents)
                else {}
            )
            if spec.get("runtime", "in_process") != "in_process":
                if not self.runtime:
                    raise ValueError("remote seats require runtime context")
                from .remote import RemoteTangramAgent

                agent = RemoteTangramAgent(
                    self.runtime, seat, asset_root=self.config.asset_root
                )
            elif self.dry_run or spec.get("type") == "scripted":
                agent = ScriptedAgent(role, self.config.asset_root)
            else:
                spec.update(
                    asset_root=self.config.asset_root,
                    required_modalities=["text", "image"],
                )
                if not spec.get("model"):
                    raise ValueError(
                        "Tangram needs explicitly configured multimodal models"
                    )
                agent = TangramReferenceAgent(make_llm_client(spec), role)
            setattr(self, role, agent)

    @classmethod
    def build_program(cls):
        from .workflow import build_program

        return build_program()

    @classmethod
    def runtime_capability(cls):
        from a2a_engine.workflow_runtime import RuntimeCapability
        from .remote import TANGRAM_TOOLS

        return RuntimeCapability(TANGRAM_TOOLS)

    def observation(self, state, role):
        row = self.rounds[state["round"] - 1]
        labels = [1, 2, 3, 4] if role == "director" else row["handles"]
        prompt = f"Round {state['round']} of {self.config.num_rounds}; exchange {state['exchange']} of {self.config.max_exchanges}. You are {role}. "
        prompt += (
            "The image shows target slots left to right."
            if role == "director"
            else "The image shows your gallery left to right with valid handles "
            + json.dumps(labels)
            + ". Your current board: "
            + json.dumps(state["board"])
            + "."
        )
        prompt += "\nPartner dialogue and completed-round feedback:\n" + "\n".join(
            f"{m['speaker']}: {m['text']}" for m in state["history"]
        )
        if role == "matcher" and state.get("validation_error"):
            prompt += "\nYour previous action was invalid: " + state["validation_error"]
        return {
            "round": state["round"],
            "exchange": state["exchange"],
            "role": role,
            "labels": labels,
            "messages": list(state["history"]),
            "content": [TextPart(text=prompt).model_dump(), row[f"{role}_image"]],
        }

    def board_image(self, round_number, board):
        """Reviewer evidence from accepted handles only; never the target order.

        This image is not added to dialogue or either seat's observation. Both
        references and exact bytes are retained for episode-scoped replay.
        """
        from .stimuli import panel

        row = self.rounds[round_number - 1]
        mapping = dict(zip(row["handles"], row["gallery"], strict=True))
        image = self.store.put(
            panel([mapping.get(handle) for handle in board], [1, 2, 3, 4]),
            "image/png",
        ).model_dump(exclude_none=True)
        if image not in self.config.image_assets:
            self.config.image_assets.append(image)
        return image

    def run(self):
        return asyncio.run(self._run_async())

    async def _run_async(self):
        from a2a_engine.remote.lobby import Registration
        from .workflow import GameContext

        self.events.append(
            "game_start",
            data={
                "num_rounds": self.config.num_rounds,
                "inventory_size": 4,
                "max_exchanges": self.config.max_exchanges,
            },
        )
        setup = {
            "round": 1,
            "exchange": 1,
            "history": [],
            "outcomes": [],
            "board": [None] * 4,
            "submitted": False,
            "validation_error": None,
        }
        try:
            with self.episode.started(
                {
                    i: Registration({"role": r, "num_rounds": self.config.num_rounds})
                    for i, r in enumerate(("director", "matcher"))
                },
                protocols=self.runtime_capability().protocols,
                register_timeout_s=120,
            ) as roster:
                final = await self.build_program().ainvoke(
                    setup,
                    context=GameContext(self, roster),
                    config={
                        "recursion_limit": self.config.num_rounds
                        * (self.config.max_exchanges * 6 + 6)
                        + 20
                    },
                )
                return self.trace(final)
        finally:
            if self._owns_episode:
                self.episode.close()

    def trace(self, state):
        outcomes = state["outcomes"]
        messages = [e for e in self.events.all() if e.type == "message"]
        self.events.append("game_end", data={"rounds_completed": len(outcomes)})
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "outcomes": outcomes,
                "rounds_completed": len(outcomes),
                "retained_context": True,
                "agent_requests": {
                    r: getattr(getattr(self, r), "request_summaries", [])
                    for r in ("director", "matcher")
                },
            },
            metrics={
                "accuracy": sum(o["accuracy"] for o in outcomes) / len(outcomes),
                "round_success": [int(o["success"]) for o in outcomes],
                "messages": len(messages),
                "words": sum(len(e.data["text"].split()) for e in messages),
            },
        )


register_environment(
    "tangram_reference",
    TangramReferenceEnvironment,
    declaration=DECLARATION,
    package="tangram-reference",
    dry_run_checks_keys=False,
)
