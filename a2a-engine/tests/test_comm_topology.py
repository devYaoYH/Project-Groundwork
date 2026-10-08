from copy import deepcopy

import pytest

from a2a_engine.comm import Topology, legacy_channels


def topology(config=None, seats=(3, 0, 2, 1), **kwargs):
    return Topology(config, seats=seats, phases={"CHEAP_TALK", "VOLUNTARY", "DECISION"},
                    default_send_phases={"CHEAP_TALK"}, **kwargs)


@pytest.mark.parametrize(("graph", "extra", "expected"), [
    ("complete", {}, [(1, 2, 3), (0, 2, 3), (0, 1, 3), (0, 1, 2)]),
    ("ring", {}, [(1, 3), (0, 2), (1, 3), (0, 2)]),
    ("star", {"hub": 2}, [(2,), (2,), (0, 1, 3), (2,)]),
    ("edges", {"edges": [[3, 0], [0, 1]], "directed": False}, [(1, 3), (0,), (), (0,)]),
    ("edges", {"edges": [[3, 0], [0, 1]], "directed": True}, [(1,), (), (), (0,)]),
])
def test_graph_neighbors_are_numeric_ordered_and_bidirectional_when_undirected(graph, extra, expected):
    policy = topology({"default": {"graph": graph, **extra}}).effective(phase="CHEAP_TALK", round=0)
    assert [policy.neighbors(seat) for seat in range(4)] == expected


@pytest.mark.parametrize("seats, expected", [((7,), ()), ((7, 2), (2,)), ((8, 2, 7), (2, 8))])
def test_ring_small_and_sparse_seat_sets(seats, expected):
    policy = topology({"default": {"graph": "ring"}}, seats=seats).effective(phase="CHEAP_TALK", round=0)
    assert policy.neighbors(7) == expected


def test_phase_inheritance_empty_channel_map_and_no_authored_mutation():
    config = {
        "default": {"graph": "ring", "channels": {"dm": {"enabled": True}, "all_groupchat": {"enabled": True}},
                    "budget": {"per_agent_per_round": 5}},
        "phases": {"VOLUNTARY": {"graph": "star", "hub": 0, "budget": {}}, "DECISION": {"channels": {}}},
    }
    authored = deepcopy(config)
    policy = topology(config)
    voluntary = policy.effective(phase="VOLUNTARY", round=11)
    assert voluntary.graph == "star"
    assert voluntary.channels == ("dm", "all_groupchat")
    assert voluntary.per_agent_per_round == 5
    assert policy.effective(phase="DECISION", round=0).channels == ()
    assert config == authored


def test_partial_channel_fields_inherit_and_equivalent_policy_compares_equal():
    policy = topology({"default": {"channels": {"dm": {"enabled": False}, "all_groupchat": {"enabled": True}}},
                       "phases": {"VOLUNTARY": {"channels": {"dm": {"scope": "graph"}}}}})
    assert policy.effective(phase="CHEAP_TALK", round=0) == policy.effective(phase="VOLUNTARY", round=10)
    assert policy.effective(phase="DECISION", round=0).channels == ()


def test_phase_channel_overrides_inherit_in_memory_defaults_without_writing_them():
    config = {"phases": {"DECISION": {"channels": {"all_groupchat": {"enabled": True}}}}}
    authored = deepcopy(config)
    policy = topology(config)
    assert policy.effective(phase="DECISION", round=0).channels == ("dm", "all_groupchat")
    assert config == authored


def test_equivalent_edges_are_canonicalized_for_change_detection():
    policy = topology({"default": {"graph": "edges", "edges": [[0, 1], [2, 3]]},
                       "phases": {"VOLUNTARY": {"edges": [[3, 2], [1, 0], [0, 1]]}}})
    assert policy.effective(phase="CHEAP_TALK", round=0) == policy.effective(phase="VOLUNTARY", round=1)


@pytest.mark.parametrize("config", [
    {"default": {"graph": "tree"}},
    {"default": {"graph": "star"}},
    {"default": {"graph": "star", "hub": 99}},
    {"default": {"hub": True}},
    {"default": {"graph": "edges", "edges": [[0, 4]]}},
    {"default": {"graph": "edges", "edges": [[0, 0]]}},
    {"default": {"edges": [[0, "1"]]}},
    {"default": {"channels": {"groupchat": {"enabled": True}}}},
    {"default": {"channels": {"dm": {"enabled": "false"}}}},
    {"default": {"channels": {"participant_groupchat": {"scope": "meeting"}}}},
    {"default": {"budget": {"per_agent_per_round": -2}}},
    {"default": {"budget": {"per_agent_per_round": True}}},
    {"phases": {"TYPO": {}}},
    {"phases": {"DECISION": {"channels": {"unknown": {"enabled": False}}}}},
    {"rounds": []},
    {"default": {"unknown": 1}},
])
def test_invalid_policy_fails_before_a_run(config):
    with pytest.raises(ValueError):
        topology(config)


@pytest.mark.parametrize("communication", [{"permissions": {"send": True}}, {"topology": None}, {"topology": []}, []])
def test_reject_unsupported_permissions_and_malformed_topology(communication):
    with pytest.raises(ValueError):
        Topology.from_communication(communication, seats=[0, 1], phases={"CHEAP_TALK"}, default_send_phases={"CHEAP_TALK"})


@pytest.mark.parametrize("protocol, channels", [
    ("direct_message", {"dm"}), ("private", {"dm"}), ("meeting_chat", {"participant_groupchat"}),
    ("all_agent_chat", {"all_groupchat"}), ("mixed", {"dm", "all_groupchat"}),
    ("dm_and_participant_groupchat", {"dm", "participant_groupchat"}),
    ({"direct": True, "groupchat": True, "unknown": False}, {"dm", "all_groupchat"}),
])
def test_legacy_normalization(protocol, channels):
    assert legacy_channels(protocol) == channels
    policy = topology(communication_protocol=protocol, dm_cap=3)
    effective = policy.effective(phase="CHEAP_TALK", round=0)
    assert set(effective.channels) == channels
    assert effective.legacy and effective.per_agent_per_round == 3
    assert policy.effective(phase="DECISION", round=0).channels == ()


def test_explicit_policy_does_not_read_invalid_legacy_protocol_or_budget():
    policy = topology({}, communication_protocol="invalid", dm_cap=0)
    effective = policy.effective(phase="CHEAP_TALK", round=0)
    assert not effective.legacy and effective.per_agent_per_round == -1
    with pytest.raises(ValueError, match="unknown topology phase"):
        policy.effective(phase="UNKNOWN", round=0)
