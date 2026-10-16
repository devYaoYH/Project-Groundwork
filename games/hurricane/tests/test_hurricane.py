"""Protocol tests for the hurricane environment: rules, boundaries, scoring, termination."""

import asyncio
import copy
import json
import pytest
from hurricane.agents import HurricaneAgent, ScriptedManager, pooled_estimate
from hurricane.bank import load_bank, oracle
from hurricane.environment import HurricaneConfig, HurricaneEnvironment, reading
from hurricane.workflow import build_program


class ControlledManager:
    """Replays a policy function over the exact observations the graph hands out."""

    def __init__(self, name, policy):
        self.name, self.policy, self.observations = name, policy, []

    async def act(self, observation, tools):
        self.observations.append(copy.deepcopy(observation))
        return self.policy(observation)


def play(config=None, policies=None):
    game = HurricaneEnvironment({"seed": 7, **(config or {})}, dry_run=True)
    if policies:
        game.agents = [ControlledManager(f"manager_{i}", p) for i, p in enumerate(policies)]
    return game, game.run()


def events(trace, *types):
    return [e for e in trace.events if e.type in types]


def hold_or_pass(observation):
    return {"kind": {"experiment": "pass", "discussion": "pass", "decision": "hold"}[observation["phase"]]}


def test_frozen_bank_rows_match_their_oracle():
    bank = load_bank()
    assert bank.get("storm.central_city").oracle_result == {"hit_zones": [3, 4, 5], "population_at_risk": 90}
    assert bank.get("storm.wide_front").oracle_result["hit_zones"] == [2, 3, 4, 5, 6]
    assert bank.get("storm.southern_village").oracle_result["population_at_risk"] == 60
    assert oracle({"landfall_zone": 0, "impact_radius": 1, "populations": [5, 6, 7]}) == {
        "hit_zones": [0, 1],
        "population_at_risk": 11,
    }


def test_config_freezes_scenario_and_rejects_drift():
    cfg = HurricaneConfig(scenario_id="storm.wide_front")
    assert (cfg.landfall_zone, cfg.impact_radius, cfg.num_zones) == (4, 2, 9)
    assert HurricaneConfig(item_id="storm.northern_edge").populations[0] == 40
    with pytest.raises(ValueError, match="frozen scenario"):
        HurricaneConfig(scenario_id="storm.wide_front", impact_radius=1)
    with pytest.raises(ValueError, match="frozen scenario"):
        HurricaneConfig(landfall_zone=2)
    with pytest.raises(ValueError, match="unknown storm"):
        HurricaneConfig(scenario_id="storm.nowhere")
    with pytest.raises(ValueError, match="sector"):
        HurricaneConfig(num_agents=4)


def test_observation_hides_the_storm_and_unshared_readings():
    game = HurricaneEnvironment({}, dry_run=True)
    secret = [7, 8, 7, 8, 7]
    state = {
        "round_index": 1, "seat": 0, "phase": "discussion", "phase_limit": 3, "phase_turn": 0,
        "readings": {
            0: [{"round": 1, "instrument": "recon", "ensemble": [4, 4, 5, 4, 3], "shared": False}],
            1: [{"round": 1, "instrument": "model", "ensemble": secret, "shared": False}],
            2: [{"round": 1, "instrument": "model", "ensemble": [2, 3, 4, 5, 6], "shared": True}],
        },
        "probe_log": [{"round": 1, "participant": 2, "instrument": "model"}],
        "shared_data": [{"round": 1, "participant": 3, "instrument": "model", "ensemble": [2, 3, 4, 5, 6]}],
        "estimates": {2: 4}, "recommendations": [], "discussion": [], "orders": [],
        "evacuated": {}, "recon_remaining": 2,
    }
    obs = game.observation(state)
    serialized = json.dumps(obs)
    assert "landfall_zone" not in serialized and "hit_zones" not in serialized
    assert "landfall_zone" not in obs and "populations" not in obs
    assert str(secret) not in serialized and "7, 8, 7" not in serialized
    assert obs["your_readings"][0]["ensemble"] == [4, 4, 5, 4, 3]
    assert obs["shared_data"][0]["participant"] == 3
    assert obs["probe_log"] == [{"round": 1, "participant": 2, "instrument": "model"}]
    assert obs["estimates"] == {"3": 4} and obs["your_zones"] == [0, 1, 2]
    assert obs["order_effective_this_round"] and obs["last_effective_round"] == 4
    game.episode.close()


def test_scripted_episode_is_optimal_deterministic_and_private():
    game, trace = play()
    assert trace.metrics["total_cost"] == 90 and trace.metrics["regret"] == 0
    assert trace.metrics["population_exposed"] == 0 and trace.metrics["late_orders"] == 0
    assert (trace.metrics["recon_used"], trace.metrics["model_used"]) == (3, 15)
    assert trace.metrics["disclosure_rate"] == 1.0 and trace.metrics["estimate_agreement"] == 1.0
    assert trace.metrics["recommendation_uptake"] == 1.0 and trace.metrics["invalid_actions"] == 0
    assert [o["round"] for o in trace.final_state["orders"]] == [4, 4, 4]
    assert trace.final_state["ordered_zones"] == [3, 4, 5]
    assert trace.final_state["terminated_reason"] == "landfall"
    again = play()[1]
    assert [(e.type, e.data) for e in again.events] == [(e.type, e.data) for e in trace.events]
    assert again.metrics == trace.metrics
    private = events(trace, "probe", "observation")
    assert private and all(e.data["visibility"] == "private" for e in private)
    for event in events(trace, "observation"):
        content = json.loads(event.data["content"])
        assert "landfall_zone" not in content
        for row in content["your_readings"]:
            assert row["round"] <= content["round"]
    # Every ensemble in a later observation's shared data was disclosed by an earlier disclosure event.
    disclosed = {(r["round"], e.data["participant"], tuple(r["ensemble"])) for e in events(trace, "disclosure") for r in e.data["readings"]}
    for event in events(trace, "observation"):
        for row in json.loads(event.data["content"])["shared_data"]:
            assert (row["round"], row["participant"], tuple(row["ensemble"])) in disclosed


def test_readings_are_seeded_by_round_seat_and_instrument_not_play_order():
    cfg = HurricaneConfig(seed=7)
    assert reading(cfg, 1, 0, "recon") == reading(cfg, 1, 0, "recon")
    assert reading(cfg, 1, 0, "recon") != reading(cfg, 1, 1, "recon")
    assert reading(HurricaneConfig(seed=1), 1, 0, "model") != reading(HurricaneConfig(seed=2), 1, 0, "model")
    assert all(0 <= z < 9 for z in reading(HurricaneConfig(seed=3, model_sigma_initial=10), 1, 0, "model"))
    assert cfg.model_sigma(1) == 3.0 and cfg.model_sigma(6) == 1.0 and cfg.model_sigma(3) == pytest.approx(2.2)


def test_lead_time_makes_late_orders_cost_without_protecting():
    _, held = play({"evacuation_lead_time": 6})
    assert held.final_state["orders"] == []
    assert held.metrics["casualty_cost"] == 900 and held.metrics["total_cost"] == 900
    assert held.metrics["hit_zones_protected_rate"] == 0

    def last_round_order(observation):
        if observation["phase"] == "decision" and observation["round"] == observation["rounds_total"]:
            return {"kind": "evacuate", "zones": observation["your_zones"]}
        return hold_or_pass(observation)

    _, late = play({"evacuation_lead_time": 2}, [last_round_order] * 3)
    assert late.metrics["late_orders"] == 9 and late.metrics["orders"] == 9
    assert late.metrics["evacuation_cost"] == 210 and late.metrics["casualty_cost"] == 900
    assert late.metrics["population_needlessly_evacuated"] == 120
    assert all(not o["effective"] for o in late.final_state["orders"])
    assert late.metrics["probes_used"] == 0 and late.metrics["disclosure_rate"] == 0.0


def test_invalid_actions_consume_turns_and_never_commit():
    def seat0(observation):
        phase, r = observation["phase"], observation["round"]
        if phase == "experiment":
            return {"kind": "probe", "instrument": "recon"}  # budget is zero: invalid
        if phase == "discussion":
            return {"kind": "say", "text": "x", "estimate": 99} if r == 1 else {"kind": "say", "text": "x", "share": "yes"}
        return {"kind": "evacuate", "zones": [5]}  # another sector

    def seat1(observation):
        phase = observation["phase"]
        if phase == "decision":
            return {"kind": "evacuate", "zones": [3, 3]}  # repeat order each round: only the first commits
        if phase == "discussion":
            return {"kind": "say", "text": "sharing", "share": True, "recommend": [3, 9]}  # zone 9 absent
        return {"kind": "probe", "instrument": "model"}

    def seat2(observation):
        return {"parse_failed": True}

    game, trace = play({"num_rounds": 2, "recon_budget": 0}, [seat0, seat1, seat2])
    invalid = events(trace, "invalid_action")
    # seat 0: every turn; seat 1: both discussion turns and the repeated order; seat 2: every turn.
    assert trace.metrics["invalid_actions"] == len(invalid) == 6 + 3 + 6
    assert trace.final_state["orders"] == [{"round": 1, "participant": 2, "zone": 3, "effective": False}]
    assert trace.metrics["recon_used"] == 0 and trace.metrics["model_used"] == 2
    assert trace.metrics["recommendations"] == 0 and trace.metrics["estimates_reported"] == 0
    assert trace.metrics["messages"] == 0 and trace.final_state["ordered_zones"] == [3]


def test_sharing_discloses_every_unshared_reading_once():
    def seat0(observation):
        phase, r = observation["phase"], observation["round"]
        if phase == "experiment":
            return {"kind": "probe", "instrument": "model"}
        if phase == "discussion":
            return {"kind": "say", "text": "all of it", "share": r == 2, "estimate": 4}
        return {"kind": "hold"}

    def seat1(observation):
        if observation["phase"] == "discussion":
            return {"kind": "say", "text": "I recommend the city", "recommend": [4]}
        return hold_or_pass(observation)

    def seat2(observation):
        if observation["phase"] == "decision" and observation["round"] == 3:
            return {"kind": "evacuate", "zones": [6]}
        if observation["phase"] == "discussion":
            return {"kind": "say", "text": "I estimate 5", "estimate": 5, "recommend": [6]}
        return hold_or_pass(observation)

    game, trace = play({"num_rounds": 3, "evacuation_lead_time": 0}, [seat0, seat1, seat2])
    disclosures = events(trace, "disclosure")
    assert len(disclosures) == 1 and [r["round"] for r in disclosures[0].data["readings"]] == [1, 2]
    assert trace.metrics["readings_shared"] == 2 and trace.metrics["disclosure_rate"] == pytest.approx(2 / 3)
    final = json.loads(events(trace, "observation")[-1].data["content"])
    assert [r["round"] for r in final["shared_data"]] == [1, 2]
    assert all(r["participant"] == 1 for r in final["shared_data"])
    assert trace.metrics["estimate_error"] == 0.5 and trace.metrics["estimate_agreement"] == 0.5
    # Zone 6 was recommended only by its own manager: no uptake from another colleague.
    assert trace.metrics["recommendation_uptake"] == 0.0
    assert game.agents[0].observations[-1]["evacuation_lead_time"] == 0


def test_scripted_manager_pools_shared_data_and_evacuates_in_time():
    observation = {
        "participant": 2, "phase": "decision", "round": 4, "last_effective_round": 4,
        "impact_radius": 1, "your_zones": [3, 4, 5],
        "zones": [{"zone": z, "order": {"round": 1} if z == 3 else None} for z in range(9)],
        "your_readings": [{"round": 1, "instrument": "model", "ensemble": [6, 6], "shared": True}],
        "shared_data": [
            {"round": 1, "participant": 2, "instrument": "model", "ensemble": [6, 6]},
            {"round": 1, "participant": 1, "instrument": "recon", "ensemble": [4, 4, 4, 4]},
        ],
        "instruments": {"recon": {"remaining": 0}},
    }
    assert pooled_estimate(observation) == 5
    action = asyncio.run(ScriptedManager("m").act(observation, {}))
    assert action == {"kind": "evacuate", "zones": [4, 5]}
    assert asyncio.run(ScriptedManager("m").act({**observation, "round": 3}, {})) == {"kind": "hold"}
    assert pooled_estimate({**observation, "your_readings": [], "shared_data": []}) is None


def test_model_parser_and_private_history():
    class Client:
        def __init__(self, lines):
            self.lines, self.messages = iter(lines), []

        def oneshot(self, messages):
            self.messages.append(copy.deepcopy(messages))
            return next(self.lines)

    client = Client(['```json\n{"kind":"probe","instrument":"recon"}\n```', "not json", "[1]", '{"kind":"hold"}'])
    agent = HurricaneAgent(client, "manager_0")
    assert asyncio.run(agent.act({"round": 1, "private": "my readings"}, {})) == {"kind": "probe", "instrument": "recon"}
    assert asyncio.run(agent.act({"round": 2}, {})) == {"parse_failed": True}
    assert asyncio.run(agent.act({"round": 3}, {})) == {"parse_failed": True}
    assert [len(m) for m in client.messages] == [2, 4, 6]
    assert "my readings" in client.messages[2][1]["content"]
    agent.history_allowed = lambda: False
    assert asyncio.run(agent.act({"round": 4}, {})) == {"parse_failed": True}
    assert len(agent.history) == 6


def test_export_has_real_nested_phases_and_is_pure(monkeypatch):
    from a2a_engine.workflow import export_program
    from hurricane import environment, workflow

    def fail(*args, **kwargs):
        raise AssertionError("export executed game code")

    for module, names in (
        (workflow, ["begin_round", "record_turn", "landfall", "finalize", "observation", "manager"]),
        (environment.HurricaneEnvironment, ["__init__", "observation", "landfall"]),
        (environment, ["reading", "make_llm_client"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, fail)
    doc = export_program(build_program(), "hurricane")
    assert doc == export_program(build_program(), "hurricane")
    assert doc["status"] == "available"
    assert len(doc["workflow"]["graphs"]) == 5
    assert {g["id"].rsplit(":", 1)[-1].split("/")[-1] for g in doc["workflow"]["graphs"]} >= set()
    assert any("discussion" in g["id"] for g in doc["workflow"]["graphs"])
    top = doc["workflow"]["graphs"][0]
    assert {e.get("route_key") for e in top["edges"] if e["conditional"]} == {"next", "done"}


def test_agent_gateway_rejects_hidden_calls():
    from hurricane.workflow import GameContext

    game = HurricaneEnvironment({}, dry_run=True)
    GameContext(game)
    with pytest.raises(RuntimeError, match="executable AgentStep"):
        asyncio.run(game.agents[0].act({}, {}))
    game.episode.close()


@pytest.mark.parametrize("identity_key", [None, "id", "participant_id"])
def test_speakers_follow_the_roster(identity_key):
    names = [f"manager_{i}" for i in range(3)] if identity_key is None else ["ana", "bo", "cy"]
    agents = [{"type": "scripted", **({identity_key: n} if identity_key else {})} for n in names]
    trace = HurricaneEnvironment({"num_rounds": 2, "agents": agents}, dry_run=True).run()
    spoken = {e.data["speaker"] for e in trace.events if e.type in ("message", "probe", "observation", "hold", "evacuation_order", "disclosure")}
    assert spoken == set(names)
    from a2a_engine.dataset import EpisodeDataset

    df = EpisodeDataset.from_traces([trace]).to_messages_df()
    assert not df.empty and set(df["speaker"]) == set(names)


def test_design_compilation_selects_the_frozen_storm():
    from a2a_engine.compiler import compile
    from a2a_engine.design import Design
    from hurricane.declaration import DECLARATION

    design = Design.model_validate(
        {
            "schema_version": 1,
            "release": "hurricane@v1",
            "parameters": {
                "scenario_id": {"pin": "storm.wide_front"},
                "landfall_zone": {"randomize": True},
                "impact_radius": {"randomize": True},
                "num_rounds": {"pin": 3},
                "evacuation_lead_time": {"pin": 1},
                "discussion_cycles": {"pin": 1},
                "ensemble_size": {"pin": 3},
                "recon_budget": {"pin": 1},
                "recon_sigma": {"pin": 0.5},
                "model_sigma_initial": {"pin": 2.0},
                "model_sigma_final": {"pin": 1.0},
                "casualty_cost_per_capita": {"pin": 10.0},
            },
            "units": {"episodes_per_cell": 1},
            "seed": {"root": 19},
            "roster": [
                {"id": f"m{i}", "role": "manager", "kind": "scripted", "binding": "baseline"}
                for i in range(3)
            ],
        }
    )
    plan = compile(design, DECLARATION, load_bank(), experiment_id="storm-design", experiment_name="wide")
    cfg = plan.preview_episode_config
    assert cfg["item_id"] == "storm.wide_front" and cfg["impact_radius"] == 2
    trace = HurricaneEnvironment(cfg, dry_run=True).run()
    assert trace.final_state["hit_zones"] == [2, 3, 4, 5, 6]
    assert trace.metrics["recon_used"] == 1 and trace.final_state["rounds_completed"] == 3
    assert trace.config.scenario_id == "storm.wide_front"
