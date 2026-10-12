import copy
import json
import pytest
from hidden_profile.agents import HiddenProfileAgent, ScriptedParticipant
from hidden_profile.bank import load_bank, score
from hidden_profile.environment import HiddenProfileEnvironment
from hidden_profile.workflow import build_program


def test_frozen_oracle_and_shared_evidence_deduplicate():
    bank = load_bank()
    row = bank.get("caucus_hidden_01").raw
    assert score(row["facts"] * 4) == {"A": 6, "B": 11, "C": 4}
    assert row["oracle_result"]["packet_scores"] == [{"A": 6, "B": 5, "C": 1}] * 4
    assert bank.get("relief_hidden_01").oracle_result["pooled_scores"] == {
        "A": 0,
        "B": 8,
        "C": 9,
    }
    with pytest.raises(ValueError, match="conflicting"):
        score([row["facts"][0], {**row["facts"][0], "points": 99}])


def test_packets_do_not_leak_and_reports_are_sealed():
    game = HiddenProfileEnvironment({}, dry_run=True)
    state = {
        "round_index": 0,
        "seat": 0,
        "phase": "discussion",
        "discussion": [],
        "commitment_messages": [],
        "proposal": None,
        "endorsements": [],
        "phase_limit": 8,
        "phase_turn": 0,
        "pre_reports": {1: {"choice": "SECRET", "scores": {}}},
    }
    obs = game.observation(state)
    serialized = json.dumps(obs)
    assert (
        "SECRET" not in serialized
        and "oracle" not in serialized
        and "packets" not in obs
    )
    assert "Secured a library-hours extension" in serialized
    assert "Negotiated a housing-service improvement" not in serialized
    assert all(
        set(f) == {"candidate", "points", "statement"} for f in obs["private_packet"]
    )
    game.episode.close()


def test_two_round_success_and_context_accrual():
    game = HiddenProfileEnvironment({"discussion_cycles": 2}, dry_run=True)
    objects = [id(a) for a in game.agents]
    trace = game.run()
    assert [r["choice"] for r in trace.final_state["rounds"]] == ["B", "C"]
    assert trace.metrics["optimal_rounds"] == 2
    assert trace.metrics["post_score_absolute_error"] == 0
    assert trace.metrics["preference_revisions"] == 8
    assert objects == [id(a.agent) for a in game.agents]
    for agent in game.agents:
        lengths = agent.request_history_lengths
        assert lengths == list(range(0, 2 * len(lengths), 2))
        assert any(
            '"round": 2' in m["content"] for m in agent.history if m["role"] == "user"
        )
    observations = [e for e in trace.events if e.type == "observation"]
    assert len(observations) == sum(len(a.request_history_lengths) for a in game.agents)
    assert all(e.data["visibility"] == "private" for e in observations)


class PassingParticipant(ScriptedParticipant):
    async def act(self, obs, tools):
        if obs["phase"] == "commitment":
            return {"kind": "pass"}
        return await super().act(obs, tools)


def test_no_consensus_is_honest_and_bounded():
    game = HiddenProfileEnvironment(
        {"discussion_cycles": 1, "commitment_cycles": 2}, dry_run=True
    )
    game.agents = [PassingParticipant(f"participant_{i + 1}") for i in range(4)]
    trace = game.run()
    assert trace.metrics["no_consensus_rounds"] == 2
    assert trace.metrics["optimal_rounds"] == 0
    assert all(
        r["choice"] is None and r["regret"] is None for r in trace.final_state["rounds"]
    )
    assert (
        len(
            [
                e
                for e in trace.events
                if e.type == "message" and e.data["phase"] == "commitment"
            ]
        )
        == 16
    )


def test_model_private_history_keeps_round_one():
    class Client:
        def __init__(self):
            self.messages = []

        def oneshot(self, messages):
            self.messages.append(copy.deepcopy(messages))
            return '{"kind":"pass"}'

    import asyncio

    client = Client()
    agent = HiddenProfileAgent(client, "participant_1")
    asyncio.run(agent.act({"round": 1, "private": "my secret"}, {}))
    asyncio.run(agent.act({"round": 2, "private": "new secret"}, {}))
    assert len(client.messages[0]) == 2 and len(client.messages[1]) == 4
    assert "my secret" in client.messages[1][1]["content"]


def test_export_has_real_nested_phases_and_is_pure():
    from a2a_engine.workflow import export_program

    doc = export_program(build_program(), "hidden_profile")
    assert doc["status"] == "available"
    assert len(doc["workflow"]["graphs"]) == 6
    assert any("discussion" in g["id"] for g in doc["workflow"]["graphs"])


def test_design_compilation_selects_full_packet_and_rejects_drift():
    from a2a_engine.compiler import compile
    from a2a_engine.design import Design
    from hidden_profile.declaration import DECLARATION
    from hidden_profile.environment import HiddenProfileConfig

    design = Design.model_validate(
        {
            "schema_version": 1,
            "release": "hidden_profile@v1",
            "parameters": {
                "sequence_id": {"randomize": True},
                "condition": {"pin": "full"},
                "discussion_cycles": {"pin": 1},
                "commitment_cycles": {"pin": 2},
            },
            "units": {"episodes_per_cell": 1},
            "seed": {"root": 19},
            "roster": [
                {
                    "id": f"p{i}",
                    "role": "participant",
                    "kind": "scripted",
                    "binding": "baseline",
                }
                for i in range(4)
            ],
        }
    )
    plan = compile(
        design,
        DECLARATION,
        load_bank(),
        experiment_id="profile-design",
        experiment_name="full-profile",
    )
    cfg = plan.preview_episode_config
    assert cfg["item_id"] == "caucus_full_01"
    assert cfg["condition"] == "full"
    assert cfg["sequence_id"] == "caucus_full_01"
    assert HiddenProfileConfig(**cfg).round_item_ids == [
        "caucus_full_01",
        "relief_full_01",
    ]
    game = HiddenProfileEnvironment(cfg, dry_run=True)
    trace = game.run()
    assert all(
        r["choice"] == "B"
        for r in trace.final_state["rounds"][0]["pre_reports"].values()
    )
    assert trace.final_state["rounds"][0]["preference_revisions"] == 0
    assert (
        HiddenProfileConfig(item_id="caucus_full_01").round_item_ids[0]
        == "caucus_full_01"
    )
    with pytest.raises(ValueError, match="frozen item"):
        HiddenProfileConfig(
            item_id="caucus_full_01",
            round_item_ids=["caucus_hidden_01", "relief_hidden_01"],
        )
    with pytest.raises(ValueError, match="frozen allocation"):
        HiddenProfileConfig(item_id="caucus_full_01", condition="hidden")


@pytest.mark.parametrize("identity_key", [None, "id", "participant_id"])
def test_chat_speakers_match_roster_and_sealed_text_never_enters_peer_history(
    identity_key,
):
    names = (
        [f"participant_{i}" for i in range(4)]
        if identity_key is None
        else ["alice", "bob", "charlie", "dana"]
    )
    agents = [
        {"type": "scripted", **({identity_key: name} if identity_key else {})}
        for name in names
    ]
    trace = HiddenProfileEnvironment(
        {"discussion_cycles": 1, "agents": agents}, dry_run=True
    ).run()
    assert {
        e.data["speaker"]
        for e in trace.events
        if e.type in ("message", "observation", "sealed_report")
    } == set(names)
    reports = [e for e in trace.events if e.type == "sealed_report"]
    assert len(reports) == 16
    assert all(
        e.data["visibility"] == "private" and "choice" in e.data["text"]
        for e in reports
    )
    for event in trace.events:
        if event.type != "observation":
            continue
        observation = json.loads(event.data["content"])
        assert all(
            "Sealed " not in message["text"] for message in observation["discussion"]
        )
        assert all(
            "Sealed " not in message["text"]
            for message in observation["commitment_messages"]
        )
        assert "pre_reports" not in observation and "post_reports" not in observation


def test_declaration_scalar_fields_and_bank_share_contract():
    from a2a_engine.testing import (
        assert_declaration_matches_config,
        assert_item_parameters_match_bank,
    )
    from hidden_profile.declaration import DECLARATION
    from hidden_profile.environment import HiddenProfileConfig

    assert_declaration_matches_config(DECLARATION, HiddenProfileConfig)
    assert_item_parameters_match_bank(DECLARATION, load_bank())
    selected = HiddenProfileConfig(sequence_id="caucus_full_01")
    assert selected.round_item_ids == ["caucus_full_01", "relief_full_01"]
    assert selected.condition == "full"


def test_matched_conditions_are_consistent_through_every_round():
    bank = load_bank()
    for item in bank.items:
        assert all(
            bank.get(round_id).raw["condition"] == item.raw["condition"]
            for round_id in item.params["round_item_ids"]
        )
    hidden = bank.get("relief_hidden_01").raw
    full = bank.get("relief_full_01").raw
    assert hidden["facts"] == full["facts"]
    assert (
        hidden["oracle_result"]["pooled_scores"]
        == full["oracle_result"]["pooled_scores"]
    )
    assert (
        full["oracle_result"]["packet_scores"]
        == [full["oracle_result"]["pooled_scores"]] * 4
    )
