"""Protocol tests for the hurricane environment: rules, boundaries, channels, scoring, termination."""

import asyncio
import copy
import json
import pytest
from hurricane.agents import HurricaneAgent, ScriptedManager, parse_turn, pooled_estimate
from hurricane.bank import load_bank, oracle
from hurricane.environment import HurricaneConfig, HurricaneEnvironment, reading
from hurricane.workflow import build_program


class Controlled:
    """Replays a policy function over the exact observations the graph hands out."""

    def __init__(self, name, policy):
        self.name, self.policy, self.observations = name, policy, []

    async def act(self, observation, tools):
        self.observations.append(copy.deepcopy(observation))
        return self.policy(observation)


def play(config=None, policies=None):
    game = HurricaneEnvironment({"seed": 7, **(config or {})}, dry_run=True)
    if policies:
        game.agents = [Controlled(f"p{i}", p) for i, p in enumerate(policies)]
    return game, game.run()


def events(trace, *types):
    return [e for e in trace.events if e.type in types]


def passive(observation):
    return {"actions": [{"kind": "pass"}]}


def main_only(table):
    """Policy from {round: [actions]} for main turns; passes everywhere else."""

    def policy(observation):
        if observation["turn_kind"] == "main":
            return {"actions": table.get(observation["round"], [{"kind": "pass"}])}
        return passive(observation)

    return policy


def observations_of(game, name):
    return next(a.observations for a in game.agents if a.name == name)


# -- bank and config ---------------------------------------------------------------


def test_frozen_bank_rows_match_their_oracle():
    bank = load_bank()
    assert bank.get("storm.central_city").oracle_result == {"hit_zones": [3, 4, 5], "population_at_risk": 90}
    assert bank.get("storm.wide_front").oracle_result["hit_zones"] == [2, 3, 4, 5, 6]
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
    with pytest.raises(ValueError, match="unknown storm"):
        HurricaneConfig(scenario_id="storm.nowhere")
    with pytest.raises(ValueError, match="sector"):
        HurricaneConfig(num_agents=4)


def test_readings_are_seeded_by_round_seat_and_instrument_not_play_order():
    cfg = HurricaneConfig(seed=7)
    assert reading(cfg, 1, 0, "recon") == reading(cfg, 1, 0, "recon")
    assert reading(cfg, 1, 0, "recon") != reading(cfg, 1, 1, "recon")
    assert reading(HurricaneConfig(seed=1), 1, 0, "model") != reading(HurricaneConfig(seed=2), 1, 0, "model")
    assert cfg.model_sigma(1) == 3.0 and cfg.model_sigma(6) == 1.0 and cfg.model_sigma(3) == pytest.approx(2.2)


# -- information boundaries ---------------------------------------------------------


def test_observation_hides_the_storm_and_unshared_readings():
    game = HurricaneEnvironment({}, dry_run=True)
    secret = [7, 8, 7, 8, 7]
    state = {
        **game.initial_state(),
        "round_index": 1, "seat": 0, "turn_kind": "main", "phase_limit": 3, "phase_turn": 0,
        "readings": {
            0: [{"round": 1, "instrument": "recon", "ensemble": [4, 4, 5, 4, 3], "audience": [0]}],
            1: [{"round": 1, "instrument": "model", "ensemble": secret, "audience": [1, 2]}],
            2: [{"round": 1, "instrument": "model", "ensemble": [2, 3, 4, 5, 6], "audience": [0, 1, 2]}],
        },
        "probe_log": [{"round": 1, "participant": 2, "instrument": "model"}],
        "estimates": [{"round": 1, "participant": 3, "value": 4, "audience": [0, 1, 2], "channel": "bulletin"},
                      {"round": 1, "participant": 2, "value": 8, "audience": [1, 2], "channel": "dm"}],
        "recon_remaining": 2,
    }
    obs = game.observation(state)
    serialized = json.dumps(obs)
    assert "landfall_zone" not in serialized and "hit_zones" not in serialized and "populations" not in obs
    assert str(secret) not in serialized and "7, 8, 7" not in serialized
    assert obs["your_readings"][0]["visible_to"] == [1]
    assert obs["known_data"] == [{"round": 1, "participant": 3, "instrument": "model", "ensemble": [2, 3, 4, 5, 6]}]
    assert obs["estimates"] == {"3": 4}
    assert obs["attention_budget"] == 3 and obs["your_zones"] == [0, 1, 2]
    game.episode.close()


def test_meeting_dm_and_board_audiences_are_enforced():
    def p0(o):
        k = o["turn_kind"]
        if k == "main" and o["round"] == 1:
            return {"actions": [
                {"kind": "probe", "instrument": "recon"},
                {"kind": "convene", "participants": [2], "agenda": "compare recon"},
                {"kind": "dm", "to": 3, "text": "privately: zone 4", "share": True, "estimate": 4},
            ]}
        if k == "meeting":
            said = any(m["participant"] == 1 for m in o["current_meeting"]["transcript"])
            return {"actions": [{"kind": "leave"}]} if said else {"actions": [
                {"kind": "say", "text": "my recon says 4", "share": True, "estimate": 4, "recommend": [3, 4, 5]}]}
        if k == "main":
            return {"actions": [{"kind": "write_board", "text": f"round {o['round']} note"}]}
        return passive(o)

    def p1(o):
        if o["turn_kind"] == "meeting":
            return {"actions": [{"kind": "say", "text": "noted, I agree", "share": False, "estimate": 4}]}
        if o["turn_kind"] == "main" and o["round"] == 2:
            return {"actions": [{"kind": "evacuate", "zones": [3, 4, 5]}]}
        return passive(o)

    def p2(o):
        if o["turn_kind"] == "reply":
            return {"actions": [{"kind": "dm", "to": 1, "text": "got it", "estimate": 4}]}
        return passive(o)

    game, trace = play({"num_rounds": 3, "evacuation_lead_time": 1}, [p0, p1, p2])
    m = trace.metrics
    assert m["meetings_convened"] == 1 and m["meeting_messages"] == 2 and m["dm_messages"] == 2
    assert m["reply_messages"] == 1 and m["board_writes"] == 2 and m["bulletin_messages"] == 0
    assert (m["readings_common"], m["readings_subset"], m["readings_private"]) == (1, 0, 0)
    assert m["total_cost"] == 90 and m["regret"] == 0
    assert m["recommendation_uptake"] == 1.0  # p1 ordered zones p0 recommended inside the meeting
    assert trace.final_state["meetings"][0]["attended"] == [1, 2]
    # Third parties never see meeting transcripts, DMs, or data shared through them.
    for obs in observations_of(game, "p2"):
        assert all("transcript" not in meeting for meeting in obs["meetings"])
        assert all(meeting["attendees"] == [1, 2] for meeting in obs["meetings"])
    for obs in observations_of(game, "p1"):
        assert obs["inbox"] == [] and obs["sent_dms"] == []
    p1_last, p2_last = observations_of(game, "p1")[-1], observations_of(game, "p2")[-1]
    assert [(r["participant"], r["instrument"]) for r in p1_last["known_data"]] == [(1, "recon")]
    assert [(r["participant"], r["instrument"]) for r in p2_last["known_data"]] == [(1, "recon")]
    assert p1_last["estimates"] == {"1": 4, "2": 4} and p2_last["estimates"] == {"1": 4, "3": 4}
    assert p1_last["recommendations"] and p2_last["recommendations"] == []
    assert all(e["text"] for e in p2_last["board"]) and len(p2_last["board"]) == 2
    # Trace events carry recipients and visibility for every private exchange.
    for e in events(trace, "message", "disclosure"):
        assert e.data["channel"] in ("dm", "meeting", "bulletin")
        assert (e.data["visibility"] == "public") == (e.data["channel"] == "bulletin")
        assert isinstance(e.data["to_agents"], list)
    meeting_messages = [e for e in events(trace, "message") if e.data["channel"] == "meeting"]
    assert [e.data["speaker"] for e in meeting_messages] == ["p0", "p1"]
    assert meeting_messages[0].data["to_agents"] == [2]


def test_meeting_dialogue_is_bounded_and_costs_attention_next_round():
    def convener(o):
        if o["turn_kind"] == "main" and o["round"] == 1:
            return {"actions": [{"kind": "convene", "participants": [2, 3], "agenda": "all hands"}]}
        if o["turn_kind"] == "meeting":
            return {"actions": [{"kind": "say", "text": "go on"}]}
        if o["turn_kind"] == "main":
            return {"actions": [{"kind": "probe", "instrument": "model"}, {"kind": "write_board", "text": "x"},
                                {"kind": "write_board", "text": "y"}]}
        return passive(o)

    def talker(o):
        if o["turn_kind"] == "meeting":
            return {"actions": [{"kind": "say", "text": "and on"}]}
        return convener(o) if o["turn_kind"] == "main" and o["round"] > 1 else passive(o)

    game, trace = play({"num_rounds": 2, "meeting_turns": 2, "meeting_attention_cost": 1}, [convener, talker, talker])
    assert trace.metrics["meeting_messages"] == 6  # three attendees x two turns, round robin
    meeting = [e for e in events(trace, "message") if e.data["channel"] == "meeting"]
    assert [e.data["participant"] for e in meeting] == [1, 2, 3, 1, 2, 3]
    assert events(trace, "meeting_end")[0].data["attended"] == [1, 2, 3]
    starts = events(trace, "round_start")
    assert starts[1].data["attention_debt"] == {"1": 1, "2": 1, "3": 1}
    # Round 2: each seat has budget 2, so the third action is rejected.
    ends = [e for e in events(trace, "turn_end") if e.data["round"] == 2]
    assert all(e.data["attention_budget"] == 2 and e.data["attention_used"] == 2 for e in ends)
    assert sum(e.data["reason"] == "attention_exhausted" for e in events(trace, "invalid_action")) == 3
    assert trace.metrics["board_writes"] == 3


def test_leaving_ends_a_meeting_and_a_dm_triggers_one_reply_turn():
    def p0(o):
        if o["turn_kind"] == "main" and o["round"] == 1:
            return {"actions": [{"kind": "convene", "participants": [2], "agenda": "quick"},
                                {"kind": "dm", "to": 2, "text": "ping"}]}
        if o["turn_kind"] == "meeting":
            return {"actions": [{"kind": "leave"}]}
        return passive(o)

    replies = []

    def p1(o):
        if o["turn_kind"] == "reply":
            replies.append(o)
            return {"actions": [{"kind": "dm", "to": 1, "text": "pong"}, {"kind": "dm", "to": 1, "text": "ignored"}]}
        if o["turn_kind"] == "meeting":
            raise AssertionError("meeting should close once the convener leaves")
        return passive(o)

    game, trace = play({"num_rounds": 1}, [p0, p1, passive])
    assert trace.metrics["meeting_messages"] == 0 and events(trace, "meeting_end")[0].data["attended"] == [2]
    assert len(replies) == 1 and replies[0]["inbox"][0]["text"] == "ping" and replies[0]["inbox"][0]["from"] == 1
    assert trace.metrics["dm_messages"] == 2 and trace.metrics["reply_messages"] == 1
    assert observations_of(game, "p2")[-1]["inbox"] == []
    _, quiet = play({"num_rounds": 1, "reply_turns": 0}, [p0, p1, passive])
    assert quiet.metrics["reply_messages"] == 0


# -- rules, scoring, termination ------------------------------------------------------


def test_scripted_episode_is_optimal_deterministic_and_private():
    game, trace = play()
    assert trace.metrics["total_cost"] == 90 and trace.metrics["regret"] == 0
    assert trace.metrics["population_exposed"] == 0 and trace.metrics["late_orders"] == 0
    assert (trace.metrics["recon_used"], trace.metrics["model_used"]) == (3, 15)
    assert trace.metrics["disclosure_rate"] == 1.0 and trace.metrics["knowledge_common_fraction"] == 1.0
    assert trace.metrics["estimate_agreement"] == 1.0 and trace.metrics["invalid_actions"] == 0
    assert trace.metrics["meetings_convened"] == 0 and trace.metrics["bulletin_messages"] == 18
    assert [o["round"] for o in trace.final_state["orders"]] == [4, 4, 4]
    assert trace.final_state["ordered_zones"] == [3, 4, 5] and trace.final_state["terminated_reason"] == "landfall"
    again = play()[1]
    assert [(e.type, e.data) for e in again.events] == [(e.type, e.data) for e in trace.events]
    private = events(trace, "probe", "observation")
    assert private and all(e.data["visibility"] == "private" for e in private)
    for event in events(trace, "observation"):
        assert "landfall_zone" not in json.loads(event.data["content"])


def test_lead_time_makes_late_orders_cost_without_protecting():
    _, held = play({"evacuation_lead_time": 6})
    assert held.final_state["orders"] == []
    assert held.metrics["casualty_cost"] == 900 and held.metrics["total_cost"] == 900
    assert held.metrics["hit_zones_protected_rate"] == 0

    def last_round_order(observation):
        if observation["turn_kind"] == "main" and observation["round"] == observation["rounds_total"]:
            return {"actions": [{"kind": "evacuate", "zones": observation["your_zones"]}]}
        return passive(observation)

    _, late = play({"evacuation_lead_time": 2}, [last_round_order] * 3)
    assert late.metrics["late_orders"] == 9 and late.metrics["orders"] == 9
    assert late.metrics["evacuation_cost"] == 210 and late.metrics["casualty_cost"] == 900
    assert all(not o["effective"] for o in late.final_state["orders"])
    assert late.metrics["probes_used"] == 0 and late.metrics["disclosure_rate"] == 0.0


def test_invalid_actions_consume_turns_and_never_commit():
    def seat0(o):
        if o["turn_kind"] != "main":
            return passive(o)
        return {"actions": [
            {"kind": "probe", "instrument": "recon"},  # budget is zero: rejected
            {"kind": "post", "text": "x", "estimate": 99},  # out of range
            {"kind": "evacuate", "zones": [5]},  # another sector
        ]}

    def seat1(o):
        if o["turn_kind"] != "main":
            return passive(o)
        return {"actions": [
            {"kind": "evacuate", "zones": [3, 3]},  # repeat order each round: only the first commits
            {"kind": "post", "text": "sharing", "share": True, "recommend": [3, 9]},  # zone 9 absent
            {"kind": "convene", "participants": [2], "agenda": "self"},  # cannot invite yourself
            {"kind": "dm", "to": 2, "text": "me"},  # cannot message yourself
        ]}

    def seat2(o):
        return {"parse_failed": True}

    game, trace = play({"num_rounds": 2, "recon_budget": 0, "attention_budget": 4}, [seat0, seat1, seat2])
    reasons = [e.data["reason"] for e in events(trace, "invalid_action")]
    assert trace.metrics["invalid_actions"] == len(reasons) == 6 + 7 + 2
    assert reasons.count("malformed_response") == 2
    assert trace.final_state["orders"] == [{"round": 1, "participant": 2, "zone": 3, "effective": False}]
    assert trace.metrics["messages"] == 0 and trace.metrics["meetings_convened"] == 0


def test_empty_recommendation_is_no_recommendation_not_a_rejection():
    def seat0(o):
        if o["turn_kind"] != "main" or o["round"] != 1:
            return passive(o)
        return {"actions": [
            {"kind": "probe", "instrument": "model"},
            {"kind": "post", "text": "nothing to recommend yet", "share": True, "estimate": 4, "recommend": []},
            {"kind": "dm", "to": 2, "text": "same here", "recommend": []},
        ]}

    game, trace = play({"num_rounds": 1}, [seat0, passive, passive])
    assert trace.metrics["invalid_actions"] == 0
    assert trace.metrics["bulletin_messages"] == 1 and trace.metrics["dm_messages"] == 1
    assert trace.metrics["recommendations"] == 0 and trace.metrics["disclosure_rate"] == 1.0
    posted = [e for e in events(trace, "message") if e.data["channel"] == "bulletin"][0]
    assert posted.data["recommend"] is None and posted.data["estimate"] == 4 and posted.data["shared_readings"] == 1
    # Evacuating nothing is still not an order.
    def evacuate_nothing(o):
        return {"actions": [{"kind": "evacuate", "zones": []}]} if o["turn_kind"] == "main" else passive(o)
    _, rejected = play({"num_rounds": 1}, [evacuate_nothing, passive, passive])
    assert rejected.metrics["invalid_actions"] == 1 and rejected.metrics["orders"] == 0


def test_sharing_discloses_every_unshared_reading_once_per_audience():
    def seat0(o):
        if o["turn_kind"] != "main":
            return passive(o)
        actions = [{"kind": "probe", "instrument": "model"}]
        if o["round"] == 2:
            actions.append({"kind": "dm", "to": 2, "text": "for you", "share": True})
        if o["round"] == 3:
            actions.append({"kind": "post", "text": "for all", "share": True, "estimate": 4})
        return {"actions": actions}

    game, trace = play({"num_rounds": 3, "attention_budget": 2}, [seat0, passive, passive])
    disclosures = events(trace, "disclosure")
    assert [(e.data["channel"], [r["round"] for r in e.data["readings"]]) for e in disclosures] == [
        ("dm", [1, 2]), ("bulletin", [1, 2, 3])]
    assert trace.metrics["readings_common"] == 3 and trace.metrics["disclosure_rate"] == 1.0
    p1 = observations_of(game, "p1")
    assert [r["round"] for r in p1[-1]["known_data"]] == [1, 2, 3]
    assert [r["round"] for r in observations_of(game, "p2")[-1]["known_data"]] == [1, 2, 3]
    # Partition in round 2 (before the bulletin post): the DM recipient knows rounds 1-2, the third seat nothing.
    round_two = [json.loads(e.data["content"]) for e in events(trace, "observation") if e.data["round"] == 2]
    assert [r["round"] for r in round_two[1]["known_data"]] == [1, 2]
    assert round_two[2]["known_data"] == []


def test_scripted_manager_pools_everything_it_knows_and_evacuates_in_time():
    observation = {
        "turn_kind": "main", "participant": 2, "round": 4, "last_effective_round": 4,
        "impact_radius": 1, "your_zones": [3, 4, 5], "attention_budget": 3, "probed_this_round": False,
        "participants": [{"participant": i} for i in (1, 2, 3)],
        "zones": [{"zone": z, "order": {"round": 1} if z == 3 else None} for z in range(9)],
        "your_readings": [{"round": 1, "instrument": "model", "ensemble": [6, 6], "visible_to": [1, 2, 3]}],
        "known_data": [{"round": 1, "participant": 1, "instrument": "recon", "ensemble": [4, 4, 4, 4]}],
        "instruments": {"recon": {"remaining": 0}},
    }
    assert pooled_estimate(observation) == 5
    action = asyncio.run(ScriptedManager("m").act(observation, {}))
    assert [a["kind"] for a in action["actions"]] == ["probe", "post", "evacuate"]
    assert action["actions"][2]["zones"] == [4, 5] and action["actions"][1]["share"] is True
    held = asyncio.run(ScriptedManager("m").act({**observation, "round": 3}, {}))
    assert [a["kind"] for a in held["actions"]] == ["probe", "post"]
    assert pooled_estimate({**observation, "your_readings": [], "known_data": []}) is None


def test_model_parser_bounded_history_and_turn_shapes():
    class Client:
        def __init__(self, lines):
            self.lines, self.messages = iter(lines), []

        def oneshot(self, messages):
            self.messages.append(copy.deepcopy(messages))
            return next(self.lines)

    assert parse_turn('[{"kind":"pass"}]') == {"actions": [{"kind": "pass"}]}
    assert parse_turn('{"kind":"leave"}') == {"kind": "leave"}
    assert parse_turn("nope") == {"parse_failed": True}
    client = Client(['```json\n{"actions":[{"kind":"probe","instrument":"recon"}]}\n```', "not json", "[1]", '{"kind":"pass"}'])
    agent = HurricaneAgent(client, "p0", history_window=1)
    assert asyncio.run(agent.act({"round": 1, "private": "my readings"}, {})) == {"actions": [{"kind": "probe", "instrument": "recon"}]}
    assert asyncio.run(agent.act({"round": 2}, {})) == {"parse_failed": True}
    assert asyncio.run(agent.act({"round": 3}, {})) == {"actions": [1]}
    # history_window=1 keeps only the previous turn in the provider request.
    assert [len(m) for m in client.messages] == [2, 4, 4]
    assert "my readings" not in json.dumps(client.messages[2])
    unbounded = HurricaneAgent(Client(['{"kind":"pass"}'] * 3), "p1")
    for r in range(3):
        asyncio.run(unbounded.act({"round": r}, {}))
    assert [len(m) for m in unbounded.client.messages] == [2, 4, 6]


def test_export_has_real_nested_phases_and_is_pure(monkeypatch):
    from a2a_engine.workflow import export_program
    from hurricane import environment, workflow

    def fail(*args, **kwargs):
        raise AssertionError("export executed game code")

    for module, names in (
        (workflow, ["begin_round", "record_turn", "landfall", "finalize", "observation", "manager", "open_meeting"]),
        (environment.HurricaneEnvironment, ["__init__", "observation", "landfall"]),
        (environment, ["reading", "make_llm_client"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, fail)
    doc = export_program(build_program(), "hurricane")
    assert doc == export_program(build_program(), "hurricane")
    assert doc["status"] == "available"
    ids = [g["id"] for g in doc["workflow"]["graphs"]]
    assert len(ids) == 7 and any("meeting" in i for i in ids) and any("reply" in i for i in ids)
    assert any(i.endswith("debrief") for i in ids) and any(i.endswith("storm") for i in ids)
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
    names = [f"participant_{i}" for i in range(3)] if identity_key is None else ["ana", "bo", "cy"]
    agents = [{"type": "scripted", **({identity_key: n} if identity_key else {})} for n in names]
    trace = HurricaneEnvironment({"num_rounds": 2, "agents": agents}, dry_run=True).run()
    spoken = {e.data["speaker"] for e in trace.events if e.type in ("message", "probe", "observation", "turn_end", "evacuation_order", "disclosure")}
    assert spoken == set(names)
    from a2a_engine.dataset import EpisodeDataset

    df = EpisodeDataset.from_traces([trace]).to_messages_df()
    assert not df.empty and set(df["speaker"]) == set(names)


def test_design_compilation_selects_the_frozen_storm():
    from a2a_engine.compiler import compile
    from a2a_engine.design import Design
    from hurricane.declaration import DECLARATION

    pins = {
        "num_rounds": 3, "evacuation_lead_time": 1, "ensemble_size": 3, "recon_budget": 1,
        "recon_sigma": 0.5, "model_sigma_initial": 2.0, "model_sigma_final": 1.0,
        "casualty_cost_per_capita": 10.0, "attention_budget": 3, "meeting_turns": 2,
        "meeting_attention_cost": 1, "meeting_cost": 0.0, "max_meetings_per_round": 1,
        "reply_turns": 1, "history_window": 4, "season_memory": "notes", "board_persists": False,
        "max_note_chars": 400,
    }
    design = Design.model_validate(
        {
            "schema_version": 1,
            "release": "hurricane@v2",
            "parameters": {
                "scenario_id": {"pin": "storm.wide_front"},
                "landfall_zone": {"randomize": True},
                "impact_radius": {"randomize": True},
                **{name: {"pin": value} for name, value in pins.items()},
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
    assert cfg["item_id"] == "storm.wide_front" and cfg["impact_radius"] == 2 and cfg["history_window"] == 4
    assert cfg["season_memory"] == "notes" and cfg["board_persists"] is False
    trace = HurricaneEnvironment(cfg, dry_run=True).run()
    assert trace.final_state["hit_zones"] == [2, 3, 4, 5, 6]
    assert trace.metrics["recon_used"] == 1 and trace.final_state["rounds_completed"] == 3
    # A compiled design selects one storm; a season is a plain config list and still a single episode.
    assert HurricaneConfig(**cfg).storms == ["storm.wide_front"]


# -- seasons: several storms, the same seats, bounded cross-storm memory ---------------------

SEASON = ["storm.central_city", "storm.northern_edge"]


def season_observations(trace, name, storm):
    """Observations one seat received during a storm, read back from the private trace events."""
    return [json.loads(e.data["content"]) for e in events(trace, "observation")
            if e.data["speaker"] == name and e.data.get("storm") == storm]


def test_season_config_resolves_storms_and_rejects_drift():
    cfg = HurricaneConfig(storms=SEASON)
    assert (cfg.scenario_id, cfg.landfall_zone, cfg.num_storms) == ("storm.central_city", 4, 2)
    assert cfg.for_storm(1).landfall_zone == 1 and cfg.for_storm(1).populations[0] == 40
    assert cfg.for_storm(1).attention_budget == cfg.attention_budget and cfg.for_storm(1).storms == SEASON
    assert HurricaneConfig().storms == ["storm.central_city"] and HurricaneConfig().num_storms == 1
    assert HurricaneConfig(scenario_id="storm.wide_front", storms=["storm.wide_front", "storm.wide_front"]).num_storms == 2
    with pytest.raises(ValueError, match="first storm"):
        HurricaneConfig(scenario_id="storm.wide_front", storms=SEASON)
    with pytest.raises(ValueError, match="unknown storm"):
        HurricaneConfig(storms=["storm.central_city", "storm.nowhere"])
    with pytest.raises(ValueError, match="season_memory"):
        HurricaneConfig(season_memory="everything")
    # A repeated scenario draws fresh noise in a later storm; a single storm keeps the v2 readings.
    assert reading(cfg, 1, 0, "model", storm=0) != reading(cfg, 1, 0, "model", storm=1)
    assert reading(cfg, 1, 0, "model") == reading(HurricaneConfig(), 1, 0, "model")


def test_single_storm_episode_carries_no_season_plumbing():
    game, trace = play()
    assert not events(trace, "storm_start", "storm_end", "note")
    assert all("season" not in json.loads(e.data["content"]) for e in events(trace, "observation"))
    assert all(e.data["turn_kind"] != "debrief" for e in events(trace, "observation"))
    assert trace.metrics["storms_completed"] == 1 and len(trace.final_state["storms"]) == 1
    assert trace.metrics["regret_first_storm"] == trace.metrics["regret_last_storm"] == 0
    assert trace.final_state["storms"][0]["scenario_id"] == "storm.central_city"


def test_season_resets_storm_state_keeps_the_board_and_records_public_outcomes():
    game, trace = play({"storms": SEASON})
    assert trace.metrics["storms_completed"] == 2 and [s["landfall_zone"] for s in trace.final_state["storms"]] == [4, 1]
    # Scripted managers are optimal on every storm, so season totals are the two oracle costs.
    assert trace.metrics["total_cost"] == 90 + 60 and trace.metrics["regret"] == 0
    assert [s["regret"] for s in trace.final_state["storms"]] == [0, 0]
    assert trace.metrics["recon_used"] == 6 and trace.metrics["probes_used"] == 36
    assert [e.data["storm"] for e in events(trace, "storm_start")] == [1, 2]
    assert [e.data["scenario_id"] for e in events(trace, "storm_start")] == SEASON
    assert "hit_zones" not in trace.final_state and len(trace.final_state["orders"]) == 6
    assert {o["storm"] for o in trace.final_state["orders"]} == {1, 2}
    assert len(trace.final_state["observation_chars_by_storm"]) == 2
    second = [json.loads(e.data["content"]) for e in events(trace, "observation") if e.data.get("storm") == 2]
    first_turn = second[0]
    assert first_turn["season"]["storm"] == 2 and first_turn["season"]["storms_total"] == 2
    assert first_turn["your_readings"] == [] and first_turn["known_data"] == [] and first_turn["bulletin"] == []
    assert first_turn["instruments"]["recon"]["remaining"] == 3 and first_turn["round"] == 1
    assert all(z["order"] is None for z in first_turn["zones"]) and first_turn["zones"][0]["population"] == 40
    record = first_turn["season"]["record"]
    assert len(record) == 1 and record[0]["landfall_zone"] == 4 and record[0]["hit_zones"] == [3, 4, 5]
    assert [o["zone"] for o in record[0]["orders"]] == [3, 4, 5] and record[0]["regret"] == 0
    # Nothing in any observation names where a storm went or what the other seats privately read.
    for obs in second:
        serialized = json.dumps(obs)
        assert "central_city" not in serialized and "northern_edge" not in serialized and "title" not in obs
        assert "landfall_zone" not in {k for k in obs} and "your_notes" not in obs
    again = play({"storms": SEASON})[1]
    assert [(e.type, e.data) for e in again.events] == [(e.type, e.data) for e in trace.events]


def test_board_persists_across_storms_unless_disabled():
    def writer(o):
        if o["turn_kind"] == "main" and o["round"] == 1 and o["season"]["storm"] == 1:
            return {"actions": [{"kind": "write_board", "text": "zone 4 took the hit last year"}]}
        return passive(o)

    for persists, expected in ((True, 1), (False, 0)):
        game, trace = play({"storms": SEASON, "num_rounds": 1, "board_persists": persists}, [writer, passive, passive])
        later = season_observations(trace, "p1", 2)
        assert later and all(len(o["board"]) == expected for o in later)
        assert events(trace, "storm_start")[1].data["board_entries"] == expected
        assert trace.metrics["board_writes"] == 1


def test_season_memory_none_withholds_the_record():
    game, trace = play({"storms": SEASON, "num_rounds": 1, "season_memory": "none"})
    later = season_observations(trace, "participant_0", 2)
    assert later and all("record" not in o["season"] and o["season"]["memory"] == "none" for o in later)
    assert not events(trace, "note") and trace.metrics["notes_written"] == 0


def test_season_memory_notes_runs_one_private_debrief_per_boundary():
    def noter(o):
        if o["turn_kind"] == "debrief":
            assert o["landfall_result"]["storm"] == o["season"]["storm"] and o["actions"] == ["note", "pass"]
            assert o["round"] == o["rounds_total"]
            return {"actions": [{"kind": "note", "text": f"p0 secret note after storm {o['season']['storm']}"}]}
        return passive(o)

    def silent(o):
        return {"actions": [{"kind": "pass"}]}

    def verbose(o):
        if o["turn_kind"] == "debrief":
            return {"actions": [{"kind": "note", "text": "x" * 51}]}
        return passive(o)

    cfg = {"storms": [*SEASON, "storm.wide_front"], "num_rounds": 1, "season_memory": "notes", "max_note_chars": 50}
    game, trace = play(cfg, [noter, silent, verbose])
    debriefs = [o for o in observations_of(game, "p0") if o["turn_kind"] == "debrief"]
    assert [o["season"]["storm"] for o in debriefs] == [1, 2]  # none after the final storm
    notes = events(trace, "note")
    assert [(e.data["storm"], e.data["speaker"]) for e in notes] == [(1, "p0"), (2, "p0")]
    assert all(e.data["visibility"] == "private" and e.data["to_agents"] == [] for e in notes)
    assert sum(e.data["reason"] == "rejected" for e in events(trace, "invalid_action")) == 2
    assert trace.metrics["notes_written"] == 2
    assert [e.data["notes"] for e in events(trace, "storm_end")] == [1, 1, 0]
    third = season_observations(trace, "p0", 3)
    assert third and [n["storm"] for n in third[0]["your_notes"]] == [1, 2]
    assert third[0]["your_notes"][0]["text"] == "p0 secret note after storm 1"
    for name in ("p1", "p2"):
        for o in season_observations(trace, name, 3):
            assert o["your_notes"] == [] and "secret note" not in json.dumps(o)
    # The scripted manager writes a deterministic note from the public result alone.
    scripted = play({**cfg, "max_note_chars": 1000})[1]
    assert scripted.metrics["notes_written"] == 6 and scripted.metrics["invalid_actions"] == 0
    assert "landfall at zone 4" in events(scripted, "note")[0].data["text"]


def test_agent_drops_provider_history_at_a_storm_boundary_unless_full():
    class Client:
        def __init__(self):
            self.messages = []

        def oneshot(self, messages):
            self.messages.append(copy.deepcopy(messages))
            return '{"kind":"pass"}'

    def run(agent, storms):
        for storm in storms:
            asyncio.run(agent.act({"season": {"storm": storm}} if storm else {"round": 1}, {}))
        return [len(m) for m in agent.client.messages]

    assert run(HurricaneAgent(Client(), "a", season_memory="none"), [1, 1, 2, 2]) == [2, 4, 2, 4]
    assert run(HurricaneAgent(Client(), "b", season_memory="outcomes"), [1, 1, 2]) == [2, 4, 2]
    assert run(HurricaneAgent(Client(), "c", season_memory="notes", history_window=1), [1, 1, 2]) == [2, 4, 2]
    assert run(HurricaneAgent(Client(), "d", season_memory="full"), [1, 1, 2]) == [2, 4, 6]
    # Without a season in the observation nothing is ever dropped.
    assert run(HurricaneAgent(Client(), "e", season_memory="none"), [None, None, None]) == [2, 4, 6]


def test_colleagues_may_be_addressed_by_participant_number_or_seat_name():
    def seat0(o):
        if o["turn_kind"] == "meeting":
            return {"actions": [{"kind": "leave"}]}
        if o["turn_kind"] != "main" or o["round"] != 1:
            return passive(o)
        return {"actions": [
            {"kind": "dm", "to": "bo", "text": "by name"},
            {"kind": "dm", "to": "3", "text": "by numeric string"},
            {"kind": "dm", "to": "participant_9", "text": "unknown name"},  # rejected
            {"kind": "dm", "to": "ana", "text": "to myself"},  # rejected
            {"kind": "convene", "participants": ["bo", 3], "agenda": "mixed addressing"},
        ]}

    def quiet(o):
        return {"actions": [{"kind": "leave"}]} if o["turn_kind"] == "meeting" else passive(o)

    agents = [{"type": "scripted", "id": n} for n in ("ana", "bo", "cy")]
    game = HurricaneEnvironment({"seed": 7, "num_rounds": 1, "attention_budget": 5, "agents": agents}, dry_run=True)
    game.agents = [Controlled(n, p) for n, p in (("ana", seat0), ("bo", quiet), ("cy", quiet))]
    trace = game.run()
    dms = [e.data for e in events(trace, "message") if e.data["channel"] == "dm"]
    assert [(d["to_agents"], d["text"]) for d in dms] == [([2], "by name"), ([3], "by numeric string")]
    assert [e.data["reason"] for e in events(trace, "invalid_action")] == ["rejected", "rejected"]
    assert events(trace, "convene")[0].data["attendees"] == [1, 2, 3]
    assert observations_of(game, "bo")[-1]["inbox"][0]["from"] == 1
