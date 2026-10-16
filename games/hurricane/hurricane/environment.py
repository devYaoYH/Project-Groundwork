"""Three coastal managers run instruments, pool readings and order evacuations."""

import asyncio
import hashlib
import random
import uuid
from collections import Counter
from pydantic import Field, model_validator
from a2a_engine import EventLog, EpisodeConfigBase, EpisodeTrace, register_environment
from a2a_engine.llm.factory import make_llm_client
from .agents import HurricaneAgent, ScriptedManager
from .bank import hit_zones, load_bank
from .declaration import DECLARATION

INSTRUMENTS = ("recon", "model")


class HurricaneConfig(EpisodeConfigBase):
    environment_id: str = "hurricane"
    num_agents: int = Field(default=3, ge=2, le=5)
    item_id: str | None = None
    scenario_id: str = "storm.central_city"
    # Frozen with the selected scenario row; a config may repeat but not change them.
    landfall_zone: int | None = None
    impact_radius: int | None = None
    populations: list[int] | None = None
    num_rounds: int = Field(default=6, ge=1, le=12)
    evacuation_lead_time: int = Field(default=2, ge=0, le=12)
    discussion_cycles: int = Field(default=1, ge=1, le=6)
    ensemble_size: int = Field(default=5, ge=1, le=20)
    recon_budget: int = Field(default=3, ge=0, le=20)
    recon_sigma: float = Field(default=0.6, ge=0, le=10)
    model_sigma_initial: float = Field(default=3.0, ge=0, le=10)
    model_sigma_final: float = Field(default=1.0, ge=0, le=10)
    evacuation_cost_per_capita: float = Field(default=1.0, ge=0)
    casualty_cost_per_capita: float = Field(default=10.0, ge=0)
    max_message_chars: int = Field(default=2000, ge=100, le=16000)
    turn_timeout_s: float = Field(default=120, gt=0, le=600)

    @model_validator(mode="before")
    @classmethod
    def select_frozen_scenario(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        selected = value.get("item_id") or value.get("scenario_id") or "storm.central_city"
        try:
            params = load_bank().get(selected).params
        except KeyError as exc:
            raise ValueError("unknown storm scenario") from exc
        for key in ("scenario_id", "landfall_zone", "impact_radius", "populations"):
            if key in value and value[key] is not None and value[key] != params[key]:
                raise ValueError(f"{key} differs from the selected frozen scenario")
            value[key] = params[key]
        return value

    @model_validator(mode="after")
    def coast_divides_into_sectors(self):
        if len(self.populations) % self.num_agents:
            raise ValueError("zones must divide evenly into one sector per manager")
        return self

    @property
    def num_zones(self):
        return len(self.populations)

    @property
    def zones_per_sector(self):
        return self.num_zones // self.num_agents

    def sector_of(self, zone):
        return zone // self.zones_per_sector

    def model_sigma(self, round_number):
        if self.num_rounds == 1:
            return self.model_sigma_initial
        fraction = (round_number - 1) / (self.num_rounds - 1)
        return self.model_sigma_initial + (self.model_sigma_final - self.model_sigma_initial) * fraction

    def sigma(self, instrument, round_number):
        return self.recon_sigma if instrument == "recon" else self.model_sigma(round_number)

    def order_effective(self, round_number):
        return self.num_rounds - round_number >= self.evacuation_lead_time

    @property
    def last_effective_round(self):
        last = self.num_rounds - self.evacuation_lead_time
        return last if last >= 1 else None


def reading(config, round_number, seat, instrument):
    """One ensemble; seeded by (seed, round, seat, instrument) so order of play never alters it."""
    blob = f"{config.seed or 0}:{round_number}:{seat}:{instrument}".encode()
    rng = random.Random(int.from_bytes(hashlib.sha256(blob).digest()[:8], "big"))
    sigma = config.sigma(instrument, round_number)
    return [
        min(config.num_zones - 1, max(0, round(rng.gauss(config.landfall_zone, sigma))))
        for _ in range(config.ensemble_size)
    ]


class HurricaneEnvironment:
    def __init__(
        self,
        config,
        dry_run=False,
        *,
        episode=None,
        runtime_manager=None,
        runtime_context=None,
    ):
        from a2a_engine.remote.lobby import open_episode

        self.config = (
            config if isinstance(config, HurricaneConfig) else HurricaneConfig(**config)
        )
        self.dry_run = dry_run
        self.events = EventLog.from_config(self.config)
        self.bank = load_bank()
        self._owns_episode = episode is None
        self.episode = episode or open_episode(
            runtime_manager,
            self.config.model_dump(),
            scripted=dry_run,
            runtime_context=runtime_context,
        )
        self.runtime = self.episode.managed
        self.agents = []
        try:
            for seat in range(self.config.num_agents):
                cfg = (
                    self.config.agents[seat].model_dump()
                    if seat < len(self.config.agents)
                    else {"type": "scripted" if dry_run else "llm", "model": "gpt-4o-mini"}
                )
                name = cfg.get("id") or cfg.get("participant_id") or f"participant_{seat}"
                if cfg.get("runtime", "in_process") != "in_process":
                    from .remote import RemoteManagerAgent

                    if self.runtime is None:
                        raise ValueError("remote manager requires runtime context")
                    agent = RemoteManagerAgent(
                        self.runtime, seat, timeout_s=self.config.turn_timeout_s, name=name
                    )
                elif dry_run or cfg.get("type") == "scripted":
                    agent = ScriptedManager(name)
                else:
                    agent = HurricaneAgent(make_llm_client(cfg), name)
                self.agents.append(agent)
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
        from a2a_engine.remote.contract import PROTOCOL_VERSION_V2
        from .remote import HURRICANE_TOOLS

        return RuntimeCapability(HURRICANE_TOOLS, protocols=frozenset({PROTOCOL_VERSION_V2}))

    # -- observations -----------------------------------------------------

    def zones_public(self, state):
        cfg = self.config
        evacuated = state["evacuated"]
        return [
            {
                "zone": z,
                "sector": cfg.sector_of(z),
                "population": cfg.populations[z],
                "order": {k: evacuated[z][k] for k in ("round", "participant", "effective")}
                if z in evacuated
                else None,
            }
            for z in range(cfg.num_zones)
        ]

    def observation(self, state):
        """Everything one seat may see. The storm and other seats' unshared readings are absent."""
        cfg, seat, phase = self.config, state["seat"], state["phase"]
        round_number = state["round_index"] + 1
        instructions = {
            "experiment": 'Run one instrument or pass: {"kind":"probe","instrument":"recon|model"} or {"kind":"pass"}. Recon is precise but the team budget is shared; model runs are noisy early and sharpen each round. Only you see the ensemble your instrument returns.',
            "discussion": 'Speak to the group: {"kind":"say","text":"...","share":true|false,"estimate":zone|null,"recommend":[zones]|null} or {"kind":"pass"}. share:true discloses all your unshared readings as data. estimate is your public landfall-zone estimate. recommend lists zones (any sector) you believe must be evacuated.',
            "decision": 'Order evacuations in your own sector: {"kind":"evacuate","zones":[...]} or {"kind":"hold"}. Orders are irreversible and cost the zone population; an order protects people only when issued at least evacuation_lead_time rounds before landfall.',
        }
        mine = list(state["readings"].get(seat, []))
        return {
            "round": round_number,
            "rounds_total": cfg.num_rounds,
            "rounds_after_this": cfg.num_rounds - round_number,
            "evacuation_lead_time": cfg.evacuation_lead_time,
            "order_effective_this_round": cfg.order_effective(round_number),
            "last_effective_round": cfg.last_effective_round,
            "participant": seat + 1,
            "name": self.agents[seat].name,
            "phase": phase,
            "your_sector": seat,
            "your_zones": [
                z for z in range(cfg.num_zones) if cfg.sector_of(z) == seat
            ],
            "impact_radius": cfg.impact_radius,
            "costs": {
                "evacuation_per_capita": cfg.evacuation_cost_per_capita,
                "casualty_per_capita": cfg.casualty_cost_per_capita,
            },
            "zones": self.zones_public(state),
            "instruments": {
                "recon": {"sigma": cfg.recon_sigma, "remaining": state["recon_remaining"]},
                "model": {"sigma_now": round(cfg.model_sigma(round_number), 3)},
                "ensemble_size": cfg.ensemble_size,
            },
            "probe_log": list(state["probe_log"]),
            "your_readings": mine,
            "shared_data": list(state["shared_data"]),
            "estimates": {
                str(s + 1): e for s, e in sorted(state["estimates"].items())
            },
            "recommendations": list(state["recommendations"]),
            "discussion": list(state["discussion"]),
            "turns_remaining": state["phase_limit"] - state["phase_turn"],
            "response_instruction": instructions[phase],
        }

    # -- lifecycle --------------------------------------------------------

    def run(self):
        return asyncio.run(self._run_async())

    async def _run_async(self):
        from .workflow import GameContext, recursion_limit
        from a2a_engine.remote.lobby import Registration

        cfg = self.config
        self.events.append(
            "game_start",
            data={
                "num_rounds": cfg.num_rounds,
                "num_agents": cfg.num_agents,
                "num_zones": cfg.num_zones,
                "populations": list(cfg.populations),
                "recon_budget": cfg.recon_budget,
                "evacuation_lead_time": cfg.evacuation_lead_time,
                "scenario_id": cfg.scenario_id,
                "item_bank_sha256": self.bank.item_bank_sha256,
            },
        )
        registrations = {
            i: Registration(
                {"role": "manager", "participant": i + 1, "participant_id": self.agents[i].name}
            )
            for i in range(cfg.num_agents)
        }
        try:
            with self.episode.started(
                registrations,
                protocols=self.runtime_capability().protocols,
                register_timeout_s=cfg.turn_timeout_s,
            ) as roster:
                state = await self.build_program().ainvoke(
                    {
                        "round_index": 0,
                        "readings": {},
                        "probe_log": [],
                        "shared_data": [],
                        "estimates": {},
                        "recommendations": [],
                        "discussion": [],
                        "orders": [],
                        "evacuated": {},
                        "recon_remaining": cfg.recon_budget,
                        "invalid_actions": 0,
                    },
                    context=GameContext(self, roster),
                    config={"recursion_limit": recursion_limit(cfg)},
                )
            return self.trace(state)
        finally:
            if self._owns_episode:
                self.episode.close()

    # -- scoring ------------------------------------------------------------

    def landfall(self, state):
        cfg = self.config
        hit = hit_zones(cfg.landfall_zone, cfg.impact_radius, cfg.num_zones)
        evacuated = state["evacuated"]
        ordered = sorted(evacuated)
        protected = [z for z in hit if z in evacuated and evacuated[z]["effective"]]
        exposed = [z for z in hit if z not in protected]
        needless = [z for z in ordered if z not in hit]
        population = lambda zones: sum(cfg.populations[z] for z in zones)  # noqa: E731
        evacuation_cost = cfg.evacuation_cost_per_capita * population(ordered)
        casualty_cost = cfg.casualty_cost_per_capita * population(exposed)
        oracle_cost = cfg.evacuation_cost_per_capita * population(hit)
        return {
            "landfall_zone": cfg.landfall_zone,
            "hit_zones": hit,
            "ordered_zones": ordered,
            "protected_zones": protected,
            "exposed_zones": exposed,
            "needlessly_evacuated_zones": needless,
            "population_at_risk": population(hit),
            "population_protected": population(protected),
            "population_exposed": population(exposed),
            "population_needlessly_evacuated": population(needless),
            "evacuation_cost": evacuation_cost,
            "casualty_cost": casualty_cost,
            "total_cost": evacuation_cost + casualty_cost,
            "oracle_cost": oracle_cost,
            "regret": evacuation_cost + casualty_cost - oracle_cost,
            "late_orders": sum(not o["effective"] for o in state["orders"]),
            "hit_zones_protected_rate": len(protected) / len(hit) if hit else 1.0,
        }

    def trace(self, state):
        cfg, result = self.config, state["result"]
        events = self.events.all()
        messages = [e for e in events if e.type == "message"]
        readings = [r for rows in state["readings"].values() for r in rows]
        estimates = state["estimates"]
        errors = [abs(e - cfg.landfall_zone) for e in estimates.values()]
        modal = Counter(estimates.values()).most_common(1)
        recommended_by_other = {
            (zone, participant)
            for rec in state["recommendations"]
            for zone in rec["zones"]
            for participant in range(1, cfg.num_agents + 1)
            if participant != rec["participant"]
        }
        uptake = [
            (o["zone"], o["participant"]) in recommended_by_other for o in state["orders"]
        ]
        metrics = {
            **{
                k: result[k]
                for k in (
                    "total_cost",
                    "evacuation_cost",
                    "casualty_cost",
                    "oracle_cost",
                    "regret",
                    "population_at_risk",
                    "population_protected",
                    "population_exposed",
                    "population_needlessly_evacuated",
                    "hit_zones_protected_rate",
                    "late_orders",
                )
            },
            "orders": len(state["orders"]),
            "probes_used": len(readings),
            "recon_used": sum(r["instrument"] == "recon" for r in readings),
            "model_used": sum(r["instrument"] == "model" for r in readings),
            "readings_shared": sum(r["shared"] for r in readings),
            "disclosure_rate": sum(r["shared"] for r in readings) / len(readings)
            if readings
            else 0.0,
            "estimates_reported": len(estimates),
            "estimate_error": sum(errors) / len(errors) if errors else 0.0,
            "estimate_agreement": modal[0][1] / len(estimates) if estimates else 0.0,
            "recommendations": len(state["recommendations"]),
            "recommendation_uptake": sum(uptake) / len(uptake) if uptake else 0.0,
            "messages": len(messages),
            "dialogue_words": sum(len(e.data["text"].split()) for e in messages),
            "invalid_actions": state["invalid_actions"],
        }
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=cfg,
            events=events,
            final_state={
                **result,
                "orders": list(state["orders"]),
                "estimates": {str(s + 1): e for s, e in sorted(estimates.items())},
                "recon_remaining": state["recon_remaining"],
                "rounds_completed": state["round_index"],
                "terminated_reason": "landfall",
            },
            metrics=metrics,
        )


register_environment(
    "hurricane",
    HurricaneEnvironment,
    package="hurricane",
    declaration=DECLARATION,
    dry_run_checks_keys=False,
)
