"""Repeated private-map grounding. Runtime lifecycle remains runner-owned."""

import asyncio
import json
import math
import uuid
from pathlib import Path
from pydantic import Field, model_validator
from a2a_engine import EpisodeConfigBase, EpisodeTrace, EventLog, register_environment
from a2a_engine.content import AssetStore, inline_media
from a2a_engine.llm.factory import make_llm_client
from a2a_engine.modalities import validate_config
from .agents import MapDirectorAgent, ScriptedMapAgent, GIVER_SYSTEM, FOLLOWER_SYSTEM
from .declaration import DECLARATION, BANK
from .maps import render_map, local


class MapDirectorConfig(EpisodeConfigBase):
    environment_id: str = "map_director"
    num_agents: int = 2
    num_rounds: int = Field(default=2, ge=2, le=2)
    max_turns: int = Field(default=6, ge=1, le=20)
    turn_timeout_s: float = Field(default=120, gt=0)
    item_id: str = "discrepant_pair"
    discrepancy: str = "missing_renamed+duplicate"
    required_modalities: list[str] = Field(default_factory=lambda: ["text", "image"])
    asset_root: str = "results/map_director_assets"
    image_assets: list[dict] = Field(default_factory=list)
    item_bank_sha256: str = BANK.item_bank_sha256

    @model_validator(mode="after")
    def roster_size(self):
        if self.num_agents != 2:
            raise ValueError("Map Director requires exactly two seats")
        if self.item_bank_sha256 != BANK.item_bank_sha256:
            raise ValueError("map bank digest mismatch")
        return self


def resolve_config(config):
    result = dict(config)
    supplied_digest = result.get("item_bank_sha256")
    if supplied_digest and supplied_digest != BANK.item_bank_sha256:
        raise ValueError("map bank digest mismatch")
    item = BANK.get(result.get("item_id", "discrepant_pair"))
    result.update(
        item_bank_sha256=BANK.item_bank_sha256, discrepancy=item.params["discrepancy"]
    )
    return result


class MapDirectorEnvironment:
    def __init__(
        self,
        config,
        dry_run=False,
        *,
        episode=None,
        runtime_manager=None,
        runtime_context=None,
    ):
        self.config = MapDirectorConfig(
            **resolve_config(
                config.model_dump() if isinstance(config, MapDirectorConfig) else config
            )
        )
        self.dry_run = dry_run
        self.events = EventLog.from_config(self.config)
        self.tasks = BANK.get(self.config.item_id).raw["tasks"]
        # Compiled designs point initial images at the immutable bank.
        # Runtime overlays belong in the evidence store, not the installed package.
        if Path(self.config.asset_root).resolve() == BANK.asset_store.root:
            self.config.asset_root = "results/map_director_assets"
        self.store = AssetStore(self.config.asset_root)
        for image in BANK.assets:
            self.store.put(BANK.asset_store.read(image), image.mime_type)
        self.assets = {
            image.sha256: image.model_dump(exclude_none=True) for image in BANK.assets
        }
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
            if not dry_run:
                validate_config(self.config.model_dump())
            for seat, role in enumerate(("giver", "follower")):
                spec = (
                    self.config.agents[seat].model_dump()
                    if len(self.config.agents) > seat
                    else {}
                )
                if spec.get("runtime", "in_process") != "in_process":
                    from .remote import RemoteMapAgent

                    agent = RemoteMapAgent(
                        self.runtime, seat, timeout_s=self.config.turn_timeout_s
                    )
                elif dry_run or spec.get("type") == "scripted":
                    agent = ScriptedMapAgent(role)
                else:
                    spec.update(
                        asset_root=str(self.store.root),
                        required_modalities=["text", "image"],
                    )
                    agent = MapDirectorAgent(make_llm_client(spec), role=role)
                setattr(self, role, agent)
        except BaseException:
            if self._owns_episode:
                self.episode.close()
            raise

    @classmethod
    def build_program(cls):
        from .workflow import build_program

        return build_program()

    @classmethod
    def runtime_capability(cls):
        from a2a_engine.workflow_runtime import RuntimeCapability
        from .remote import MAP_TOOLS

        return RuntimeCapability(MAP_TOOLS)

    def observation(self, state, role):
        task = self.tasks[state["round_index"]]
        incoming = state.get("incoming", "")
        public = {
            "round": state["round_index"] + 1,
            "turn": state["turn"] + 1,
            "turns_remaining": self.config.max_turns - state["turn"],
            "incoming": incoming,
        }
        if role == "follower" and state.get("validation_feedback"):
            public["validation_feedback"] = state["validation_feedback"]
        text = (
            json.dumps(public)
            + "\nThis is your private map. Read its own axis numbers. Reply with the prescribed JSON."
        )
        image = task["giver_image"] if role == "giver" else task["follower_image"]
        if role == "follower" and state["route"]:
            route = [
                [
                    (p[i] - task["follower_frame"]["offset"][i])
                    / task["follower_frame"]["scale"]
                    for i in range(2)
                ]
                for p in state["route"]
            ]
            image = self.store.put(
                render_map(
                    task["follower_landmarks"],
                    task["follower_frame"],
                    route,
                    draft=True,
                ),
                "image/png",
            ).model_dump(exclude_none=True)
            self.assets[image["sha256"]] = image
        content = [{"type": "text", "text": text}, image]
        self.events.append(
            "observation",
            data={
                "speaker": role,
                "round": public["round"],
                "content": content,
                "visibility": "private",
            },
        )
        return {**public, "content": inline_media(content, self.store.root)}

    def validate_action(self, action, state):
        route = state["route"]
        feedback = ""
        supplied = action.get("route")
        if supplied is not None:
            frame = self.tasks[state["round_index"]]["follower_frame"]
            low = local([0, 0], frame)
            high = local([100, 100], frame)
            valid = isinstance(supplied, list) and 2 <= len(supplied) <= 128
            if valid:
                valid = all(
                    isinstance(p, list)
                    and len(p) == 2
                    and all(
                        isinstance(v, (int, float))
                        and not isinstance(v, bool)
                        and math.isfinite(v)
                        and low[i] <= v <= high[i]
                        for i, v in enumerate(p)
                    )
                    for p in supplied
                )
            if valid:
                route = supplied
            else:
                feedback = "Invalid drawing: use 2–128 finite [x,y] points inside your map axes. Previous drawing retained."
        submitted = action.get("submit") is True and bool(route) and not feedback
        if action.get("submit") is True and not route and not feedback:
            feedback = "Submission needs a drawing of at least two points."
        return route, submitted, feedback

    def run(self):
        return asyncio.run(self._run_async())

    async def _run_async(self):
        from .workflow import GameContext
        from a2a_engine.remote.lobby import Registration

        try:
            self.events.append(
                "game_start",
                data={
                    "num_rounds": self.config.num_rounds,
                    "max_turns": self.config.max_turns,
                    "item_bank_sha256": BANK.item_bank_sha256,
                },
            )
            registrations = {
                0: Registration({"role": "giver"}, GIVER_SYSTEM),
                1: Registration({"role": "follower"}, FOLLOWER_SYSTEM),
            }
            with self.episode.started(
                registrations,
                protocols=self.runtime_capability().protocols,
                register_timeout_s=self.config.turn_timeout_s,
            ) as roster:
                result = await self.build_program().ainvoke(
                    {"round_index": 0, "outcomes": []},
                    context=GameContext(self, roster),
                    config={
                        "recursion_limit": self.config.num_rounds
                        * (self.config.max_turns * 5 + 8)
                        + 10
                    },
                )
                return result["trace"]
        finally:
            if self._owns_episode:
                self.episode.close()

    def trace(self, state):
        self.config.image_assets = list(self.assets.values())
        self.config.asset_root = str(self.store.root)
        outcomes = state["outcomes"]
        rate = sum(x["success"] for x in outcomes) / len(outcomes)
        self.events.append(
            "game_end", data={"success_rate": rate, "rounds_completed": len(outcomes)}
        )
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={"rounds": outcomes, "item_id": self.config.item_id},
            metrics={
                "success": [x["success"] for x in outcomes],
                "route_error": [x["route_error"] for x in outcomes],
                "turns_used": [x["turns_used"] for x in outcomes],
                "success_rate": rate,
            },
        )


register_environment(
    "map_director",
    MapDirectorEnvironment,
    package="map-director",
    declaration=DECLARATION,
    resolve_config=resolve_config,
    dry_run_checks_keys=False,
)
